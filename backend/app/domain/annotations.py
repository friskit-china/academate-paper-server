from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.domain.enums import AnnotationResolutionStatus

SELECTOR_VERSION = 1
MAX_EXACT_QUOTE_CODEPOINTS = 2_000
MAX_CONTEXT_CODEPOINTS = 64


class SelectorValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SelectorResolution:
    status: AnnotationResolutionStatus
    start_offset: int | None
    end_offset: int | None
    reason: str | None = None


def normalize_selector_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def selector_text_hash(value: str) -> str:
    return hashlib.sha256(normalize_selector_text(value).encode("utf-8")).hexdigest()


def validate_selector(selector: object, *, block_id: str, target_text: str) -> dict[str, Any]:
    if not isinstance(selector, dict):
        raise SelectorValidationError("A versioned selector object is required.")
    version = selector.get("version")
    selected_block_id = selector.get("block_id")
    start = selector.get("start_offset")
    end = selector.get("end_offset")
    exact = selector.get("exact_quote")
    prefix = selector.get("prefix", "")
    suffix = selector.get("suffix", "")
    text_hash = selector.get("text_hash")
    if version != SELECTOR_VERSION:
        raise SelectorValidationError("Unsupported selector version.")
    if selected_block_id != block_id:
        raise SelectorValidationError("Selector block does not match the annotation target.")
    if isinstance(start, bool) or not isinstance(start, int):
        raise SelectorValidationError("Selector start_offset must be an integer.")
    if isinstance(end, bool) or not isinstance(end, int):
        raise SelectorValidationError("Selector end_offset must be an integer.")
    if not isinstance(exact, str) or not exact:
        raise SelectorValidationError("Selector exact_quote must not be empty.")
    if not isinstance(prefix, str) or not isinstance(suffix, str):
        raise SelectorValidationError("Selector prefix and suffix must be strings.")
    if len(exact) > MAX_EXACT_QUOTE_CODEPOINTS:
        raise SelectorValidationError("Selector exact_quote is too long.")
    if len(prefix) > MAX_CONTEXT_CODEPOINTS or len(suffix) > MAX_CONTEXT_CODEPOINTS:
        raise SelectorValidationError("Selector prefix or suffix is too long.")
    normalized_text = normalize_selector_text(target_text)
    normalized_exact = normalize_selector_text(exact)
    normalized_prefix = normalize_selector_text(prefix)
    normalized_suffix = normalize_selector_text(suffix)
    if start < 0 or end <= start or end > len(normalized_text):
        raise SelectorValidationError("Selector offsets are outside the target text.")
    if normalized_text[start:end] != normalized_exact:
        raise SelectorValidationError("Selector offsets do not match exact_quote.")
    expected_prefix = normalized_text[max(0, start - len(normalized_prefix)) : start]
    expected_suffix = normalized_text[end : end + len(normalized_suffix)]
    if expected_prefix != normalized_prefix or expected_suffix != normalized_suffix:
        raise SelectorValidationError("Selector context does not match the target text.")
    if text_hash is not None and (
        not isinstance(text_hash, str) or text_hash != selector_text_hash(normalized_text)
    ):
        raise SelectorValidationError("Selector text_hash does not match the target text.")
    return {
        "version": SELECTOR_VERSION,
        "block_id": block_id,
        "start_offset": start,
        "end_offset": end,
        "exact_quote": normalized_exact,
        "prefix": normalized_prefix,
        "suffix": normalized_suffix,
        **({"text_hash": text_hash} if text_hash is not None else {}),
    }


def resolve_selector(selector: object, target_text: str) -> SelectorResolution:
    if not isinstance(selector, dict):
        return SelectorResolution(
            AnnotationResolutionStatus.UNRESOLVED, None, None, "INVALID_SELECTOR"
        )
    try:
        start = selector["start_offset"]
        end = selector["end_offset"]
        exact = normalize_selector_text(selector["exact_quote"])
        prefix = normalize_selector_text(selector.get("prefix", ""))
        suffix = normalize_selector_text(selector.get("suffix", ""))
    except (KeyError, TypeError):
        return SelectorResolution(
            AnnotationResolutionStatus.UNRESOLVED, None, None, "INVALID_SELECTOR"
        )
    if (
        selector.get("version") != SELECTOR_VERSION
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not isinstance(exact, str)
        or not exact
        or not isinstance(prefix, str)
        or not isinstance(suffix, str)
    ):
        return SelectorResolution(
            AnnotationResolutionStatus.UNRESOLVED, None, None, "INVALID_SELECTOR"
        )
    text = normalize_selector_text(target_text)
    if 0 <= start < end <= len(text) and text[start:end] == exact:
        return SelectorResolution(AnnotationResolutionStatus.RESOLVED_OFFSETS, start, end)

    candidates: list[int] = []
    cursor = 0
    while True:
        position = text.find(exact, cursor)
        if position < 0:
            break
        candidates.append(position)
        cursor = position + 1
    if not candidates:
        return SelectorResolution(
            AnnotationResolutionStatus.UNRESOLVED, None, None, "EXACT_QUOTE_NOT_FOUND"
        )
    if len(candidates) == 1:
        position = candidates[0]
        return SelectorResolution(
            AnnotationResolutionStatus.RESOLVED_EXACT, position, position + len(exact)
        )
    if not prefix and not suffix:
        return SelectorResolution(
            AnnotationResolutionStatus.UNRESOLVED, None, None, "EXACT_QUOTE_AMBIGUOUS"
        )
    contextual = []
    for position in candidates:
        quote_end = position + len(exact)
        prefix_matches = not prefix or text[max(0, position - len(prefix)) : position] == prefix
        suffix_matches = not suffix or text[quote_end : quote_end + len(suffix)] == suffix
        if prefix_matches and suffix_matches:
            contextual.append(position)
    if len(contextual) == 1:
        position = contextual[0]
        return SelectorResolution(
            AnnotationResolutionStatus.RESOLVED_CONTEXT, position, position + len(exact)
        )
    return SelectorResolution(
        AnnotationResolutionStatus.UNRESOLVED,
        None,
        None,
        "CONTEXT_AMBIGUOUS" if contextual else "CONTEXT_MISMATCH",
    )
