from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

PAPER_TYPES = {
    "METHOD",
    "EMPIRICAL",
    "THEORY",
    "SYSTEM",
    "DATASET_BENCHMARK",
    "SURVEY_REVIEW",
    "QUALITATIVE_CASE",
    "REPLICATION_NEGATIVE",
    "POSITION_CONCEPTUAL",
    "APPLICATION",
}
PAPER_MAP_NODE_TYPES = {"PAPER", "QUESTION", "CONCEPT"}
PAPER_MAP_CONCEPT_TYPES = {
    "CLAIM",
    "METHOD",
    "EVIDENCE",
    "DATASET",
    "METRIC",
    "ARTIFACT",
    "LIMITATION",
    "FUTURE",
    "SETTING",
    "IDEA",
    "MOTIVATION",
    "RESULT",
    "ASSUMPTION",
    "CONTRIBUTION",
}
PAPER_MAP_EDGE_TYPES = {
    "HAS_QUESTION",
    "HAS_CONCEPT",
    "SUPPORTS",
    "CONTRADICTS",
    "EVALUATES",
    "USES",
    "DEPENDS_ON",
    "COMPARES_WITH",
    "LIMITS",
    "EXTENDS",
    "RELATED_TO",
}
PAPER_MAP_ANSWER_STATUSES = {
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "NOT_STATED",
    "NOT_APPLICABLE",
    "WEB_REQUIRED",
    "INFERENCE",
}


@dataclass(frozen=True, slots=True)
class PaperMapContextItem:
    kind: str
    content: str
    block_id: str | None = None
    content_hash: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class PaperMapQuestionCandidate:
    question_key: str
    question_text: str
    dimension: str
    paper_types: tuple[str, ...] = ()
    user_defined: bool = False


@dataclass(frozen=True, slots=True)
class PaperMapPlanRequest:
    paper_id: str
    paper_title: str
    paper_abstract: str
    question_candidates: tuple[PaperMapQuestionCandidate, ...]
    context: tuple[PaperMapContextItem, ...]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class PaperMapQuestionSelection:
    question_key: str
    rationale: str
    evidence_block_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PaperMapPlanResult:
    primary_type: str
    secondary_types: tuple[str, ...]
    confidence: float
    questions: tuple[PaperMapQuestionSelection, ...]
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaperMapAnswerRequest:
    paper_id: str
    paper_title: str
    question_id: str
    question_key: str
    question: str
    context: tuple[PaperMapContextItem, ...]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class PaperMapCitation:
    block_id: str
    quote: str


@dataclass(frozen=True, slots=True)
class PaperMapDetail:
    key: str
    concept_type: str
    label: str
    body: str
    citation_indexes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PaperMapRelation:
    source_key: str
    target_key: str
    edge_type: str


@dataclass(frozen=True, slots=True)
class PaperMapAnswerResult:
    answer_status: str
    answer: str
    citations: tuple[PaperMapCitation, ...]
    details: tuple[PaperMapDetail, ...] = ()
    relations: tuple[PaperMapRelation, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    provider_request_id: str | None = None


class PaperMapGenerator(Protocol):
    provider: str
    model: str

    def plan(self, request: PaperMapPlanRequest, *, system_prompt: str) -> PaperMapPlanResult: ...

    def answer(
        self,
        request: PaperMapAnswerRequest,
        *,
        system_prompt: str,
    ) -> PaperMapAnswerResult: ...


def paper_map_context_payload(items: tuple[PaperMapContextItem, ...]) -> list[dict[str, Any]]:
    return [
        {
            "kind": item.kind,
            "content": item.content,
            "block_id": item.block_id,
            "content_hash": item.content_hash,
            "truncated": item.truncated,
        }
        for item in items
    ]
