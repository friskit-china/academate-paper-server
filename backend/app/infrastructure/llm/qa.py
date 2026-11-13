from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

from app.domain.qa import (
    QACitation,
    QARequest,
    QAResult,
    QAUsage,
    QuestionAnswerer,
    qa_user_payload,
)
from app.domain.tasks import redact_task_error_message
from app.infrastructure.llm.provider_http import (
    provider_http_error_message,
    read_provider_http_error,
    sanitize_provider_identifier,
)


class QAProviderError(RuntimeError):
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
            "answer": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string"},
                        "target": {"type": "string", "enum": ["SOURCE", "TRANSLATION"]},
                        "quote": {"type": "string"},
                        "translation_id": {"type": ["string", "null"]},
                    },
                    "required": ["block_id", "target", "quote"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["answer", "citations"],
        "additionalProperties": False,
    }


def _prompt(request: QARequest) -> str:
    return json.dumps(qa_user_payload(request), ensure_ascii=False)


def _retry_after(headers: Message | None) -> int | None:
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
        return min(300, max(0, int(value))) if value is not None else None
    except ValueError:
        return None


def _http_error(error: urllib.error.HTTPError, request_id: str) -> QAProviderError:
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
    return QAProviderError(
        provider_http_error_message("Q&A", diagnostic),
        error_type=error_type,
        error_code=diagnostic.error_code,
        retryable=retryable,
        retry_after_seconds=_retry_after(error.headers),
        provider_request_id=diagnostic.request_id,
    )


def _parse(payload: object) -> tuple[str, tuple[QACitation, ...]]:
    if not isinstance(payload, dict):
        raise QAProviderError(
            "Q&A provider returned an invalid response shape.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_schema",
        )
    answer = payload.get("answer")
    citations = payload.get("citations")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 40_000:
        raise QAProviderError(
            "Q&A provider returned an invalid answer.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_answer",
        )
    if not isinstance(citations, list) or len(citations) > 32:
        raise QAProviderError(
            "Q&A provider returned invalid citations.",
            error_type="INVALID_RESPONSE",
            error_code="invalid_citations",
        )
    parsed: list[QACitation] = []
    for citation in citations:
        if not isinstance(citation, dict):
            raise QAProviderError(
                "Q&A provider returned an invalid citation.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_citation",
            )
        block_id = citation.get("block_id")
        target = citation.get("target")
        quote = citation.get("quote")
        translation_id = citation.get("translation_id")
        if (
            not isinstance(block_id, str)
            or target not in {"SOURCE", "TRANSLATION"}
            or not isinstance(quote, str)
            or not quote.strip()
            or len(quote) > 2_000
            or (translation_id is not None and not isinstance(translation_id, str))
        ):
            raise QAProviderError(
                "Q&A provider returned an invalid citation.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_citation",
            )
        parsed.append(QACitation(block_id, target, quote, translation_id))
    return answer.strip(), tuple(parsed)


@dataclass(frozen=True, slots=True)
class GLMChatQuestionAnswerer:
    api_key: str = field(repr=False)
    model: str
    api_base: str = ""
    timeout_seconds: int = 90
    provider: str = "glm"

    def _endpoint(self) -> str:
        base = self.api_base.strip().rstrip("/") or "https://open.bigmodel.cn/api/paas/v4"
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def answer(self, request: QARequest, *, system_prompt: str) -> QAResult:
        client_request_id = str(uuid.uuid4())
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"{system_prompt}\nJSON schema:\n{json.dumps(_schema())}",
                },
                {"role": "user", "content": _prompt(request)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
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
            raise QAProviderError(
                f"Q&A provider network request failed: {type(error).__name__}",
                error_type="TIMEOUT" if isinstance(error, TimeoutError) else "NETWORK",
                error_code=type(error).__name__,
                retryable=True,
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise QAProviderError(
                "Q&A provider returned invalid JSON.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_json",
            ) from error
        try:
            output = response_payload["choices"][0]["message"]["content"]
            structured = json.loads(output)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise QAProviderError(
                "Q&A provider returned no structured output.",
                error_type="INVALID_RESPONSE",
                error_code="invalid_output",
            ) from error
        answer, citations = _parse(structured)
        usage = response_payload.get("usage", {})
        return QAResult(
            answer=answer,
            citations=citations,
            usage=QAUsage(
                input_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
                output_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
            ),
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
class FakeQuestionAnswerer:
    model: str = "fake-qa-v1"
    provider: str = "fake"

    def answer(self, request: QARequest, *, system_prompt: str) -> QAResult:
        del system_prompt
        citation = next(
            (
                QACitation(item.block_id, item.target, item.content[:160], item.translation_id)
                for item in request.context
                if item.block_id and item.target in {"SOURCE", "TRANSLATION"} and item.content
            ),
            None,
        )
        return QAResult(
            answer=f"Answer: {request.question}",
            citations=(citation,) if citation else (),
            usage=QAUsage(input_tokens=len(request.context), output_tokens=4),
            latency_ms=1,
            provider_request_id="fake-qa-request",
        )


def build_question_answerer(
    *, api_key: str, model: str, api_base: str, provider: str, timeout_seconds: int
) -> QuestionAnswerer:
    normalized = provider.strip().lower()
    if normalized not in {"glm", "deepseek"}:
        raise ValueError("Unsupported question-answering provider.")
    if normalized == "deepseek" and not api_base.strip():
        raise ValueError("DeepSeek requires an explicit API base.")
    return GLMChatQuestionAnswerer(api_key, model, api_base, timeout_seconds, normalized)
