from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

from app.domain.paper_map import (
    PAPER_MAP_ANSWER_STATUSES,
    PAPER_MAP_CONCEPT_TYPES,
    PAPER_MAP_EDGE_TYPES,
    PAPER_TYPES,
    PaperMapAnswerRequest,
    PaperMapAnswerResult,
    PaperMapCitation,
    PaperMapDetail,
    PaperMapGenerator,
    PaperMapPlanRequest,
    PaperMapPlanResult,
    PaperMapQuestionSelection,
    PaperMapRelation,
    paper_map_context_payload,
)
from app.domain.tasks import redact_task_error_message
from app.infrastructure.llm.provider_http import (
    provider_http_error_message,
    read_provider_http_error,
    sanitize_provider_identifier,
)


class PaperMapProviderError(RuntimeError):
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


def _retry_after(headers: Message | None) -> int | None:
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
        return min(300, max(0, int(value))) if value is not None else None
    except ValueError:
        return None


def _http_error(error: urllib.error.HTTPError, request_id: str) -> PaperMapProviderError:
    diagnostic = read_provider_http_error(error, request_id)
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
    return PaperMapProviderError(
        provider_http_error_message("Paper map", diagnostic),
        error_type=error_type,
        error_code=diagnostic.error_code,
        retryable=retryable,
        retry_after_seconds=_retry_after(error.headers),
        provider_request_id=diagnostic.request_id,
    )


def _plan_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "primary_type": {"type": "string", "enum": sorted(PAPER_TYPES)},
            "secondary_types": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(PAPER_TYPES)},
                "maxItems": 4,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "questions": {
                "type": "array",
                "minItems": 4,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "question_key": {"type": "string"},
                        "rationale": {"type": "string"},
                        "evidence_block_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 6,
                        },
                    },
                    "required": ["question_key", "rationale", "evidence_block_ids"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["primary_type", "secondary_types", "confidence", "questions"],
        "additionalProperties": False,
    }


def _answer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "answer_status": {"type": "string", "enum": sorted(PAPER_MAP_ANSWER_STATUSES)},
            "answer": {"type": "string"},
            "citations": {
                "type": "array",
                "maxItems": 16,
                "items": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["block_id", "quote"],
                    "additionalProperties": False,
                },
            },
            "concepts": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "concept_type": {
                            "type": "string",
                            "enum": sorted(PAPER_MAP_CONCEPT_TYPES),
                        },
                        "label": {"type": "string"},
                        "body": {"type": "string"},
                        "citation_indexes": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0},
                            "maxItems": 6,
                        },
                    },
                    "required": [
                        "key",
                        "concept_type",
                        "label",
                        "body",
                        "citation_indexes",
                    ],
                    "additionalProperties": False,
                },
            },
            "relations": {
                "type": "array",
                "maxItems": 16,
                "items": {
                    "type": "object",
                    "properties": {
                        "source_key": {"type": "string"},
                        "target_key": {"type": "string"},
                        "edge_type": {
                            "type": "string",
                            "enum": sorted(PAPER_MAP_EDGE_TYPES - {"HAS_QUESTION"}),
                        },
                    },
                    "required": ["source_key", "target_key", "edge_type"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["answer_status", "answer", "citations", "concepts", "relations"],
        "additionalProperties": False,
    }


def _plan_payload(request: PaperMapPlanRequest) -> dict[str, Any]:
    return {
        "paper_id": request.paper_id,
        "paper_title": request.paper_title,
        "paper_abstract": request.paper_abstract,
        "question_candidates": [
            {
                "question_key": item.question_key,
                "question_text": item.question_text,
                "dimension": item.dimension,
                "paper_types": item.paper_types,
                "user_defined": item.user_defined,
            }
            for item in request.question_candidates
        ],
        "contexts": paper_map_context_payload(request.context),
    }


def _answer_payload(request: PaperMapAnswerRequest) -> dict[str, Any]:
    return {
        "paper_id": request.paper_id,
        "paper_title": request.paper_title,
        "question_id": request.question_id,
        "question_key": request.question_key,
        "question": request.question,
        "contexts": paper_map_context_payload(request.context),
    }


def _parse_plan(payload: object, allowed: set[str]) -> PaperMapPlanResult:
    if not isinstance(payload, dict):
        raise PaperMapProviderError(
            "Paper map planner returned an invalid object.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_schema",
            retryable=True,
        )
    primary = payload.get("primary_type")
    secondary = payload.get("secondary_types")
    confidence = payload.get("confidence")
    questions = payload.get("questions")
    if (
        primary not in PAPER_TYPES
        or not isinstance(secondary, list)
        or not isinstance(confidence, (int, float))
        or not isinstance(questions, list)
    ):
        raise PaperMapProviderError(
            "Paper map planner returned invalid classification.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_classification",
            retryable=True,
        )
    parsed: list[PaperMapQuestionSelection] = []
    seen: set[str] = set()
    for item in questions:
        if not isinstance(item, dict):
            continue
        key, rationale, evidence = (
            item.get("question_key"),
            item.get("rationale"),
            item.get("evidence_block_ids"),
        )
        if (
            key not in allowed
            or key in seen
            or not isinstance(rationale, str)
            or not rationale.strip()
            or not isinstance(evidence, list)
            or not all(isinstance(value, str) for value in evidence)
        ):
            continue
        seen.add(key)
        parsed.append(PaperMapQuestionSelection(key, rationale.strip()[:1200], tuple(evidence[:6])))
    if not 4 <= len(parsed) <= 10:
        raise PaperMapProviderError(
            "Paper map planner did not select 4–10 valid questions.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_questions",
            retryable=True,
        )
    return PaperMapPlanResult(
        primary_type=primary,
        secondary_types=tuple(
            value for value in secondary if value in PAPER_TYPES and value != primary
        )[:4],
        confidence=min(1.0, max(0.0, float(confidence))),
        questions=tuple(parsed),
    )


def _parse_answer(payload: object) -> PaperMapAnswerResult:
    if not isinstance(payload, dict):
        raise PaperMapProviderError(
            "Paper map answer returned an invalid object.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_schema",
            retryable=True,
        )
    status, answer = payload.get("answer_status"), payload.get("answer")
    citations, details, relations = (
        payload.get("citations"),
        payload.get("concepts"),
        payload.get("relations"),
    )
    if (
        status not in PAPER_MAP_ANSWER_STATUSES
        or not isinstance(answer, str)
        or not answer.strip()
        or len(answer) > 20_000
        or not isinstance(citations, list)
        or not isinstance(details, list)
        or not isinstance(relations, list)
    ):
        raise PaperMapProviderError(
            "Paper map answer returned invalid content.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_answer",
            retryable=True,
        )
    parsed_citations: list[PaperMapCitation] = []
    for item in citations[:16]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("block_id"), str)
            or not isinstance(item.get("quote"), str)
            or not item["quote"].strip()
            or len(item["quote"]) > 2_000
        ):
            raise PaperMapProviderError(
                "Paper map answer returned an invalid citation.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_citation",
                retryable=True,
            )
        parsed_citations.append(PaperMapCitation(item["block_id"], item["quote"].strip()))
    parsed_details: list[PaperMapDetail] = []
    seen_keys: set[str] = set()
    for item in details[:12]:
        if not isinstance(item, dict):
            continue
        key, concept_type, label, body, indexes = (
            item.get("key"),
            item.get("concept_type"),
            item.get("label"),
            item.get("body"),
            item.get("citation_indexes"),
        )
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 64
            or key in seen_keys
            or concept_type not in PAPER_MAP_CONCEPT_TYPES
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 120
            or not isinstance(body, str)
            or not body.strip()
            or len(body) > 4_000
            or not isinstance(indexes, list)
            or not all(
                isinstance(index, int) and 0 <= index < len(parsed_citations) for index in indexes
            )
        ):
            continue
        seen_keys.add(key)
        parsed_details.append(
            PaperMapDetail(key, concept_type, label.strip(), body.strip(), tuple(indexes[:6]))
        )
    allowed_keys = {"question", *seen_keys}
    parsed_relations: list[PaperMapRelation] = []
    for item in relations[:16]:
        if not isinstance(item, dict):
            continue
        source, target, edge_type = (
            item.get("source_key"),
            item.get("target_key"),
            item.get("edge_type"),
        )
        if (
            source in allowed_keys
            and target in allowed_keys
            and source != target
            and edge_type in PAPER_MAP_EDGE_TYPES - {"HAS_QUESTION"}
        ):
            parsed_relations.append(PaperMapRelation(source, target, edge_type))
    if status == "SUPPORTED" and not parsed_citations:
        status = "NOT_STATED"
    return PaperMapAnswerResult(
        status,
        answer.strip(),
        tuple(parsed_citations),
        tuple(parsed_details),
        tuple(parsed_relations),
    )


def _decode_structured_content(value: object) -> object | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    content = value.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        if start < 0:
            return None
        try:
            parsed, _ = json.JSONDecoder().raw_decode(content[start:])
            return parsed
        except json.JSONDecodeError:
            return None


@dataclass(frozen=True, slots=True)
class GLMChatPaperMapGenerator:
    api_key: str = field(repr=False)
    model: str
    api_base: str = ""
    timeout_seconds: int = 120
    provider: str = "glm"

    def _endpoint(self) -> str:
        base = self.api_base.strip().rstrip("/") or "https://open.bigmodel.cn/api/paas/v4"
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def _call(
        self,
        user_payload: dict[str, Any],
        *,
        system_prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> tuple[object, dict[str, Any], int, str]:
        started = time.monotonic()
        total_usage: dict[str, int] = {}
        # Keep GLM's existing bounded recovery. Personal DeepSeek JSON work is explicitly
        # non-thinking and never inherits the GLM high-effort first call or an extra attempt.
        thinking_modes = (False,) if self.provider == "deepseek" else (True, False)
        for thinking_enabled in thinking_modes:
            request_id = str(uuid.uuid4())
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": f"{system_prompt}\nJSON schema:\n{json.dumps(schema)}",
                    },
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
                "max_tokens": max_tokens,
                "stream": False,
            }
            if thinking_enabled:
                payload["reasoning_effort"] = "high"
            request = urllib.request.Request(
                self._endpoint(),
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Request-Id": request_id,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    response_payload = json.load(response)
                    provider_id = sanitize_provider_identifier(
                        response.headers.get("x-request-id"),
                        limit=255,
                    )
            except urllib.error.HTTPError as error:
                raise _http_error(error, request_id) from error
            except (urllib.error.URLError, TimeoutError) as error:
                raise PaperMapProviderError(
                    f"Paper map provider network request failed: {type(error).__name__}",
                    error_type="TIMEOUT" if isinstance(error, TimeoutError) else "NETWORK",
                    error_code=type(error).__name__,
                    retryable=True,
                ) from error
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise PaperMapProviderError(
                    "Paper map provider returned invalid JSON.",
                    error_type="INVALID_RESPONSE",
                    error_code="invalid_json",
                    retryable=True,
                ) from error
            usage = response_payload.get("usage", {})
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int):
                        total_usage[key] = total_usage.get(key, 0) + value
            try:
                content = response_payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = None
            structured = _decode_structured_content(content)
            if structured is not None:
                return (
                    structured,
                    total_usage,
                    round((time.monotonic() - started) * 1000),
                    provider_id
                    or sanitize_provider_identifier(
                        response_payload.get("id"),
                        limit=255,
                    )
                    or request_id,
                )
        raise PaperMapProviderError(
            "Paper map provider returned no structured output after bounded recovery.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_output",
            retryable=True,
        )

    def plan(self, request: PaperMapPlanRequest, *, system_prompt: str) -> PaperMapPlanResult:
        structured, usage, latency, request_id = self._call(
            _plan_payload(request),
            system_prompt=system_prompt,
            schema=_plan_schema(),
            max_tokens=request.max_output_tokens,
        )
        result = _parse_plan(
            structured,
            {candidate.question_key for candidate in request.question_candidates},
        )
        return PaperMapPlanResult(
            result.primary_type,
            result.secondary_types,
            result.confidence,
            result.questions,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            latency,
            request_id,
        )

    def answer(self, request: PaperMapAnswerRequest, *, system_prompt: str) -> PaperMapAnswerResult:
        structured, usage, latency, request_id = self._call(
            _answer_payload(request),
            system_prompt=system_prompt,
            schema=_answer_schema(),
            max_tokens=request.max_output_tokens,
        )
        result = _parse_answer(structured)
        return PaperMapAnswerResult(
            result.answer_status,
            result.answer,
            result.citations,
            result.details,
            result.relations,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            latency,
            request_id,
        )


@dataclass(frozen=True, slots=True)
class FakePaperMapGenerator:
    provider: str = "fake"
    model: str = "fake-paper-map-v1"

    def plan(self, request: PaperMapPlanRequest, *, system_prompt: str) -> PaperMapPlanResult:
        del system_prompt
        keys = tuple(candidate.question_key for candidate in request.question_candidates[:8])
        block_ids = tuple(item.block_id for item in request.context if item.block_id)[:2]
        return PaperMapPlanResult(
            "METHOD",
            ("EMPIRICAL",),
            0.91,
            tuple(
                PaperMapQuestionSelection(key, f"Relevant to {request.paper_title}.", block_ids)
                for key in keys
            ),
            120,
            40,
            2,
            "fake-map-plan",
        )

    def answer(self, request: PaperMapAnswerRequest, *, system_prompt: str) -> PaperMapAnswerResult:
        del system_prompt
        item = next((value for value in request.context if value.block_id and value.content), None)
        citations = (PaperMapCitation(item.block_id or "", item.content[:160]),) if item else ()
        status = "SUPPORTED" if citations else "NOT_STATED"
        return PaperMapAnswerResult(
            status,
            f"基于论文内容回答：{request.question}",
            citations,
            (
                PaperMapDetail(
                    "key-point",
                    "CLAIM",
                    "关键结论",
                    "这是可验证的示例细节。",
                    (0,) if citations else (),
                ),
            ),
            (),
            80,
            30,
            1,
            "fake-map-answer",
        )


def build_paper_map_generator(
    *, api_key: str, model: str, api_base: str, provider: str, timeout_seconds: int
) -> PaperMapGenerator:
    normalized = provider.strip().lower()
    if normalized not in {"glm", "deepseek"}:
        raise ValueError("Unsupported paper-map provider.")
    if normalized == "deepseek" and not api_base.strip():
        raise ValueError("DeepSeek requires an explicit API base.")
    return GLMChatPaperMapGenerator(api_key, model, api_base, timeout_seconds, normalized)
