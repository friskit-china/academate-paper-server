from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.llm_credentials import request_llm_task_credential
from app.application.access import load_workspace_paper
from app.application.paper_maps import (
    create_followup_question,
    current_paper_map,
    schedule_paper_map,
    schedule_question_answer,
)
from app.domain.auth import Permission, RequestPrincipal
from app.domain.enums import PaperMapQuestionStatus
from app.domain.paper_map import PAPER_MAP_CONCEPT_TYPES
from app.infrastructure.db.models import (
    PaperMapEdgeModel,
    PaperMapModel,
    PaperMapNodeModel,
    PaperMapQuestionModel,
)

router = APIRouter(
    tags=["paper-maps"],
    dependencies=[Depends(resolve_request_principal)],
)


class PaperMapQuestionResponse(BaseModel):
    id: str
    question_key: str
    question_text: str
    anchor_node_id: str | None
    is_initial: bool
    rationale: str
    evidence_block_ids: list[str]
    ordinal: int
    status: str
    answer_generation: int
    answer_status: str | None
    answer_text: str | None
    citations: list[dict[str, Any]]
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    error_type: str | None
    error_code: str | None
    error_message: str | None
    updated_at: datetime


class PaperMapNodeResponse(BaseModel):
    id: str
    question_id: str | None
    node_key: str
    node_type: str
    concept_type: str | None
    label: str
    body: str
    status: str | None
    evidence: list[dict[str, Any]]
    ordinal: int


class PaperMapEdgeResponse(BaseModel):
    id: str
    source_node_id: str
    target_node_id: str
    edge_type: str
    label: str | None
    is_tree: bool


class PaperMapResponse(BaseModel):
    id: str
    paper_id: str
    generation: int
    status: str
    content_hash: str
    question_library_version: str
    primary_type: str | None
    secondary_types: list[str]
    type_confidence: float | None
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    error_type: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    questions: list[PaperMapQuestionResponse]
    nodes: list[PaperMapNodeResponse]
    edges: list[PaperMapEdgeResponse]


class PaperMapEnvelopeResponse(BaseModel):
    configured: bool
    provider: str | None
    model: str | None
    paper_map: PaperMapResponse | None


class PaperMapTaskResponse(BaseModel):
    map_id: str
    task_id: str
    generation: int
    status: str


class PaperMapAnswerTaskResponse(BaseModel):
    map_id: str
    question_id: str
    task_id: str
    generation: int
    status: str


class PaperMapFollowupRequest(BaseModel):
    question: str


class PaperMapConceptCreateRequest(BaseModel):
    concept_type: str
    label: str
    body: str = ""


class PaperMapEdgeCreateRequest(BaseModel):
    source_node_id: str
    target_node_id: str
    label: str = ""


class PaperMapEdgeUpdateRequest(BaseModel):
    label: str


class PaperMapMutationResponse(BaseModel):
    node: PaperMapNodeResponse | None = None
    edge: PaperMapEdgeResponse | None = None


def _map_query(map_id: str, workspace_id: str):
    return (
        select(PaperMapModel)
        .where(
            PaperMapModel.id == map_id,
            PaperMapModel.workspace_id == workspace_id,
        )
        .options(
            selectinload(PaperMapModel.questions),
            selectinload(PaperMapModel.nodes),
            selectinload(PaperMapModel.edges),
        )
    )


def _serialize_question(question: PaperMapQuestionModel) -> PaperMapQuestionResponse:
    return PaperMapQuestionResponse(
        id=question.id,
        question_key=question.question_key,
        question_text=question.question_text,
        anchor_node_id=question.anchor_node_id,
        is_initial=question.is_initial,
        rationale=question.rationale,
        evidence_block_ids=list(question.evidence_block_ids_json or []),
        ordinal=question.ordinal,
        status=question.status,
        answer_generation=question.answer_generation,
        answer_status=question.answer_status,
        answer_text=question.answer_text,
        citations=list(question.citations_json or []),
        provider=question.provider,
        model=question.model,
        input_tokens=question.input_tokens,
        output_tokens=question.output_tokens,
        latency_ms=question.latency_ms,
        error_type=question.error_type,
        error_code=question.error_code,
        error_message=question.error_message,
        updated_at=question.updated_at,
    )


def _serialize_node(node: PaperMapNodeModel) -> PaperMapNodeResponse:
    return PaperMapNodeResponse(
        id=node.id,
        question_id=node.question_id,
        node_key=node.node_key,
        node_type=node.node_type,
        concept_type=node.concept_type,
        label=node.label,
        body=node.body,
        status=node.status,
        evidence=list(node.evidence_json or []),
        ordinal=node.ordinal,
    )


def _serialize_edge(edge: PaperMapEdgeModel) -> PaperMapEdgeResponse:
    return PaperMapEdgeResponse(
        id=edge.id,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        edge_type=edge.edge_type,
        label=edge.label,
        is_tree=edge.is_tree,
    )


def _current_map_for_node(
    session,
    node_id: str,
    principal: RequestPrincipal,
) -> tuple[PaperMapNodeModel, PaperMapModel]:
    row = session.execute(
        select(PaperMapNodeModel, PaperMapModel)
        .join(PaperMapModel, PaperMapModel.id == PaperMapNodeModel.map_id)
        .where(
            PaperMapNodeModel.id == node_id,
            PaperMapModel.workspace_id == principal.active_workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Paper map node not found.")
    node, paper_map = row
    if load_workspace_paper(session, principal, paper_map.paper_id) is None:
        raise HTTPException(status_code=404, detail="Paper map not found.")
    current = current_paper_map(
        session,
        paper_map.paper_id,
        workspace_id=principal.active_workspace_id,
    )
    if current is None or current.id != paper_map.id:
        raise HTTPException(status_code=409, detail="This node belongs to an older map.")
    return node, paper_map


def _current_map_for_edge(
    session,
    edge_id: str,
    principal: RequestPrincipal,
) -> tuple[PaperMapEdgeModel, PaperMapModel]:
    row = session.execute(
        select(PaperMapEdgeModel, PaperMapModel)
        .join(PaperMapModel, PaperMapModel.id == PaperMapEdgeModel.map_id)
        .where(
            PaperMapEdgeModel.id == edge_id,
            PaperMapModel.workspace_id == principal.active_workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Paper map edge not found.")
    edge, paper_map = row
    if load_workspace_paper(session, principal, paper_map.paper_id) is None:
        raise HTTPException(status_code=404, detail="Paper map not found.")
    current = current_paper_map(
        session,
        paper_map.paper_id,
        workspace_id=principal.active_workspace_id,
    )
    if current is None or current.id != paper_map.id:
        raise HTTPException(status_code=409, detail="This edge belongs to an older map.")
    return edge, paper_map


def _serialize_map(paper_map: PaperMapModel) -> PaperMapResponse:
    return PaperMapResponse(
        id=paper_map.id,
        paper_id=paper_map.paper_id,
        generation=paper_map.generation,
        status=paper_map.status,
        content_hash=paper_map.content_hash,
        question_library_version=paper_map.question_library_version,
        primary_type=paper_map.primary_type,
        secondary_types=list(paper_map.secondary_types_json or []),
        type_confidence=paper_map.type_confidence,
        provider=paper_map.provider,
        model=paper_map.model,
        input_tokens=paper_map.input_tokens,
        output_tokens=paper_map.output_tokens,
        latency_ms=paper_map.latency_ms,
        error_type=paper_map.error_type,
        error_code=paper_map.error_code,
        error_message=paper_map.error_message,
        created_at=paper_map.created_at,
        updated_at=paper_map.updated_at,
        completed_at=paper_map.completed_at,
        questions=[
            _serialize_question(question)
            for question in sorted(paper_map.questions, key=lambda value: value.ordinal)
        ],
        nodes=[_serialize_node(node) for node in paper_map.nodes],
        edges=[_serialize_edge(edge) for edge in paper_map.edges],
    )


@router.get(
    "/papers/{paper_id}/visual-map",
    response_model=PaperMapEnvelopeResponse,
)
def get_visual_map(paper_id: str, request: Request) -> PaperMapEnvelopeResponse:
    principal = require_request_permission(request, Permission.PAPER_MAP_READ)
    with request.app.state.session_factory() as session:
        if load_workspace_paper(session, principal, paper_id) is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        current = current_paper_map(
            session,
            paper_id,
            workspace_id=principal.active_workspace_id,
        )
        paper_map = (
            session.scalar(_map_query(current.id, principal.active_workspace_id))
            if current is not None
            else None
        )
        llm_credential = request_llm_task_credential(
            request,
            user_id=principal.user_id,
            required=False,
        )
        return PaperMapEnvelopeResponse(
            configured=llm_credential is not None,
            provider=llm_credential.provider if llm_credential is not None else None,
            model=llm_credential.model if llm_credential is not None else None,
            paper_map=_serialize_map(paper_map) if paper_map is not None else None,
        )


@router.post(
    "/papers/{paper_id}/visual-map",
    response_model=PaperMapTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_visual_map(paper_id: str, request: Request) -> PaperMapTaskResponse:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    with request.app.state.session_factory() as session:
        workspace_paper = load_workspace_paper(session, principal, paper_id)
        if workspace_paper is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        settings = request.app.state.settings
        llm_credential = request_llm_task_credential(request, user_id=principal.user_id)
        assert llm_credential is not None
        paper_map, task = schedule_paper_map(
            session,
            paper=workspace_paper.paper,
            workspace_id=principal.active_workspace_id,
            requested_by_user_id=principal.user_id,
            provider=llm_credential.provider,
            model=llm_credential.model,
            max_attempts=settings.paper_map_max_attempts,
            credential_id=llm_credential.credential_id,
        )
        session.commit()
        return PaperMapTaskResponse(
            map_id=paper_map.id,
            task_id=task.id,
            generation=paper_map.generation,
            status=paper_map.status,
        )


@router.post(
    "/paper-map-questions/{question_id}/answer",
    response_model=PaperMapAnswerTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_question_answer(
    question_id: str,
    request: Request,
) -> PaperMapAnswerTaskResponse:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    with request.app.state.session_factory() as session:
        row = session.execute(
            select(PaperMapQuestionModel, PaperMapModel)
            .join(PaperMapModel, PaperMapModel.id == PaperMapQuestionModel.map_id)
            .where(
                PaperMapQuestionModel.id == question_id,
                PaperMapModel.workspace_id == principal.active_workspace_id,
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Paper map question not found.")
        question, paper_map = row
        if load_workspace_paper(session, principal, paper_map.paper_id) is None:
            raise HTTPException(status_code=404, detail="Paper map not found.")
        current = current_paper_map(
            session,
            paper_map.paper_id,
            workspace_id=principal.active_workspace_id,
        )
        if current is None or current.id != paper_map.id:
            raise HTTPException(
                status_code=409,
                detail="This question belongs to an older paper map generation.",
            )
        if question.status in {
            PaperMapQuestionStatus.QUEUED,
            PaperMapQuestionStatus.RUNNING,
        }:
            raise HTTPException(status_code=409, detail="This answer is already in progress.")
        settings = request.app.state.settings
        llm_credential = request_llm_task_credential(request, user_id=principal.user_id)
        assert llm_credential is not None
        task = schedule_question_answer(
            session,
            question=question,
            workspace_id=principal.active_workspace_id,
            requested_by_user_id=principal.user_id,
            max_attempts=settings.paper_map_max_attempts,
            credential_id=llm_credential.credential_id,
        )
        session.commit()
        return PaperMapAnswerTaskResponse(
            map_id=paper_map.id,
            question_id=question.id,
            task_id=task.id,
            generation=question.answer_generation,
            status=question.status,
        )


@router.post(
    "/paper-map-nodes/{node_id}/follow-ups",
    response_model=PaperMapAnswerTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_node_followup(
    node_id: str,
    payload: PaperMapFollowupRequest,
    request: Request,
) -> PaperMapAnswerTaskResponse:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    with request.app.state.session_factory() as session:
        anchor, paper_map = _current_map_for_node(session, node_id, principal)
        settings = request.app.state.settings
        llm_credential = request_llm_task_credential(request, user_id=principal.user_id)
        assert llm_credential is not None
        try:
            question, _node, task = create_followup_question(
                session,
                anchor=anchor,
                workspace_id=principal.active_workspace_id,
                requested_by_user_id=principal.user_id,
                question_text=payload.question,
                max_attempts=settings.paper_map_max_attempts,
                credential_id=llm_credential.credential_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        session.commit()
        return PaperMapAnswerTaskResponse(
            map_id=paper_map.id,
            question_id=question.id,
            task_id=task.id,
            generation=question.answer_generation,
            status=question.status,
        )


@router.post(
    "/paper-map-nodes/{node_id}/concepts",
    response_model=PaperMapMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_node_concept(
    node_id: str,
    payload: PaperMapConceptCreateRequest,
    request: Request,
) -> PaperMapMutationResponse:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    concept_type = payload.concept_type.strip().upper()
    label = payload.label.strip()
    body = payload.body.strip()
    if concept_type not in PAPER_MAP_CONCEPT_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported paper map concept type.")
    if not 1 <= len(label) <= 240:
        raise HTTPException(status_code=422, detail="Concept titles must contain 1–240 characters.")
    if len(body) > 8_000:
        raise HTTPException(status_code=422, detail="Concept bodies cannot exceed 8000 characters.")
    with request.app.state.session_factory() as session:
        anchor, paper_map = _current_map_for_node(session, node_id, principal)
        ordinal = int(
            session.scalar(
                select(func.coalesce(func.max(PaperMapNodeModel.ordinal), 0)).where(
                    PaperMapNodeModel.map_id == paper_map.id
                )
            )
            or 0
        ) + 1
        import uuid

        node = PaperMapNodeModel(
            map_id=paper_map.id,
            node_key=f"concept:manual:{uuid.uuid4().hex}",
            node_type="CONCEPT",
            concept_type=concept_type,
            label=label,
            body=body,
            status="USER_ADDED",
            ordinal=ordinal,
        )
        session.add(node)
        session.flush()
        edge = PaperMapEdgeModel(
            map_id=paper_map.id,
            source_node_id=anchor.id,
            target_node_id=node.id,
            edge_type="HAS_CONCEPT",
            is_tree=True,
        )
        session.add(edge)
        session.commit()
        session.refresh(node)
        session.refresh(edge)
        return PaperMapMutationResponse(node=_serialize_node(node), edge=_serialize_edge(edge))


@router.post(
    "/paper-maps/{map_id}/edges",
    response_model=PaperMapMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_map_edge(
    map_id: str,
    payload: PaperMapEdgeCreateRequest,
    request: Request,
) -> PaperMapMutationResponse:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    if payload.source_node_id == payload.target_node_id:
        raise HTTPException(status_code=422, detail="A node cannot connect to itself.")
    if len(payload.label.strip()) > 160:
        raise HTTPException(status_code=422, detail="Edge labels cannot exceed 160 characters.")
    with request.app.state.session_factory() as session:
        paper_map = session.scalar(
            select(PaperMapModel).where(
                PaperMapModel.id == map_id,
                PaperMapModel.workspace_id == principal.active_workspace_id,
            )
        )
        if paper_map is None:
            raise HTTPException(status_code=404, detail="Paper map not found.")
        if load_workspace_paper(session, principal, paper_map.paper_id) is None:
            raise HTTPException(status_code=404, detail="Paper map not found.")
        current = current_paper_map(
            session,
            paper_map.paper_id,
            workspace_id=principal.active_workspace_id,
        )
        if current is None or current.id != paper_map.id:
            raise HTTPException(status_code=409, detail="This is an older paper map.")
        source = session.scalar(
            select(PaperMapNodeModel).where(
                PaperMapNodeModel.id == payload.source_node_id,
                PaperMapNodeModel.map_id == map_id,
            )
        )
        target = session.scalar(
            select(PaperMapNodeModel).where(
                PaperMapNodeModel.id == payload.target_node_id,
                PaperMapNodeModel.map_id == map_id,
            )
        )
        if source is None or target is None or source.map_id != map_id or target.map_id != map_id:
            raise HTTPException(
                status_code=422,
                detail="Both edge endpoints must belong to this map.",
            )
        duplicate = session.scalar(
            select(PaperMapEdgeModel).where(
                PaperMapEdgeModel.map_id == map_id,
                PaperMapEdgeModel.source_node_id == source.id,
                PaperMapEdgeModel.target_node_id == target.id,
                PaperMapEdgeModel.edge_type == "RELATED_TO",
            )
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="This manual edge already exists.")
        edge = PaperMapEdgeModel(
            map_id=map_id,
            source_node_id=source.id,
            target_node_id=target.id,
            edge_type="RELATED_TO",
            label=payload.label.strip(),
            is_tree=False,
        )
        session.add(edge)
        session.commit()
        session.refresh(edge)
        return PaperMapMutationResponse(edge=_serialize_edge(edge))


@router.patch(
    "/paper-map-edges/{edge_id}",
    response_model=PaperMapMutationResponse,
)
def update_map_edge(
    edge_id: str,
    payload: PaperMapEdgeUpdateRequest,
    request: Request,
) -> PaperMapMutationResponse:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    label = payload.label.strip()
    if len(label) > 160:
        raise HTTPException(status_code=422, detail="Edge labels cannot exceed 160 characters.")
    with request.app.state.session_factory() as session:
        edge, _paper_map = _current_map_for_edge(session, edge_id, principal)
        edge.label = label
        session.commit()
        session.refresh(edge)
        return PaperMapMutationResponse(edge=_serialize_edge(edge))


@router.delete("/paper-map-edges/{edge_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_map_edge(edge_id: str, request: Request) -> None:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    with request.app.state.session_factory() as session:
        edge, _paper_map = _current_map_for_edge(session, edge_id, principal)
        session.delete(edge)
        session.commit()


@router.delete("/paper-map-nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_map_node(node_id: str, request: Request) -> None:
    principal = require_request_permission(request, Permission.PAPER_MAP_WRITE)
    with request.app.state.session_factory() as session:
        node, paper_map = _current_map_for_node(session, node_id, principal)
        if node.node_type == "QUESTION" and node.question_id:
            question = session.scalar(
                select(PaperMapQuestionModel).where(
                    PaperMapQuestionModel.id == node.question_id,
                    PaperMapQuestionModel.map_id == paper_map.id,
                )
            )
            if question is not None:
                question_nodes = session.scalars(
                    select(PaperMapNodeModel).where(
                        PaperMapNodeModel.question_id == question.id,
                        PaperMapNodeModel.map_id == paper_map.id,
                    )
                ).all()
                for question_node in question_nodes:
                    session.delete(question_node)
                session.flush()
                session.delete(question)
            else:
                session.delete(node)
        else:
            session.delete(node)
        session.commit()
