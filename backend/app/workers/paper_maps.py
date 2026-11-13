from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.paper_maps import (
    PAPER_MAP_ANSWER_PROMPT_VERSION,
    PAPER_MAP_ANSWER_SYSTEM_PROMPT,
    PAPER_MAP_PLAN_PROMPT_VERSION,
    PAPER_MAP_PLAN_SYSTEM_PROMPT,
    answer_context_audit,
    build_answer_request,
    build_plan_request,
    paper_content_hash,
    paper_map_answer_prompt_hash,
    plan_context_audit,
    schedule_question_answer,
)
from app.domain.citations import resolve_canonical_quote
from app.domain.enums import PaperMapQuestionStatus, PaperMapStatus
from app.domain.paper_map import PaperMapGenerator
from app.domain.ports import QueuedTask
from app.domain.tasks import TaskScope, TaskVisibility, redact_task_error_message
from app.infrastructure.db.models import (
    ContentBlockModel,
    LibraryItemModel,
    PaperMapEdgeModel,
    PaperMapModel,
    PaperMapNodeModel,
    PaperMapQuestionModel,
    PaperModel,
    UserModel,
    WorkspaceMembershipModel,
    now_utc,
)
from app.infrastructure.llm.paper_map import PaperMapProviderError


class PaperMapTaskError(RuntimeError):
    pass


def _map_task_identity(task: QueuedTask) -> tuple[str, int]:
    map_id = str(task.payload.get("map_id", ""))
    generation = int(task.payload.get("generation", 0))
    if not map_id or generation < 1:
        raise PaperMapTaskError("Paper map task payload is incomplete.")
    return map_id, generation


def _workspace_task_scope(task: QueuedTask) -> tuple[str, str]:
    if task.visibility is not TaskVisibility.WORKSPACE_DERIVED:
        raise PaperMapTaskError("Paper map tasks require workspace-derived visibility.")
    if task.workspace_id is None or task.requested_by_user_id is None:
        raise PaperMapTaskError("Paper map task ownership is incomplete.")
    expected = TaskScope.workspace_derived(
        workspace_id=task.workspace_id,
        requested_by_user_id=task.requested_by_user_id,
        credential_id=task.credential_id,
    )
    if task.scope_key != expected.scope_key:
        raise PaperMapTaskError("Paper map task scope is inconsistent.")
    return task.workspace_id, task.requested_by_user_id


def _scoped_paper(
    session: Session,
    *,
    paper_id: str,
    workspace_id: str,
) -> PaperModel | None:
    return session.scalar(
        select(PaperModel)
        .join(LibraryItemModel, LibraryItemModel.paper_id == PaperModel.id)
        .where(
            PaperModel.id == paper_id,
            LibraryItemModel.workspace_id == workspace_id,
        )
    )


def _requester_is_workspace_member(
    session: Session,
    *,
    workspace_id: str,
    requested_by_user_id: str,
) -> bool:
    return (
        session.scalar(
            select(WorkspaceMembershipModel.user_id)
            .join(UserModel, UserModel.id == WorkspaceMembershipModel.user_id)
            .where(
                WorkspaceMembershipModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.user_id == requested_by_user_id,
                UserModel.status == "ACTIVE",
            )
        )
        is not None
    )


def plan_paper_map(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    generator: PaperMapGenerator | None,
    *,
    context_token_budget: int,
    max_output_tokens: int,
    answer_max_attempts: int,
) -> dict[str, Any]:
    workspace_id, requested_by_user_id = _workspace_task_scope(task)
    if task.paper_id is None or generator is None:
        raise PaperMapProviderError(
            "Paper map generation requires a configured provider.",
            error_type="CONFIGURATION",
            error_code="paper_map_not_configured",
        )
    map_id, generation = _map_task_identity(task)
    with session_factory() as session:
        if not _requester_is_workspace_member(
            session,
            workspace_id=workspace_id,
            requested_by_user_id=requested_by_user_id,
        ):
            raise PaperMapTaskError("The paper map task requester is not a workspace member.")
        paper_map = session.scalar(
            select(PaperMapModel).where(
                PaperMapModel.id == map_id,
                PaperMapModel.workspace_id == workspace_id,
            )
        )
        paper = _scoped_paper(
            session,
            paper_id=task.paper_id,
            workspace_id=workspace_id,
        )
        if (
            paper_map is None
            or paper is None
            or paper_map.paper_id != paper.id
            or paper_map.requested_by_user_id != requested_by_user_id
            or paper_map.generation != generation
        ):
            raise PaperMapTaskError("The paper map target changed or no longer exists.")
        existing_count = len(paper_map.questions)
        if (
            paper_map.status in {PaperMapStatus.SUCCEEDED, PaperMapStatus.PARTIAL}
            and existing_count
        ):
            return {
                "map_id": paper_map.id,
                "generation": generation,
                "questions": existing_count,
                "idempotent_replay": True,
            }
        if paper_map.content_hash != paper_content_hash(session, paper):
            paper_map.status = PaperMapStatus.STALE
            paper_map.error_type = "STALE_CONTENT"
            paper_map.error_code = "paper_content_changed"
            paper_map.error_message = "Paper content changed before the map was generated."
            paper_map.completed_at = now_utc()
            session.commit()
            raise PaperMapTaskError("Paper content changed before map generation.")
        paper_map.status = PaperMapStatus.RUNNING
        paper_map.error_type = None
        paper_map.error_code = None
        paper_map.error_message = None
        request = build_plan_request(
            session,
            paper_map,
            workspace_id=workspace_id,
            token_budget=context_token_budget,
            max_output_tokens=max_output_tokens,
        )
        paper_map.plan_context_json = plan_context_audit(
            request,
            generation=generation,
            token_budget=context_token_budget,
        )
        session.commit()

    result = generator.plan(request, system_prompt=PAPER_MAP_PLAN_SYSTEM_PROMPT)
    allowed_evidence = {item.block_id for item in request.context if item.block_id}
    with session_factory() as session:
        if not _requester_is_workspace_member(
            session,
            workspace_id=workspace_id,
            requested_by_user_id=requested_by_user_id,
        ):
            raise PaperMapTaskError("The paper map task requester changed before publication.")
        paper_map = session.scalar(
            select(PaperMapModel).where(
                PaperMapModel.id == map_id,
                PaperMapModel.workspace_id == workspace_id,
                PaperMapModel.requested_by_user_id == requested_by_user_id,
            )
        )
        paper = _scoped_paper(
            session,
            paper_id=task.paper_id,
            workspace_id=workspace_id,
        )
        if (
            paper_map is None
            or paper is None
            or paper_map.paper_id != paper.id
            or paper_map.generation != generation
        ):
            raise PaperMapTaskError("The paper map target changed before publication.")
        if paper_map.questions:
            return {
                "map_id": paper_map.id,
                "generation": generation,
                "questions": len(paper_map.questions),
                "idempotent_replay": True,
            }
        root = PaperMapNodeModel(
            map_id=paper_map.id,
            node_key="paper",
            node_type="PAPER",
            label=paper.title,
            body=paper.abstract or "",
            ordinal=0,
        )
        session.add(root)
        session.flush()
        questions: list[PaperMapQuestionModel] = []
        question_texts = {
            candidate.question_key: candidate.question_text
            for candidate in request.question_candidates
        }
        for ordinal, selection in enumerate(result.questions, start=1):
            question = PaperMapQuestionModel(
                map_id=paper_map.id,
                question_key=selection.question_key,
                question_text=question_texts[selection.question_key],
                is_initial=True,
                rationale=selection.rationale,
                evidence_block_ids_json=[
                    block_id
                    for block_id in selection.evidence_block_ids
                    if block_id in allowed_evidence
                ],
                ordinal=ordinal,
                status=PaperMapQuestionStatus.NOT_REQUESTED,
            )
            session.add(question)
            session.flush()
            node = PaperMapNodeModel(
                map_id=paper_map.id,
                question_id=question.id,
                node_key=f"question:{question.question_key}",
                node_type="QUESTION",
                label=question.question_text,
                body=question.rationale,
                status=question.status,
                ordinal=ordinal,
            )
            session.add(node)
            session.flush()
            session.add(
                PaperMapEdgeModel(
                    map_id=paper_map.id,
                    source_node_id=root.id,
                    target_node_id=node.id,
                    edge_type="HAS_QUESTION",
                    is_tree=True,
                )
            )
            questions.append(question)
        paper_map.primary_type = result.primary_type
        paper_map.secondary_types_json = list(result.secondary_types)
        paper_map.type_confidence = result.confidence
        paper_map.provider = generator.provider
        paper_map.model = generator.model
        paper_map.plan_prompt_version = PAPER_MAP_PLAN_PROMPT_VERSION
        paper_map.input_tokens = result.input_tokens
        paper_map.output_tokens = result.output_tokens
        paper_map.latency_ms = result.latency_ms
        paper_map.provider_request_id = result.provider_request_id
        paper_map.status = PaperMapStatus.SUCCEEDED
        paper_map.completed_at = now_utc()
        for question in questions:
            schedule_question_answer(
                session,
                question=question,
                workspace_id=workspace_id,
                requested_by_user_id=requested_by_user_id,
                max_attempts=answer_max_attempts,
                credential_id=task.credential_id,
            )
        session.commit()
        return {
            "map_id": paper_map.id,
            "generation": generation,
            "questions": len(questions),
            "answers_queued": len(questions),
            "input_tokens": result.input_tokens or 0,
            "output_tokens": result.output_tokens or 0,
            "latency_ms": result.latency_ms or 0,
            "idempotent_replay": False,
        }


def _validate_citations(
    session: Session,
    *,
    paper_id: str,
    allowed_block_ids: set[str],
    citations: tuple,
) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    for citation in citations:
        if citation.block_id not in allowed_block_ids:
            raise PaperMapProviderError(
                "Paper map answer cited context that was not supplied.",
                error_type="INVALID_RESPONSE",
                error_code="citation_outside_context",
                retryable=True,
            )
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == citation.block_id,
                ContentBlockModel.paper_id == paper_id,
            )
        )
        canonical_text = block.source_text or "" if block is not None else ""
        canonical_quote = resolve_canonical_quote(canonical_text, citation.quote)
        if block is None or canonical_quote is None:
            raise PaperMapProviderError(
                "Paper map answer citation does not match canonical source text.",
                error_type="INVALID_RESPONSE",
                error_code="citation_quote_invalid",
                retryable=True,
            )
        validated.append({"block_id": citation.block_id, "quote": canonical_quote})
    return validated


def _delete_previous_answer_nodes(
    session: Session,
    *,
    map_id: str,
    question_id: str,
) -> None:
    node_ids = list(
        session.scalars(
            select(PaperMapNodeModel.id).where(
                PaperMapNodeModel.map_id == map_id,
                PaperMapNodeModel.question_id == question_id,
                PaperMapNodeModel.node_type != "QUESTION",
            )
        ).all()
    )
    if not node_ids:
        return
    session.execute(
        delete(PaperMapEdgeModel).where(
            PaperMapEdgeModel.map_id == map_id,
            or_(
                PaperMapEdgeModel.source_node_id.in_(node_ids),
                PaperMapEdgeModel.target_node_id.in_(node_ids),
            )
        )
    )
    session.execute(
        delete(PaperMapNodeModel).where(
            PaperMapNodeModel.map_id == map_id,
            PaperMapNodeModel.id.in_(node_ids),
        )
    )


def answer_map_question(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    generator: PaperMapGenerator | None,
    *,
    context_token_budget: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    workspace_id, _requested_by_user_id = _workspace_task_scope(task)
    if task.paper_id is None or generator is None:
        raise PaperMapProviderError(
            "Paper map generation requires a configured provider.",
            error_type="CONFIGURATION",
            error_code="paper_map_not_configured",
        )
    map_id = str(task.payload.get("map_id", ""))
    question_id = str(task.payload.get("question_id", ""))
    generation = int(task.payload.get("generation", 0))
    if not map_id or not question_id or generation < 1:
        raise PaperMapTaskError("Paper map answer task payload is incomplete.")
    with session_factory() as session:
        if not _requester_is_workspace_member(
            session,
            workspace_id=workspace_id,
            requested_by_user_id=task.requested_by_user_id,
        ):
            raise PaperMapTaskError("The paper map task requester is not a workspace member.")
        paper_map = session.scalar(
            select(PaperMapModel).where(
                PaperMapModel.id == map_id,
                PaperMapModel.workspace_id == workspace_id,
            )
        )
        question = session.scalar(
            select(PaperMapQuestionModel).where(
                PaperMapQuestionModel.id == question_id,
                PaperMapQuestionModel.map_id == map_id,
            )
        )
        paper = _scoped_paper(
            session,
            paper_id=task.paper_id,
            workspace_id=workspace_id,
        )
        if (
            question is None
            or paper_map is None
            or paper is None
            or question.map_id != paper_map.id
            or paper_map.paper_id != task.paper_id
        ):
            raise PaperMapTaskError("The paper map question no longer exists.")
        if question.answer_generation != generation:
            raise PaperMapTaskError("The paper map answer generation is no longer current.")
        if question.status == PaperMapQuestionStatus.SUCCEEDED and question.answer_text:
            return {
                "map_id": map_id,
                "question_id": question_id,
                "generation": generation,
                "idempotent_replay": True,
            }
        question.status = PaperMapQuestionStatus.RUNNING
        question.error_type = None
        question.error_code = None
        question.error_message = None
        request = build_answer_request(
            session,
            question,
            workspace_id=workspace_id,
            token_budget=context_token_budget,
            max_output_tokens=max_output_tokens,
        )
        question.answer_context_json = answer_context_audit(
            request,
            generation=generation,
            token_budget=context_token_budget,
        )
        session.commit()

    result = generator.answer(request, system_prompt=PAPER_MAP_ANSWER_SYSTEM_PROMPT)
    allowed_block_ids = {item.block_id for item in request.context if item.block_id}
    with session_factory() as session:
        if not _requester_is_workspace_member(
            session,
            workspace_id=workspace_id,
            requested_by_user_id=task.requested_by_user_id,
        ):
            raise PaperMapTaskError("The paper map task requester changed before publication.")
        paper_map = session.scalar(
            select(PaperMapModel).where(
                PaperMapModel.id == map_id,
                PaperMapModel.workspace_id == workspace_id,
            )
        )
        question = session.scalar(
            select(PaperMapQuestionModel).where(
                PaperMapQuestionModel.id == question_id,
                PaperMapQuestionModel.map_id == map_id,
            )
        )
        paper = _scoped_paper(
            session,
            paper_id=task.paper_id,
            workspace_id=workspace_id,
        )
        if (
            question is None
            or paper_map is None
            or paper is None
            or paper_map.paper_id != paper.id
            or question.answer_generation != generation
        ):
            raise PaperMapTaskError("The answer target changed before publication.")
        if question.status == PaperMapQuestionStatus.SUCCEEDED and question.answer_text:
            return {
                "map_id": map_id,
                "question_id": question_id,
                "generation": generation,
                "idempotent_replay": True,
            }
        citations = _validate_citations(
            session,
            paper_id=paper_map.paper_id,
            allowed_block_ids=allowed_block_ids,
            citations=result.citations,
        )
        if result.answer_status in {"SUPPORTED", "PARTIALLY_SUPPORTED"} and not citations:
            raise PaperMapProviderError(
                "Paper map answer claimed support without a verified citation.",
                error_type="INVALID_RESPONSE",
                error_code="citations_required",
                retryable=True,
            )
        _delete_previous_answer_nodes(
            session,
            map_id=paper_map.id,
            question_id=question.id,
        )
        question_node = session.scalar(
            select(PaperMapNodeModel).where(
                PaperMapNodeModel.map_id == paper_map.id,
                PaperMapNodeModel.question_id == question.id,
                PaperMapNodeModel.node_type == "QUESTION",
            )
        )
        if question_node is None:
            raise PaperMapTaskError("The question node no longer exists.")
        question_node.body = result.answer
        question_node.status = result.answer_status
        question_node.evidence_json = citations
        keyed_nodes = {"question": question_node}
        for ordinal, detail in enumerate(result.details, start=1):
            evidence = [
                citations[index] for index in detail.citation_indexes if index < len(citations)
            ]
            node = PaperMapNodeModel(
                map_id=paper_map.id,
                question_id=question.id,
                node_key=f"concept:{question.id}:{generation}:{detail.key}",
                node_type="CONCEPT",
                concept_type=detail.concept_type,
                label=detail.label,
                body=detail.body,
                status=result.answer_status,
                evidence_json=evidence,
                ordinal=ordinal,
            )
            session.add(node)
            session.flush()
            keyed_nodes[detail.key] = node
            session.add(
                PaperMapEdgeModel(
                    map_id=paper_map.id,
                    source_node_id=question_node.id,
                    target_node_id=node.id,
                    edge_type="HAS_CONCEPT",
                    is_tree=True,
                )
            )
        existing_relations = {
            (question_node.id, node.id, "HAS_CONCEPT")
            for key, node in keyed_nodes.items()
            if key != "question"
        }
        for relation in result.relations:
            source = keyed_nodes.get(relation.source_key)
            target = keyed_nodes.get(relation.target_key)
            identity = (
                source.id if source else "",
                target.id if target else "",
                relation.edge_type,
            )
            if source is None or target is None or identity in existing_relations:
                continue
            session.add(
                PaperMapEdgeModel(
                    map_id=paper_map.id,
                    source_node_id=source.id,
                    target_node_id=target.id,
                    edge_type=relation.edge_type,
                    is_tree=False,
                )
            )
            existing_relations.add(identity)
        question.status = PaperMapQuestionStatus.SUCCEEDED
        question.answer_status = result.answer_status
        question.answer_text = result.answer
        question.citations_json = citations
        question.answer_prompt_version = PAPER_MAP_ANSWER_PROMPT_VERSION
        question.answer_prompt_hash = paper_map_answer_prompt_hash()
        question.provider = generator.provider
        question.model = generator.model
        question.input_tokens = result.input_tokens
        question.output_tokens = result.output_tokens
        question.latency_ms = result.latency_ms
        question.provider_request_id = result.provider_request_id
        question.error_type = None
        question.error_code = None
        question.error_message = None
        question.completed_at = now_utc()
        session.flush()
        remaining_failures = int(
            session.scalar(
                select(func.count())
                .select_from(PaperMapQuestionModel)
                .where(
                    PaperMapQuestionModel.map_id == paper_map.id,
                    PaperMapQuestionModel.status == PaperMapQuestionStatus.FAILED,
                )
            )
            or 0
        )
        paper_map.status = (
            PaperMapStatus.PARTIAL if remaining_failures else PaperMapStatus.SUCCEEDED
        )
        session.commit()
        return {
            "map_id": map_id,
            "question_id": question_id,
            "generation": generation,
            "answer_status": result.answer_status,
            "citations": len(citations),
            "concepts": len(result.details),
            "input_tokens": result.input_tokens or 0,
            "output_tokens": result.output_tokens or 0,
            "latency_ms": result.latency_ms or 0,
            "idempotent_replay": False,
        }


def mark_paper_map_failure(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    error: Exception,
    *,
    will_retry: bool,
) -> None:
    try:
        workspace_id, _requested_by_user_id = _workspace_task_scope(task)
    except PaperMapTaskError:
        return
    map_id = str(task.payload.get("map_id", ""))
    question_id = str(task.payload.get("question_id", ""))
    generation = int(task.payload.get("generation", 0))
    with session_factory() as session:
        if question_id:
            paper_map = session.scalar(
                select(PaperMapModel).where(
                    PaperMapModel.id == map_id,
                    PaperMapModel.workspace_id == workspace_id,
                )
            )
            question = session.scalar(
                select(PaperMapQuestionModel).where(
                    PaperMapQuestionModel.id == question_id,
                    PaperMapQuestionModel.map_id == map_id,
                )
            )
            if (
                paper_map is None
                or question is None
                or question.answer_generation != generation
            ):
                return
            question.status = (
                PaperMapQuestionStatus.QUEUED if will_retry else PaperMapQuestionStatus.FAILED
            )
            question.error_type = getattr(error, "error_type", type(error).__name__)[:64]
            code = getattr(error, "error_code", None)
            question.error_code = str(code)[:128] if code else None
            question.error_message = redact_task_error_message(error, max_chars=2_000)
            provider_request_id = getattr(error, "provider_request_id", None)
            if provider_request_id:
                question.provider_request_id = str(provider_request_id)[:255]
            if not will_retry:
                question.completed_at = now_utc()
                paper_map.status = PaperMapStatus.PARTIAL
            question_node = session.scalar(
                select(PaperMapNodeModel).where(
                    PaperMapNodeModel.question_id == question.id,
                    PaperMapNodeModel.node_type == "QUESTION",
                )
            )
            if question_node is not None:
                question_node.status = question.status
        elif map_id:
            paper_map = session.scalar(
                select(PaperMapModel).where(
                    PaperMapModel.id == map_id,
                    PaperMapModel.workspace_id == workspace_id,
                )
            )
            if paper_map is None or paper_map.generation != generation:
                return
            paper_map.status = PaperMapStatus.QUEUED if will_retry else PaperMapStatus.FAILED
            paper_map.error_type = getattr(error, "error_type", type(error).__name__)[:64]
            code = getattr(error, "error_code", None)
            paper_map.error_code = str(code)[:128] if code else None
            paper_map.error_message = redact_task_error_message(error, max_chars=2_000)
            provider_request_id = getattr(error, "provider_request_id", None)
            if provider_request_id:
                paper_map.provider_request_id = str(provider_request_id)[:255]
            if not will_retry:
                paper_map.completed_at = now_utc()
        session.commit()
