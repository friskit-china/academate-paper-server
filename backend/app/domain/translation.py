from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

TRANSLATION_PROMPT_VERSION = "zh-academic-entities-v6"
MATH_PLACEHOLDER_PATTERN = re.compile(r"⟦MATH_(\d+)⟧")
REFERENCE_PLACEHOLDER_PATTERN = re.compile(r"⟦REF_(\d+)⟧")


@dataclass(frozen=True, slots=True)
class TranslationUnit:
    key: str
    text: str
    math: tuple[str, ...] = ()
    references: tuple[dict[str, Any], ...] = ()

    @property
    def math_placeholders(self) -> tuple[str, ...]:
        return tuple(f"⟦MATH_{index}⟧" for index in range(len(self.math)))

    @property
    def reference_placeholders(self) -> tuple[str, ...]:
        return tuple(f"⟦REF_{index}⟧" for index in range(len(self.references)))

    @property
    def placeholders(self) -> tuple[str, ...]:
        return (*self.math_placeholders, *self.reference_placeholders)


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    target_language: str
    paper_title: str
    section_title: str | None
    previous_context: str | None
    next_context: str | None
    units: tuple[TranslationUnit, ...]


@dataclass(frozen=True, slots=True)
class TranslationUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TranslationEntity:
    translated_term: str
    source_term: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    translations: dict[str, str]
    entities: dict[str, tuple[TranslationEntity, ...]] | None = None
    usage: TranslationUsage = TranslationUsage()
    latency_ms: int | None = None
    provider_request_id: str | None = None


class Translator(Protocol):
    provider: str
    model: str

    def translate(
        self, request: TranslationRequest, *, system_prompt: str
    ) -> TranslationResult: ...


def _segments_to_unit_text(
    segments: object,
    fallback: str | None,
) -> tuple[str, tuple[str, ...], tuple[dict[str, Any], ...]]:
    if not isinstance(segments, list):
        return (fallback or "").strip(), (), ()
    text_parts: list[str] = []
    math: list[str] = []
    references: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "math" and isinstance(segment.get("latex"), str):
            placeholder = f"⟦MATH_{len(math)}⟧"
            math.append(segment["latex"])
            text_parts.append(placeholder)
        elif segment.get("type") == "reference" and isinstance(segment.get("text"), str):
            placeholder = f"⟦REF_{len(references)}⟧"
            references.append(dict(segment))
            text_parts.append(placeholder)
        elif segment.get("type") == "text" and isinstance(segment.get("text"), str):
            text_parts.append(segment["text"])
    text = " ".join(part.strip() for part in text_parts if part.strip()).strip()
    return text or (fallback or "").strip(), tuple(math), tuple(references)


def extract_translation_units(
    *,
    block_type: str,
    source_text: str | None,
    render_payload: dict[str, Any],
) -> tuple[TranslationUnit, ...]:
    units: list[TranslationUnit] = []

    def add(key: str, fallback: str | None, segments: object = None) -> None:
        text, math, references = _segments_to_unit_text(segments, fallback)
        if text:
            units.append(
                TranslationUnit(key=key, text=text, math=math, references=references)
            )

    if block_type == "ABSTRACT":
        add("abstract", str(render_payload.get("text") or source_text or ""))
    elif block_type == "SECTION":
        add("title", str(render_payload.get("text") or source_text or ""))
    elif block_type in {"PARAGRAPH", "LATEX_BLOCK"}:
        add(
            "text",
            str(source_text or render_payload.get("text") or ""),
            render_payload.get("segments"),
        )
    elif block_type == "LIST":
        items = render_payload.get("items")
        item_segments = render_payload.get("item_segments")
        if isinstance(items, list):
            for index, item in enumerate(items):
                if not isinstance(item, str):
                    continue
                segments = (
                    item_segments[index]
                    if isinstance(item_segments, list) and index < len(item_segments)
                    else None
                )
                add(f"item:{index}", item, segments)
    elif block_type == "THEOREM":
        title = render_payload.get("title")
        if isinstance(title, str) and title.strip():
            add("title", title, render_payload.get("title_segments"))
        parts = render_payload.get("parts")
        if isinstance(parts, list):
            for part_index, part in enumerate(parts):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "prose" and isinstance(part.get("text"), str):
                    add(f"part:{part_index}", part["text"], part.get("segments"))
                elif part.get("type") == "list" and isinstance(part.get("items"), list):
                    item_segments = part.get("item_segments")
                    for item_index, item in enumerate(part["items"]):
                        if not isinstance(item, str):
                            continue
                        segments = (
                            item_segments[item_index]
                            if isinstance(item_segments, list) and item_index < len(item_segments)
                            else None
                        )
                        add(f"part:{part_index}:item:{item_index}", item, segments)
        elif source_text:
            add("text", source_text, render_payload.get("segments"))
    elif block_type in {"FIGURE", "TABLE", "ALGORITHM"}:
        caption = render_payload.get("caption")
        if isinstance(caption, str) and caption.strip():
            add("caption", caption, render_payload.get("caption_segments"))
    return tuple(units)


def context_hash(
    *,
    paper_title: str,
    section_title: str | None,
    previous_context: str | None,
    next_context: str | None,
) -> str:
    payload = {
        "paper_title": paper_title,
        "section_title": section_title,
        "previous_context": previous_context,
        "next_context": next_context,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def translation_input_hash(
    *,
    language: str,
    source_hash: str,
    unit_hash: str,
    context_hash_value: str,
    prompt_version: str,
    prompt_hash: str,
    provider: str,
    model: str,
) -> str:
    value = "\0".join(
        [
            language,
            source_hash,
            unit_hash,
            context_hash_value,
            prompt_version,
            prompt_hash,
            provider,
            model,
        ]
    )
    return hashlib.sha256(value.encode()).hexdigest()


def translated_payload(
    units: tuple[TranslationUnit, ...],
    translations: dict[str, str],
    entities: dict[str, tuple[TranslationEntity, ...]] | None = None,
) -> tuple[dict[str, Any], str]:
    expected = {unit.key for unit in units}
    if set(translations) != expected:
        raise ValueError("Translated unit keys do not match the request.")
    payload_units: list[dict[str, Any]] = []
    flat: list[str] = []
    entity_map = entities or {}
    for unit in units:
        translated = _normalize_guarded_placeholders(translations[unit.key], unit).strip()
        if not translated:
            raise ValueError(f"Translated unit {unit.key} is empty.")
        if len(translated) > max(4000, len(unit.text) * 8):
            raise ValueError(f"Translated unit {unit.key} exceeded its output limit.")
        for placeholder in unit.math_placeholders:
            if translated.count(placeholder) != unit.text.count(placeholder):
                raise ValueError(f"Translated unit {unit.key} changed a math placeholder.")
        for placeholder in unit.reference_placeholders:
            if translated.count(placeholder) != unit.text.count(placeholder):
                raise ValueError(f"Translated unit {unit.key} changed a reference placeholder.")
        unexpected_math = {
            match.group(0)
            for match in MATH_PLACEHOLDER_PATTERN.finditer(translated)
            if match.group(0) not in unit.placeholders
        }
        unexpected_references = {
            match.group(0)
            for match in REFERENCE_PLACEHOLDER_PATTERN.finditer(translated)
            if match.group(0) not in unit.placeholders
        }
        if unexpected_math:
            raise ValueError(f"Translated unit {unit.key} added a math placeholder.")
        if unexpected_references:
            raise ValueError(f"Translated unit {unit.key} added a reference placeholder.")
        unit_entities = entity_map.get(unit.key, ())
        discarded_reasons: Counter[str] = Counter()

        if len(unit_entities) > 64:
            discarded_reasons["entity_limit_exceeded"] += len(unit_entities) - 64
        normalized_entities: list[TranslationEntity] = []
        seen_entities: set[tuple[str, str]] = set()
        seen_translated_terms: set[str] = set()
        seen_source_terms: set[str] = set()
        for entity in unit_entities[:64]:
            translated_term = entity.translated_term.strip()
            source_term = entity.source_term.strip()
            pair = (translated_term, source_term)
            if not translated_term or not source_term:
                discarded_reasons["empty_term"] += 1
                continue
            if len(translated_term) > 200 or len(source_term) > 200:
                discarded_reasons["term_too_long"] += 1
                continue
            if pair in seen_entities:
                discarded_reasons["duplicate_pair"] += 1
                continue
            seen_entities.add(pair)
            normalized_translated = _normalized_term(translated_term)
            normalized_source = _normalized_term(source_term)
            if normalized_translated == normalized_source:
                discarded_reasons["same_surface_text"] += 1
                continue
            if translated_term not in translated:
                discarded_reasons["translated_term_not_found"] += 1
                continue
            if not _contains_exact_source_term(unit.text, source_term):
                discarded_reasons["source_term_not_found"] += 1
                continue
            if normalized_translated in seen_translated_terms:
                discarded_reasons["duplicate_translated_term"] += 1
                continue
            if normalized_source in seen_source_terms:
                discarded_reasons["duplicate_source_term"] += 1
                continue
            seen_translated_terms.add(normalized_translated)
            seen_source_terms.add(normalized_source)
            normalized_entities.append(
                TranslationEntity(translated_term=translated_term, source_term=source_term)
            )
        segments: list[dict[str, Any]] = []
        terms = sorted(
            {entity.translated_term for entity in normalized_entities},
            key=len,
            reverse=True,
        )
        entity_by_term = {entity.translated_term: entity for entity in normalized_entities}
        token_pattern = re.compile(
            "|".join(
                [
                    MATH_PLACEHOLDER_PATTERN.pattern,
                    REFERENCE_PLACEHOLDER_PATTERN.pattern,
                    *(re.escape(term) for term in terms),
                ]
            )
        )
        cursor = 0
        for match in token_pattern.finditer(translated):
            if match.start() > cursor:
                segments.append({"type": "text", "text": translated[cursor : match.start()]})
            token = match.group(0)
            math_match = MATH_PLACEHOLDER_PATTERN.fullmatch(token)
            if math_match is not None:
                math_index = int(math_match.group(1))
                segments.append({"type": "math", "latex": unit.math[math_index]})
            elif (reference_match := REFERENCE_PLACEHOLDER_PATTERN.fullmatch(token)) is not None:
                reference_index = int(reference_match.group(1))
                segments.append(dict(unit.references[reference_index]))
            else:
                entity = entity_by_term[token]
                segments.append(
                    {
                        "type": "entity",
                        "text": token,
                        "source_term": entity.source_term,
                    }
                )
            cursor = match.end()
        if cursor < len(translated):
            segments.append({"type": "text", "text": translated[cursor:]})
        unit_payload = {
            "key": unit.key,
            "text": translated,
            "segments": segments,
            "entities": [
                {
                    "translated_term": entity.translated_term,
                    "source_term": entity.source_term,
                }
                for entity in normalized_entities
            ],
        }
        if discarded_reasons:
            unit_payload["entity_validation"] = {
                "accepted": len(normalized_entities),
                "discarded": sum(discarded_reasons.values()),
                "reasons": dict(discarded_reasons),
            }
        payload_units.append(unit_payload)
        flat.append(translated)
    return {"units": payload_units}, "\n".join(flat)


def translated_display_text(
    translated_text: str | None,
    translated_payload_json: object,
) -> str:
    """Materialize the user-visible translation used by text-range selectors.

    ``translated_text`` intentionally retains guarded math placeholders for the
    provider/audit contract. Annotation selectors instead need the same semantic
    text the reader renders, with each placeholder restored to its canonical
    LaTeX source and entity hover metadata omitted.
    """
    if not isinstance(translated_payload_json, dict):
        return translated_text or ""
    units = translated_payload_json.get("units")
    if not isinstance(units, list):
        return translated_text or ""
    materialized_units: list[str] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        segments = unit.get("segments")
        if not isinstance(segments, list):
            unit_text = unit.get("text")
            if isinstance(unit_text, str):
                materialized_units.append(unit_text)
            continue
        parts: list[str] = []
        valid = True
        for segment in segments:
            if not isinstance(segment, dict):
                valid = False
                break
            segment_type = segment.get("type")
            key = "latex" if segment_type == "math" else "text"
            value = segment.get(key)
            if segment_type not in {"text", "entity", "math", "reference"} or not isinstance(
                value, str
            ):
                valid = False
                break
            parts.append(value)
        if valid:
            materialized_units.append("".join(parts))
        elif isinstance(unit.get("text"), str):
            materialized_units.append(unit["text"])
    return "\n".join(materialized_units) if materialized_units else (translated_text or "")


def _normalized_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _contains_exact_source_term(source_text: str, source_term: str) -> bool:
    prefix = r"(?<![A-Za-z0-9])" if source_term[0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9])" if source_term[-1].isalnum() else ""
    return re.search(f"{prefix}{re.escape(source_term)}{suffix}", source_text) is not None


def _normalize_guarded_placeholders(value: str, unit: TranslationUnit) -> str:
    normalized = value
    for index, latex in enumerate(unit.math):
        placeholder = f"⟦MATH_{index}⟧"
        variant = re.compile(
            rf"(?:⟦|【|\[)\s*MATH[\s_-]*{index}\s*(?:⟧|】|\])",
            flags=re.IGNORECASE,
        )
        normalized = variant.sub(placeholder, normalized)
        normalized = re.sub(rf"`\s*{re.escape(placeholder)}\s*`", placeholder, normalized)
        if normalized.count(placeholder) == 0 and len(latex.strip()) >= 3:
            latex_variants = (
                f"${latex}$",
                rf"\({latex}\)",
                latex,
            )
            for candidate in latex_variants:
                if normalized.count(candidate) == 1:
                    normalized = normalized.replace(candidate, placeholder, 1)
                    break
    for index in range(len(unit.references)):
        placeholder = f"⟦REF_{index}⟧"
        variant = re.compile(
            rf"(?:⟦|【|\[)\s*REF[\s_-]*{index}\s*(?:⟧|】|\])",
            flags=re.IGNORECASE,
        )
        normalized = variant.sub(placeholder, normalized)
        normalized = re.sub(rf"`\s*{re.escape(placeholder)}\s*`", placeholder, normalized)
    return normalized
