from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class QAContextItem:
    kind: str
    content: str
    block_id: str | None = None
    target: str | None = None
    translation_id: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class QARequest:
    question_id: str
    question: str
    paper_id: str
    paper_title: str
    target: str
    block_id: str | None
    context: tuple[QAContextItem, ...]
    max_output_tokens: int


def qa_user_payload(request: QARequest) -> dict[str, Any]:
    """Return the canonical, provider-bound user payload for audit and transport."""
    return {
        "question": request.question,
        "question_id": request.question_id,
        "paper_id": request.paper_id,
        "paper_title": request.paper_title,
        "target": request.target,
        "target_block_id": request.block_id,
        "contexts": [
            {
                "kind": item.kind,
                "content": item.content,
                "block_id": item.block_id,
                "target": item.target,
                "translation_id": item.translation_id,
                "truncated": item.truncated,
            }
            for item in request.context
        ],
    }


@dataclass(frozen=True, slots=True)
class QACitation:
    block_id: str
    target: str
    quote: str
    translation_id: str | None = None


@dataclass(frozen=True, slots=True)
class QAUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class QAResult:
    answer: str
    citations: tuple[QACitation, ...]
    usage: QAUsage = QAUsage()
    latency_ms: int | None = None
    provider_request_id: str | None = None


class QuestionAnswerer(Protocol):
    provider: str
    model: str

    def answer(self, request: QARequest, *, system_prompt: str) -> QAResult: ...
