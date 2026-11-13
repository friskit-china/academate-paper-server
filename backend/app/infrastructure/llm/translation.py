from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

from app.domain.tasks import redact_task_error_message
from app.domain.translation import (
    TranslationEntity,
    TranslationRequest,
    TranslationResult,
    TranslationUnit,
    TranslationUsage,
    Translator,
)
from app.infrastructure.llm.provider_http import (
    provider_http_error_message,
    read_provider_http_error,
    sanitize_provider_identifier,
)

DEFAULT_TRANSLATION_SYSTEM_PROMPT = (
    "Translate academic paper text into fluent Simplified Chinese. Preserve technical terms when "
    "a forced Chinese rendering would reduce precision. Identify named methods, models, datasets, "
    "metrics, algorithms, acronyms, and important technical terms. For every identified entity, "
    "return the exact Chinese substring used in translated_text as translated_term and the exact "
    "contiguous, case-sensitive English substring from source_text that directly and exclusively "
    "produced that Chinese term as source_term. Return each unique entity mapping at most once "
    "per unit. Never infer, broaden, combine non-contiguous "
    "words, or associate a nearby but different source phrase. Omit any uncertain entity. Do not "
    "return "
    "an entity when "
    "translated_term and source_term are the same text. Reorganize long sentences for "
    "native Chinese readability without adding, removing, explaining, or changing claims. Preserve "
    "every token shaped like ⟦MATH_n⟧ or ⟦REF_n⟧ exactly: never remove, duplicate, translate, "
    "reformat, "
    "surround, or move one. Return only the requested JSON object."
)

SEGMENTED_TRANSLATION_CONTRACT = (
    "The response contract below supersedes any earlier instruction to echo math or reference "
    "placeholders. Translate only source segments with type=text and return their exact "
    "segment_key values. Never include math/reference segments or placeholders in translated "
    "text; the server restores them from canonical data."
)

_GUARDED_PLACEHOLDER_PATTERN = re.compile(r"⟦(?:MATH|REF)_\d+⟧")


class TranslationProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        error_code: str | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(redact_task_error_message(message))
        self.error_type = sanitize_provider_identifier(error_type, limit=64) or "PROVIDER_ERROR"
        self.error_code = sanitize_provider_identifier(error_code, limit=128)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.provider_request_id = sanitize_provider_identifier(
            provider_request_id,
            limit=255,
        )


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "unit_key": {"type": "string"},
                        "translated_segments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "segment_key": {"type": "string"},
                                    "translated_text": {"type": "string"},
                                },
                                "required": ["segment_key", "translated_text"],
                                "additionalProperties": False,
                            },
                        },
                        "entities": {
                            "type": "array",
                            "maxItems": 64,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "translated_term": {
                                        "type": "string",
                                        "description": (
                                            "Exact contiguous substring in translated_text."
                                        ),
                                    },
                                    "source_term": {
                                        "type": "string",
                                        "description": (
                                            "Exact case-sensitive contiguous substring in "
                                            "source_text that directly corresponds to "
                                            "translated_term; omit the entity if uncertain."
                                        ),
                                    },
                                },
                                "required": ["translated_term", "source_term"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["unit_key", "translated_segments", "entities"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }


def _guarded_source_segments(unit: TranslationUnit) -> list[dict[str, str]]:
    guarded: dict[str, dict[str, str]] = {}
    for index, latex in enumerate(unit.math):
        placeholder = f"⟦MATH_{index}⟧"
        guarded[placeholder] = {
            "type": "math",
            "placeholder": placeholder,
            "latex": latex,
        }
    for index, reference in enumerate(unit.references):
        placeholder = f"⟦REF_{index}⟧"
        guarded[placeholder] = {
            "type": "reference",
            "placeholder": placeholder,
            "display_text": str(reference.get("text", "")),
        }

    segments: list[dict[str, str]] = []
    cursor = 0
    text_index = 0
    while cursor < len(unit.text):
        next_match = min(
            (
                (position, placeholder)
                for placeholder in guarded
                if (position := unit.text.find(placeholder, cursor)) >= 0
            ),
            default=None,
        )
        end = next_match[0] if next_match is not None else len(unit.text)
        if end > cursor:
            segments.append(
                {
                    "type": "text",
                    "segment_key": f"TEXT_{text_index}",
                    "source_text": unit.text[cursor:end],
                }
            )
            text_index += 1
        if next_match is None:
            break
        position, placeholder = next_match
        segments.append(guarded[placeholder])
        cursor = position + len(placeholder)
    if not segments:
        segments.append({"type": "text", "segment_key": "TEXT_0", "source_text": ""})
    return segments


def _prompt(request: TranslationRequest) -> str:
    return json.dumps(
        {
            "target_language": request.target_language,
            "paper_title": request.paper_title[:1000],
            "section_title": (request.section_title or "")[:500],
            "previous_context": (request.previous_context or "")[:2000],
            "next_context": (request.next_context or "")[:2000],
            "contract_constraints": [
                "Translate only source_segments whose type is text. Return one translated_segments "
                "entry for every text segment, preserving each segment_key exactly. Do not return, "
                "copy, translate, or synthesize math or reference segments; the server restores "
                "them deterministically.",
                "Do not include an entity when translated_term and source_term normalize to the "
                "same surface text.",
                "For every entity, source_term must be the exact case-sensitive contiguous source "
                "phrase that semantically and exclusively produced translated_term. Never combine "
                "non-contiguous words or select a nearby phrase; omit uncertain mappings.",
                "Within each unit, return each unique translated_term/source_term mapping at most "
                "once, even when the term occurs multiple times.",
                "Do not merge, omit, duplicate, or reorder text segments.",
            ],
            "units": [
                {
                    "unit_key": unit.key,
                    "source_segments": _guarded_source_segments(unit),
                }
                for unit in request.units
            ],
        },
        ensure_ascii=False,
    )


def _parse_translations(
    payload: object, request: TranslationRequest
) -> tuple[dict[str, str], dict[str, tuple[TranslationEntity, ...]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("translations"), list):
        raise TranslationProviderError(
            "Translation provider returned an invalid response shape.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_schema",
        )
    translations: dict[str, str] = {}
    entities: dict[str, tuple[TranslationEntity, ...]] = {}
    for item in payload["translations"]:
        if not isinstance(item, dict):
            raise TranslationProviderError(
                "Translation provider returned an invalid unit.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_unit",
            )
        key = item.get("unit_key")
        translated_segments = item.get("translated_segments")
        entity_items = item.get("entities")
        if (
            not isinstance(key, str)
            or not isinstance(translated_segments, list)
            or key in translations
        ):
            raise TranslationProviderError(
                "Translation provider returned duplicate or invalid unit fields.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_unit",
                retryable=True,
            )
        unit = next((candidate for candidate in request.units if candidate.key == key), None)
        if unit is None:
            raise TranslationProviderError(
                "Translation provider returned an unexpected unit key.",
                error_type="INVALID_RESPONSE",
                error_code="unit_mismatch",
                retryable=True,
            )
        source_segments = _guarded_source_segments(unit)
        translated_by_key: dict[str, str] = {}
        for segment in translated_segments:
            if (
                not isinstance(segment, dict)
                or not isinstance(segment.get("segment_key"), str)
                or not isinstance(segment.get("translated_text"), str)
                or segment["segment_key"] in translated_by_key
            ):
                raise TranslationProviderError(
                    "Translation provider returned an invalid translated text segment.",
                    error_type="INVALID_RESPONSE",
                    error_code="invalid_segment",
                    retryable=True,
                )
            if _GUARDED_PLACEHOLDER_PATTERN.search(segment["translated_text"]):
                raise TranslationProviderError(
                    "Translation provider inserted a server-owned placeholder into a text segment.",
                    error_type="INVALID_RESPONSE",
                    error_code="guarded_placeholder_in_text_segment",
                    retryable=True,
                )
            translated_by_key[segment["segment_key"]] = segment["translated_text"]
        expected_segment_keys = {
            segment["segment_key"] for segment in source_segments if segment["type"] == "text"
        }
        if set(translated_by_key) != expected_segment_keys:
            raise TranslationProviderError(
                "Translation provider did not return the requested text segment keys.",
                error_type="INVALID_RESPONSE",
                error_code="segment_mismatch",
                retryable=True,
            )
        translations[key] = "".join(
            translated_by_key[segment["segment_key"]]
            if segment["type"] == "text"
            else segment["placeholder"]
            for segment in source_segments
        )
        parsed_entities: list[TranslationEntity] = []
        for entity in entity_items if isinstance(entity_items, list) else []:
            if (
                not isinstance(entity, dict)
                or not isinstance(entity.get("translated_term"), str)
                or not isinstance(entity.get("source_term"), str)
            ):
                parsed_entities.append(TranslationEntity(translated_term="", source_term=""))
                continue
            parsed_entities.append(
                TranslationEntity(
                    translated_term=entity["translated_term"],
                    source_term=entity["source_term"],
                )
            )
        entities[key] = tuple(parsed_entities)
    expected = {unit.key for unit in request.units}
    if set(translations) != expected:
        raise TranslationProviderError(
            "Translation provider did not return the requested unit keys.",
            error_type="INVALID_RESPONSE",
            error_code="unit_mismatch",
            retryable=True,
        )
    return translations, entities


def _retry_after(headers: Message | None) -> int | None:
    if headers is None:
        return None
    value = headers.get("retry-after")
    try:
        return min(300, max(0, int(value))) if value is not None else None
    except ValueError:
        return None


def _http_error(error: urllib.error.HTTPError, client_request_id: str) -> TranslationProviderError:
    diagnostic = read_provider_http_error(error, client_request_id)
    if diagnostic.is_content_safety_rejection:
        error_type, retryable = "CONTENT_REJECTED", False
    elif error.code in {401, 403}:
        error_type, retryable = "AUTHENTICATION", False
    elif error.code == 429:
        error_type, retryable = "RATE_LIMIT", True
    elif error.code >= 500:
        error_type, retryable = "PROVIDER_5XX", True
    elif error.code in {400, 404, 422}:
        error_type, retryable = "REQUEST_REJECTED", False
    else:
        error_type, retryable = "PROVIDER_HTTP", False
    return TranslationProviderError(
        provider_http_error_message("Translation", diagnostic),
        error_type=error_type,
        error_code=diagnostic.error_code,
        retryable=retryable,
        retry_after_seconds=_retry_after(error.headers),
        provider_request_id=diagnostic.request_id,
    )


@dataclass(frozen=True, slots=True)
class GLMChatTranslator:
    api_key: str = field(repr=False)
    model: str
    api_base: str = ""
    timeout_seconds: int = 60
    provider: str = "glm"

    def _endpoint(self) -> str:
        base = self.api_base.strip().rstrip("/") or "https://open.bigmodel.cn/api/paas/v4"
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def translate(self, request: TranslationRequest, *, system_prompt: str) -> TranslationResult:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n{SEGMENTED_TRANSLATION_CONTRACT}\nJSON schema:\n"
                        f"{json.dumps(_schema(), ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": _prompt(request)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        client_request_id = str(uuid.uuid4())
        http_request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Client-Request-Id": client_request_id,
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                response_payload = json.load(response)
                provider_request_id = sanitize_provider_identifier(
                    response.headers.get("x-request-id"),
                    limit=255,
                )
        except urllib.error.HTTPError as error:
            raise _http_error(error, client_request_id) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise TranslationProviderError(
                f"Translation provider network request failed: {type(error).__name__}",
                error_type="TIMEOUT" if isinstance(error, TimeoutError) else "NETWORK",
                error_code=type(error).__name__,
                retryable=True,
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TranslationProviderError(
                "Translation provider returned invalid JSON.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_json",
            ) from error
        try:
            output_text = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise TranslationProviderError(
                "Translation provider returned no structured output.",
                error_type="CONTENT_REJECTED",
                error_code="no_output",
            ) from error
        if not isinstance(output_text, str) or not output_text.strip():
            raise TranslationProviderError(
                "Translation provider returned no structured output.",
                error_type="CONTENT_REJECTED",
                error_code="no_output",
            )
        try:
            structured = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise TranslationProviderError(
                "Translation provider returned invalid structured output.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_output_json",
            ) from error
        usage_payload = response_payload.get("usage", {})
        usage = TranslationUsage(
            input_tokens=(
                usage_payload.get("prompt_tokens") if isinstance(usage_payload, dict) else None
            ),
            output_tokens=(
                usage_payload.get("completion_tokens") if isinstance(usage_payload, dict) else None
            ),
        )
        translations, entities = _parse_translations(structured, request)
        return TranslationResult(
            translations=translations,
            entities=entities,
            usage=usage,
            latency_ms=round((time.monotonic() - started) * 1000),
            provider_request_id=(
                provider_request_id
                or sanitize_provider_identifier(
                    response_payload.get("id") if isinstance(response_payload, dict) else None,
                    limit=255,
                )
                or client_request_id
            ),
        )


@dataclass(frozen=True, slots=True)
class FakeTranslator:
    model: str = "fake-translation-v1"
    provider: str = "fake"

    def translate(self, request: TranslationRequest, *, system_prompt: str) -> TranslationResult:
        del system_prompt
        return TranslationResult(
            translations={unit.key: f"译：{unit.text}" for unit in request.units},
            entities={unit.key: () for unit in request.units},
            usage=TranslationUsage(
                input_tokens=len(request.units), output_tokens=len(request.units)
            ),
            latency_ms=1,
            provider_request_id="fake-request",
        )


def build_translator(
    *, api_key: str, model: str, api_base: str, provider: str, timeout_seconds: int = 60
) -> Translator:
    normalized = provider.strip().lower()
    if normalized in {"glm", "deepseek"}:
        if normalized == "deepseek" and not api_base.strip():
            raise ValueError("DeepSeek requires an explicit API base.")
        return GLMChatTranslator(
            api_key=api_key,
            model=model,
            api_base=api_base,
            timeout_seconds=timeout_seconds,
            provider=normalized,
        )
    raise ValueError(f"Unsupported translation provider: {provider}")
