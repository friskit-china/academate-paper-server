from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import aliased

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.llm_credentials import request_llm_task_credential
from app.application.access import load_workspace_paper
from app.application.qa import ANSWER_QUESTION, QA_PRIORITY
from app.domain.annotations import (
    SelectorValidationError,
    resolve_selector,
    validate_selector,
)
from app.domain.auth import Permission, RequestPrincipal
from app.domain.enums import (
    AnnotationResolutionStatus,
    AnnotationTarget,
    AnnotationType,
    QAStatus,
    TaskStatus,
    TranslationStatus,
)
from app.domain.tasks import TaskScope
from app.domain.translation import translated_display_text
from app.infrastructure.db.models import (
    AnnotationCommentModel,
    AnnotationModel,
    ContentBlockModel,
    ProcessingTaskModel,
    TranslationModel,
    now_utc,
)

router = APIRouter(
    tags=["annotations"],
    dependencies=[Depends(resolve_request_principal)],
)


class AnnotationCreateRequest(BaseModel):
    paper_id: str
    block_id: str | None = None
    annotation_type: Literal["ANNOTATION", "HIGHLIGHT", "USER_NOTE", "AI_QUESTION"]
    target: Literal["SOURCE", "TRANSLATION", "BLOCK", "PAPER"]
    body: str = Field(default="", max_length=4_000)
    is_highlighted: bool | None = None
    selector: dict[str, Any] | None = None
    target_translation_id: str | None = None
    initial_comment: str | None = Field(default=None, max_length=4_000)
    initial_question: str | None = Field(default=None, max_length=4_000)


class AnnotationUpdateRequest(BaseModel):
    body: str | None = Field(default=None, max_length=4_000)
    is_highlighted: bool | None = None
    revision: int = Field(ge=1)


class AnnotationCommentCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4_000)


class AnnotationCommentUpdateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4_000)
    revision: int = Field(ge=1)


class AnnotationQuestionCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4_000)


class AnnotationCommentResponse(BaseModel):
    id: str
    annotation_id: str
    body: str
    revision: int
    created_at: datetime
    updated_at: datetime


class AnnotationResponse(BaseModel):
    id: str
    paper_id: str
    block_id: str | None
    parent_annotation_id: str | None
    anchor_annotation_id: str | None
    annotation_type: str
    target: str
    body: str
    is_highlighted: bool
    creator_type: str
    selector: dict[str, Any] | None
    target_translation_id: str | None
    target_translation_input_hash: str | None
    resolution_status: str
    resolution_reason: str | None
    resolved_start_offset: int | None
    resolved_end_offset: int | None
    revision: int
    qa_status: str | None
    qa_generation: int
    qa_error_type: str | None
    qa_error_code: str | None
    qa_error_message: str | None
    qa_context: dict[str, Any] | None
    citations: list[dict[str, Any]]
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    created_at: datetime
    updated_at: datetime
    comments: list[AnnotationCommentResponse]


class AnnotationCreateResponse(BaseModel):
    annotation: AnnotationResponse
    task_id: str | None = None
    question: AnnotationResponse | None = None


class QuestionRetryResponse(BaseModel):
    question: AnnotationResponse
    task_id: str


class AnnotationQuestionCreateResponse(BaseModel):
    question: AnnotationResponse
    task_id: str


def _current_translation(
    session,
    block: ContentBlockModel,
    settings,
    principal: RequestPrincipal,
) -> TranslationModel | None:
    statement = (
        select(TranslationModel)
        .where(
            TranslationModel.block_id == block.id,
            TranslationModel.workspace_id == principal.active_workspace_id,
            or_(
                TranslationModel.prompt_kind == "global",
                and_(
                    TranslationModel.prompt_kind.in_(["custom", "paper"]),
                    TranslationModel.requested_by_user_id == principal.user_id,
                ),
            ),
            TranslationModel.source_hash == block.content_hash,
            TranslationModel.language == settings.translation_target_language,
        )
        .order_by(TranslationModel.created_at.desc())
    )
    if settings.translation_configured:
        statement = statement.where(
            TranslationModel.provider == settings.llm_provider,
            TranslationModel.model == settings.llm_model,
        )
    current = session.scalar(statement.limit(1))
    if current is not None and current.status == TranslationStatus.SUCCEEDED:
        return current
    return None


def _comment_response(comment: AnnotationCommentModel) -> AnnotationCommentResponse:
    return AnnotationCommentResponse(
        id=comment.id,
        annotation_id=comment.annotation_id,
        body=comment.body,
        revision=comment.revision,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


def _active_comments(
    session,
    annotation_id: str,
    workspace_id: str,
    creator_user_id: str,
) -> list[AnnotationCommentModel]:
    return list(
        session.scalars(
            select(AnnotationCommentModel)
            .where(
                AnnotationCommentModel.annotation_id == annotation_id,
                AnnotationCommentModel.workspace_id == workspace_id,
                AnnotationCommentModel.creator_user_id == creator_user_id,
                AnnotationCommentModel.deleted_at.is_(None),
            )
            .order_by(AnnotationCommentModel.created_at)
        ).all()
    )


def _prune_empty_anchor(session, annotation: AnnotationModel) -> bool:
    """Soft-delete an Annotation anchor once it no longer owns user-visible content."""
    if (
        annotation.deleted_at is not None
        or annotation.annotation_type != AnnotationType.ANNOTATION
        or annotation.creator_type != "USER"
        or annotation.is_highlighted
    ):
        return False
    # The application session intentionally disables autoflush. Persist pending comment/question
    # removals before checking whether any active child content remains.
    session.flush()
    has_comment = session.scalar(
        select(AnnotationCommentModel.id)
        .where(
                AnnotationCommentModel.annotation_id == annotation.id,
                AnnotationCommentModel.workspace_id == annotation.workspace_id,
                AnnotationCommentModel.creator_user_id == annotation.creator_user_id,
                AnnotationCommentModel.deleted_at.is_(None),
        )
        .limit(1)
    )
    if has_comment is not None:
        return False
    has_question = session.scalar(
        select(AnnotationModel.id)
        .where(
                AnnotationModel.anchor_annotation_id == annotation.id,
                AnnotationModel.workspace_id == annotation.workspace_id,
                AnnotationModel.creator_user_id == annotation.creator_user_id,
                AnnotationModel.annotation_type == AnnotationType.AI_QUESTION,
            AnnotationModel.deleted_at.is_(None),
        )
        .limit(1)
    )
    if has_question is not None:
        return False
    annotation.deleted_at = now_utc()
    return True


def _response(
    annotation: AnnotationModel,
    comments: list[AnnotationCommentModel] | None = None,
) -> AnnotationResponse:
    return AnnotationResponse(
        id=annotation.id,
        paper_id=annotation.paper_id,
        block_id=annotation.block_id,
        parent_annotation_id=annotation.parent_annotation_id,
        anchor_annotation_id=annotation.anchor_annotation_id,
        annotation_type=annotation.annotation_type,
        target=annotation.target,
        body=annotation.body,
        is_highlighted=annotation.is_highlighted,
        creator_type=annotation.creator_type,
        selector=annotation.selector_json,
        target_translation_id=annotation.target_translation_id,
        target_translation_input_hash=annotation.target_translation_input_hash,
        resolution_status=annotation.resolution_status,
        resolution_reason=annotation.resolution_reason,
        resolved_start_offset=annotation.resolved_start_offset,
        resolved_end_offset=annotation.resolved_end_offset,
        revision=annotation.revision,
        qa_status=annotation.qa_status,
        qa_generation=annotation.qa_generation,
        qa_error_type=annotation.qa_error_type,
        qa_error_code=annotation.qa_error_code,
        qa_error_message=annotation.qa_error_message,
        qa_context=annotation.qa_context_json,
        citations=annotation.citations_json or [],
        provider=annotation.provider,
        model=annotation.model,
        input_tokens=annotation.input_tokens,
        output_tokens=annotation.output_tokens,
        latency_ms=annotation.latency_ms,
        created_at=annotation.created_at,
        updated_at=annotation.updated_at,
        comments=[_comment_response(comment) for comment in comments or []],
    )


def _target_text_for_create(
    session,
    payload: AnnotationCreateRequest,
    settings,
    principal: RequestPrincipal,
) -> tuple[ContentBlockModel | None, TranslationModel | None, str | None]:
    if payload.target == AnnotationTarget.PAPER:
        if payload.block_id or payload.selector or payload.target_translation_id:
            raise HTTPException(
                status_code=422, detail="PAPER annotations cannot have a block or selector."
            )
        return None, None, None
    if not payload.block_id:
        raise HTTPException(status_code=422, detail="This annotation target requires a block.")
    block = session.scalar(
        select(ContentBlockModel).where(
            ContentBlockModel.id == payload.block_id,
            ContentBlockModel.paper_id == payload.paper_id,
        )
    )
    if block is None:
        raise HTTPException(status_code=404, detail="Annotation block not found.")
    if payload.target == AnnotationTarget.BLOCK:
        if payload.selector or payload.target_translation_id:
            raise HTTPException(status_code=422, detail="BLOCK annotations cannot have a selector.")
        return block, None, None
    if payload.selector is None:
        raise HTTPException(status_code=422, detail="Text range annotations require a selector.")
    if payload.target == AnnotationTarget.SOURCE:
        if payload.target_translation_id:
            raise HTTPException(
                status_code=422, detail="SOURCE annotations cannot target a translation."
            )
        if not block.source_text:
            raise HTTPException(status_code=422, detail="This block has no selectable source text.")
        return block, None, block.source_text
    current = _current_translation(session, block, settings, principal)
    if (
        current is None
        or current.id != payload.target_translation_id
        or not current.translated_text
    ):
        raise HTTPException(
            status_code=409,
            detail="The selected translation is no longer the current successful revision.",
        )
    return block, current, translated_display_text(
        current.translated_text,
        current.translated_payload_json,
    )


def _enqueue_question(
    session,
    question: AnnotationModel,
    request: Request,
    principal: RequestPrincipal,
) -> ProcessingTaskModel:
    if (
        question.workspace_id != principal.active_workspace_id
        or question.creator_user_id != principal.user_id
        or load_workspace_paper(session, principal, question.paper_id) is None
    ):
        raise HTTPException(status_code=404, detail="Question not found.")
    settings = request.app.state.settings
    llm_credential = request_llm_task_credential(
        request,
        user_id=principal.user_id,
        allow_unconfigured_environment=True,
    )
    assert llm_credential is not None
    question.qa_generation += 1
    question.qa_status = QAStatus.QUEUED
    question.qa_error_type = None
    question.qa_error_code = None
    question.qa_error_message = None
    scope = TaskScope.user_private(
        workspace_id=principal.active_workspace_id,
        requested_by_user_id=principal.user_id,
        credential_id=llm_credential.credential_id,
    )
    task = ProcessingTaskModel(
        paper_id=question.paper_id,
        task_type=ANSWER_QUESTION,
        priority=QA_PRIORITY,
        idempotency_key=f"qa-answer:{question.id}:{question.qa_generation}",
        visibility=scope.visibility,
        scope_key=scope.scope_key,
        workspace_id=scope.workspace_id,
        requested_by_user_id=scope.requested_by_user_id,
        credential_id=scope.credential_id,
        payload_json={
            "question_id": question.id,
            "generation": question.qa_generation,
            "provider": llm_credential.provider,
            "model": llm_credential.model,
        },
        max_attempts=settings.qa_max_attempts,
    )
    session.add(task)
    return task


def _create_anchor_question(
    session,
    anchor: AnnotationModel,
    body: str,
    request: Request,
    principal: RequestPrincipal,
) -> tuple[AnnotationModel, ProcessingTaskModel]:
    if (
        anchor.workspace_id != principal.active_workspace_id
        or anchor.creator_user_id != principal.user_id
    ):
        raise HTTPException(status_code=404, detail="Annotation not found.")
    question = AnnotationModel(
        workspace_id=principal.active_workspace_id,
        creator_user_id=principal.user_id,
        paper_id=anchor.paper_id,
        block_id=anchor.block_id,
        anchor_annotation_id=anchor.id,
        annotation_type=AnnotationType.AI_QUESTION,
        target=anchor.target,
        body=body,
        is_highlighted=False,
        creator_type="USER",
        selector_json=anchor.selector_json,
        target_translation_id=anchor.target_translation_id,
        target_translation_input_hash=anchor.target_translation_input_hash,
        resolution_status=anchor.resolution_status,
        resolution_reason=anchor.resolution_reason,
        resolved_start_offset=anchor.resolved_start_offset,
        resolved_end_offset=anchor.resolved_end_offset,
        resolved_at=anchor.resolved_at,
        qa_status=QAStatus.QUEUED,
    )
    session.add(question)
    session.flush()
    return question, _enqueue_question(session, question, request, principal)


def _scoped_annotation(
    session,
    principal: RequestPrincipal,
    annotation_id: str,
) -> AnnotationModel | None:
    annotation = session.scalar(
        select(AnnotationModel).where(
            AnnotationModel.id == annotation_id,
            AnnotationModel.workspace_id == principal.active_workspace_id,
            AnnotationModel.creator_user_id == principal.user_id,
            AnnotationModel.deleted_at.is_(None),
        )
    )
    if annotation is None:
        return None
    if load_workspace_paper(session, principal, annotation.paper_id) is None:
        return None
    return annotation


@router.post(
    "/annotations",
    status_code=status.HTTP_201_CREATED,
    response_model=AnnotationCreateResponse,
    responses={202: {"model": AnnotationCreateResponse, "description": "Answer task queued"}},
)
def create_annotation(
    payload: AnnotationCreateRequest,
    request: Request,
    response: Response,
) -> AnnotationCreateResponse:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    is_anchor_type = payload.annotation_type in {
        AnnotationType.ANNOTATION,
        AnnotationType.HIGHLIGHT,
        AnnotationType.USER_NOTE,
    }
    is_paper_note = (
        payload.annotation_type == AnnotationType.USER_NOTE
        and payload.target == AnnotationTarget.PAPER
    )
    is_anchor_request = is_anchor_type and not is_paper_note
    if is_anchor_request and payload.target not in {
        AnnotationTarget.SOURCE,
        AnnotationTarget.TRANSLATION,
    }:
        raise HTTPException(status_code=422, detail="Annotations require a text range target.")
    body = payload.body.strip()
    initial_comment = (payload.initial_comment or "").strip()
    initial_question = (payload.initial_question or "").strip()
    if (
        payload.annotation_type == AnnotationType.USER_NOTE
        and is_anchor_request
        and not initial_comment
    ):
        initial_comment = body
    if payload.annotation_type == AnnotationType.AI_QUESTION and not body:
        raise HTTPException(status_code=422, detail="Questions require text.")
    if payload.annotation_type == AnnotationType.USER_NOTE and not (
        initial_comment or (is_paper_note and body)
    ):
        raise HTTPException(status_code=422, detail="Notes require comment text.")
    if not is_anchor_request and (initial_comment or initial_question):
        raise HTTPException(
            status_code=422,
            detail="Initial comments and linked questions require an Annotation anchor.",
        )
    if is_paper_note:
        if payload.is_highlighted:
            raise HTTPException(status_code=422, detail="PAPER comments cannot be highlighted.")
        is_highlighted = False
    else:
        is_highlighted = (
            payload.is_highlighted
            if payload.is_highlighted is not None
            else payload.annotation_type in {AnnotationType.HIGHLIGHT, AnnotationType.USER_NOTE}
        )
    if is_anchor_request and not (is_highlighted or initial_comment or initial_question):
        raise HTTPException(
            status_code=422,
            detail="An Annotation requires a highlight, comment, or AI question.",
        )
    with request.app.state.session_factory() as session:
        if load_workspace_paper(session, principal, payload.paper_id) is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        block, translation, target_text = _target_text_for_create(
            session,
            payload,
            request.app.state.settings,
            principal,
        )
        selector = None
        resolution_status = AnnotationResolutionStatus.NOT_APPLICABLE
        resolved_start = None
        resolved_end = None
        if target_text is not None and block is not None:
            try:
                selector = validate_selector(
                    payload.selector, block_id=block.id, target_text=target_text
                )
            except SelectorValidationError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            resolution_status = AnnotationResolutionStatus.RESOLVED_OFFSETS
            resolved_start = selector["start_offset"]
            resolved_end = selector["end_offset"]
        annotation = AnnotationModel(
            workspace_id=principal.active_workspace_id,
            creator_user_id=principal.user_id,
            paper_id=payload.paper_id,
            block_id=block.id if block else None,
            annotation_type=(
                AnnotationType.ANNOTATION if is_anchor_request else payload.annotation_type
            ),
            target=payload.target,
            body="" if is_anchor_request else body,
            is_highlighted=is_highlighted,
            creator_type="USER",
            selector_json=selector,
            target_translation_id=translation.id if translation else None,
            target_translation_input_hash=translation.input_hash if translation else None,
            resolution_status=resolution_status,
            resolved_start_offset=resolved_start,
            resolved_end_offset=resolved_end,
            resolved_at=now_utc(),
            qa_status=(
                QAStatus.QUEUED if payload.annotation_type == AnnotationType.AI_QUESTION else None
            ),
        )
        session.add(annotation)
        session.flush()
        if initial_comment:
            session.add(
                AnnotationCommentModel(
                    workspace_id=principal.active_workspace_id,
                    creator_user_id=principal.user_id,
                    annotation_id=annotation.id,
                    body=initial_comment,
                )
            )
        question = None
        task = None
        if initial_question:
            question, task = _create_anchor_question(
                session,
                annotation,
                initial_question,
                request,
                principal,
            )
        elif payload.annotation_type == AnnotationType.AI_QUESTION:
            task = _enqueue_question(session, annotation, request, principal)
        session.commit()
        session.refresh(annotation)
        comments = _active_comments(
            session,
            annotation.id,
            principal.active_workspace_id,
            principal.user_id,
        )
        if task is not None:
            session.refresh(task)
        if question is not None:
            session.refresh(question)
        if task is not None:
            response.status_code = status.HTTP_202_ACCEPTED
        else:
            response.status_code = status.HTTP_201_CREATED
        return AnnotationCreateResponse(
            annotation=_response(annotation, comments),
            task_id=task.id if task else None,
            question=_response(question) if question else None,
        )


def _mark_unresolved(annotation: AnnotationModel, reason: str) -> None:
    annotation.resolution_status = AnnotationResolutionStatus.UNRESOLVED
    annotation.resolution_reason = reason
    annotation.resolved_start_offset = None
    annotation.resolved_end_offset = None
    annotation.resolved_at = now_utc()


def _refresh_resolution(
    session,
    annotation: AnnotationModel,
    settings,
    principal: RequestPrincipal,
) -> None:
    if annotation.target in {AnnotationTarget.BLOCK, AnnotationTarget.PAPER}:
        annotation.resolution_status = AnnotationResolutionStatus.NOT_APPLICABLE
        annotation.resolution_reason = None
        return
    block = None
    if annotation.block_id:
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == annotation.block_id,
                ContentBlockModel.paper_id == annotation.paper_id,
            )
        )
    if block is None:
        _mark_unresolved(annotation, "BLOCK_NOT_FOUND")
        return
    target_text = block.source_text
    if annotation.target == AnnotationTarget.TRANSLATION:
        translation = None
        if annotation.target_translation_id:
            translation = session.scalar(
                select(TranslationModel).where(
                    TranslationModel.id == annotation.target_translation_id,
                    TranslationModel.workspace_id == principal.active_workspace_id,
                    or_(
                        TranslationModel.prompt_kind == "global",
                        and_(
                            TranslationModel.prompt_kind.in_(["custom", "paper"]),
                            TranslationModel.requested_by_user_id == principal.user_id,
                        ),
                    ),
                )
            )
        if (
            translation is None
            or translation.block_id != block.id
            or translation.input_hash != annotation.target_translation_input_hash
        ):
            _mark_unresolved(annotation, "TRANSLATION_REVISION_NOT_FOUND")
            return
        current = _current_translation(session, block, settings, principal)
        if current is None or current.id != translation.id:
            _mark_unresolved(annotation, "TRANSLATION_REVISION_NOT_CURRENT")
            return
        target_text = translated_display_text(
            translation.translated_text,
            translation.translated_payload_json,
        )
    if not target_text:
        _mark_unresolved(annotation, "TARGET_TEXT_NOT_FOUND")
        return
    result = resolve_selector(annotation.selector_json, target_text)
    annotation.resolution_status = result.status
    annotation.resolution_reason = result.reason
    annotation.resolved_start_offset = result.start_offset
    annotation.resolved_end_offset = result.end_offset
    annotation.resolved_at = now_utc()


@router.get("/papers/{paper_id}/annotations", response_model=list[AnnotationResponse])
def list_annotations(paper_id: str, request: Request) -> list[AnnotationResponse]:
    principal = require_request_permission(request, Permission.ANNOTATION_READ)
    with request.app.state.session_factory() as session:
        if load_workspace_paper(session, principal, paper_id) is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        parent_question = aliased(AnnotationModel)
        owned_parent_question = (
            select(parent_question.id)
            .where(
                parent_question.id == AnnotationModel.parent_annotation_id,
                parent_question.workspace_id == principal.active_workspace_id,
                parent_question.creator_user_id == principal.user_id,
                parent_question.annotation_type == AnnotationType.AI_QUESTION,
                parent_question.deleted_at.is_(None),
            )
            .exists()
        )
        annotations = list(
            session.scalars(
                select(AnnotationModel)
                .where(
                    AnnotationModel.paper_id == paper_id,
                    AnnotationModel.workspace_id == principal.active_workspace_id,
                    AnnotationModel.deleted_at.is_(None),
                    or_(
                        AnnotationModel.creator_user_id == principal.user_id,
                        and_(
                            AnnotationModel.annotation_type == AnnotationType.AI_RESPONSE,
                            owned_parent_question,
                        ),
                    ),
                )
                .order_by(AnnotationModel.created_at)
            ).all()
        )
        for annotation in annotations:
            _refresh_resolution(
                session,
                annotation,
                request.app.state.settings,
                principal,
            )
        session.commit()
        return [
            _response(
                annotation,
                _active_comments(
                    session,
                    annotation.id,
                    principal.active_workspace_id,
                    principal.user_id,
                ),
            )
            for annotation in annotations
        ]


@router.patch("/annotations/{annotation_id}", response_model=AnnotationResponse)
def update_annotation(
    annotation_id: str, payload: AnnotationUpdateRequest, request: Request
) -> AnnotationResponse:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    with request.app.state.session_factory() as session:
        annotation = _scoped_annotation(session, principal, annotation_id)
        if annotation is None:
            raise HTTPException(status_code=404, detail="Annotation not found.")
        if (
            annotation.annotation_type
            not in {
                AnnotationType.ANNOTATION,
                AnnotationType.HIGHLIGHT,
                AnnotationType.USER_NOTE,
            }
            or annotation.creator_type != "USER"
        ):
            raise HTTPException(status_code=409, detail="Only user annotations can be edited.")
        if annotation.revision != payload.revision:
            raise HTTPException(
                status_code=409, detail="The annotation was changed by another request."
            )
        is_highlighted = (
            annotation.is_highlighted if payload.is_highlighted is None else payload.is_highlighted
        )
        comments = _active_comments(
            session,
            annotation.id,
            principal.active_workspace_id,
            principal.user_id,
        )
        if payload.body is not None:
            legacy_body = payload.body.strip()
            if legacy_body:
                if comments:
                    comments[0].body = legacy_body
                    comments[0].revision += 1
                else:
                    session.add(
                        AnnotationCommentModel(
                            workspace_id=principal.active_workspace_id,
                            creator_user_id=principal.user_id,
                            annotation_id=annotation.id,
                            body=legacy_body,
                        )
                    )
            else:
                for comment in comments:
                    comment.deleted_at = now_utc()
        annotation.body = ""
        annotation.is_highlighted = is_highlighted
        annotation.annotation_type = AnnotationType.ANNOTATION
        annotation.revision += 1
        _prune_empty_anchor(session, annotation)
        session.commit()
        session.refresh(annotation)
        return _response(
            annotation,
            _active_comments(
                session,
                annotation.id,
                principal.active_workspace_id,
                principal.user_id,
            ),
        )


@router.post(
    "/annotations/{annotation_id}/comments",
    response_model=AnnotationCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_annotation_comment(
    annotation_id: str,
    payload: AnnotationCommentCreateRequest,
    request: Request,
) -> AnnotationCommentResponse:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Comment text is required.")
    with request.app.state.session_factory() as session:
        annotation = _scoped_annotation(session, principal, annotation_id)
        if (
            annotation is None
            or annotation.deleted_at is not None
            or annotation.annotation_type != AnnotationType.ANNOTATION
            or annotation.creator_type != "USER"
        ):
            raise HTTPException(status_code=404, detail="Annotation not found.")
        comment = AnnotationCommentModel(
            workspace_id=principal.active_workspace_id,
            creator_user_id=principal.user_id,
            annotation_id=annotation.id,
            body=body,
        )
        session.add(comment)
        annotation.revision += 1
        session.commit()
        session.refresh(comment)
        return _comment_response(comment)


@router.patch(
    "/annotation-comments/{comment_id}",
    response_model=AnnotationCommentResponse,
)
def update_annotation_comment(
    comment_id: str,
    payload: AnnotationCommentUpdateRequest,
    request: Request,
) -> AnnotationCommentResponse:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Comment text is required.")
    with request.app.state.session_factory() as session:
        row = session.execute(
            select(AnnotationCommentModel, AnnotationModel)
            .join(
                AnnotationModel,
                AnnotationModel.id == AnnotationCommentModel.annotation_id,
            )
            .where(
                AnnotationCommentModel.id == comment_id,
                AnnotationCommentModel.workspace_id == principal.active_workspace_id,
                AnnotationCommentModel.creator_user_id == principal.user_id,
                AnnotationCommentModel.deleted_at.is_(None),
                AnnotationModel.workspace_id == principal.active_workspace_id,
                AnnotationModel.creator_user_id == principal.user_id,
                AnnotationModel.deleted_at.is_(None),
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Comment not found.")
        comment, annotation = row
        if load_workspace_paper(session, principal, annotation.paper_id) is None:
            raise HTTPException(status_code=404, detail="Annotation not found.")
        if comment.revision != payload.revision:
            raise HTTPException(
                status_code=409, detail="The comment was changed by another request."
            )
        comment.body = body
        comment.revision += 1
        annotation.revision += 1
        session.commit()
        session.refresh(comment)
        return _comment_response(comment)


@router.delete(
    "/annotation-comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_annotation_comment(comment_id: str, request: Request) -> None:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    with request.app.state.session_factory() as session:
        row = session.execute(
            select(AnnotationCommentModel, AnnotationModel)
            .join(
                AnnotationModel,
                AnnotationModel.id == AnnotationCommentModel.annotation_id,
            )
            .where(
                AnnotationCommentModel.id == comment_id,
                AnnotationCommentModel.workspace_id == principal.active_workspace_id,
                AnnotationCommentModel.creator_user_id == principal.user_id,
                AnnotationCommentModel.deleted_at.is_(None),
                AnnotationModel.workspace_id == principal.active_workspace_id,
                AnnotationModel.creator_user_id == principal.user_id,
                AnnotationModel.deleted_at.is_(None),
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Comment not found.")
        comment, annotation = row
        if load_workspace_paper(session, principal, annotation.paper_id) is None:
            raise HTTPException(status_code=404, detail="Annotation not found.")
        comment.deleted_at = now_utc()
        annotation.revision += 1
        _prune_empty_anchor(session, annotation)
        session.commit()


@router.post(
    "/annotations/{annotation_id}/questions",
    response_model=AnnotationQuestionCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_annotation_question(
    annotation_id: str,
    payload: AnnotationQuestionCreateRequest,
    request: Request,
) -> AnnotationQuestionCreateResponse:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Question text is required.")
    with request.app.state.session_factory() as session:
        annotation = _scoped_annotation(session, principal, annotation_id)
        if (
            annotation is None
            or annotation.deleted_at is not None
            or annotation.annotation_type != AnnotationType.ANNOTATION
            or annotation.creator_type != "USER"
        ):
            raise HTTPException(status_code=404, detail="Annotation not found.")
        question, task = _create_anchor_question(
            session,
            annotation,
            body,
            request,
            principal,
        )
        annotation.revision += 1
        session.commit()
        session.refresh(question)
        session.refresh(task)
        return AnnotationQuestionCreateResponse(question=_response(question), task_id=task.id)


@router.delete("/annotations/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_annotation(annotation_id: str, request: Request) -> None:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    with request.app.state.session_factory() as session:
        annotation = _scoped_annotation(session, principal, annotation_id)
        if annotation is None:
            raise HTTPException(status_code=404, detail="Annotation not found.")
        if annotation.creator_type != "USER":
            raise HTTPException(
                status_code=409, detail="AI responses are managed by their question."
            )
        anchor_id = annotation.anchor_annotation_id
        annotation.deleted_at = now_utc()
        questions = [annotation] if annotation.annotation_type == AnnotationType.AI_QUESTION else []
        if annotation.annotation_type == AnnotationType.ANNOTATION:
            questions = list(
                session.scalars(
                    select(AnnotationModel).where(
                        AnnotationModel.anchor_annotation_id == annotation.id,
                        AnnotationModel.workspace_id == principal.active_workspace_id,
                        AnnotationModel.creator_user_id == principal.user_id,
                        AnnotationModel.annotation_type == AnnotationType.AI_QUESTION,
                        AnnotationModel.deleted_at.is_(None),
                    )
                ).all()
            )
            for question in questions:
                question.deleted_at = now_utc()
        question_ids = {question.id for question in questions}
        if question_ids:
            session.execute(
                delete(AnnotationModel).where(
                    AnnotationModel.workspace_id == principal.active_workspace_id,
                    AnnotationModel.parent_annotation_id.in_(question_ids),
                    AnnotationModel.annotation_type == AnnotationType.AI_RESPONSE,
                )
            )
            tasks = session.scalars(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.paper_id == annotation.paper_id,
                    ProcessingTaskModel.workspace_id == principal.active_workspace_id,
                    ProcessingTaskModel.requested_by_user_id == principal.user_id,
                    ProcessingTaskModel.task_type == ANSWER_QUESTION,
                    ProcessingTaskModel.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
                )
            ).all()
            for task in tasks:
                if task.payload_json.get("question_id") in question_ids:
                    task.status = TaskStatus.CANCELLED
                    task.finished_at = now_utc()
        if anchor_id:
            anchor = session.scalar(
                select(AnnotationModel).where(
                    AnnotationModel.id == anchor_id,
                    AnnotationModel.workspace_id == principal.active_workspace_id,
                    AnnotationModel.creator_user_id == principal.user_id,
                )
            )
            if anchor is not None:
                _prune_empty_anchor(session, anchor)
        session.commit()


@router.post(
    "/annotations/{question_id}/answer",
    response_model=QuestionRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_question(question_id: str, request: Request) -> QuestionRetryResponse:
    principal = require_request_permission(request, Permission.ANNOTATION_WRITE)
    with request.app.state.session_factory() as session:
        question = _scoped_annotation(session, principal, question_id)
        if (
            question is None
            or question.deleted_at is not None
            or question.annotation_type != AnnotationType.AI_QUESTION
        ):
            raise HTTPException(status_code=404, detail="Question not found.")
        if question.qa_status != QAStatus.FAILED:
            raise HTTPException(status_code=409, detail="Only a failed answer can be retried.")
        task = _enqueue_question(session, question, request, principal)
        session.commit()
        session.refresh(question)
        session.refresh(task)
        return QuestionRetryResponse(question=_response(question), task_id=task.id)
