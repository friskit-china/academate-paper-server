"""Review and copy verifiable source annotations between editions of one arXiv work."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.application.access import load_workspace_paper
from app.domain.annotations import (
    MAX_CONTEXT_CODEPOINTS,
    SELECTOR_VERSION,
    normalize_selector_text,
    resolve_selector,
    selector_text_hash,
)
from app.domain.arxiv import arxiv_version_number
from app.domain.auth import Permission, RequestPrincipal
from app.domain.enums import (
    AnnotationResolutionStatus,
    AnnotationTarget,
    AnnotationType,
    ProcessingStage,
)
from app.infrastructure.db.models import (
    AnnotationCommentModel,
    AnnotationModel,
    ContentBlockModel,
    PaperModel,
    now_utc,
)

router = APIRouter(tags=["annotations"], dependencies=[Depends(resolve_request_principal)])
_MIGRATION_NAMESPACE = UUID("4332e6f2-bd2f-4d2e-87a7-2e5d8296f2b5")
_READING_STAGES = {
    ProcessingStage.READING_READY,
    ProcessingStage.SEMANTIC_READY,
    ProcessingStage.KNOWLEDGE_READY,
}
_CopyStatus = Literal["MATCHED", "AMBIGUOUS", "MISSING", "ALREADY_COPIED"]
_AnnotationID = Annotated[str, Field(min_length=1, max_length=36)]


class AnnotationMigrationCandidate(BaseModel):
    source_annotation_id: str
    source_block_id: str | None
    quote: str
    body: str
    status: _CopyStatus
    target_block_id: str | None


class AnnotationMigrationSection(BaseModel):
    title: str
    status: Literal["CHANGED", "ADDED", "REMOVED"]


class AnnotationMigrationPreview(BaseModel):
    candidates: list[AnnotationMigrationCandidate]
    sections: list[AnnotationMigrationSection]


class AnnotationMigrationCopyRequest(BaseModel):
    target_paper_id: _AnnotationID
    source_annotation_ids: Annotated[list[_AnnotationID], Field(min_length=1, max_length=200)]


class AnnotationMigrationCopyResponse(BaseModel):
    copied_annotation_ids: list[str]
    skipped_source_annotation_ids: list[str]


@dataclass(frozen=True, slots=True)
class _Match:
    block_id: str
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    annotation: AnnotationModel
    comments: list[AnnotationCommentModel]
    quote: str
    status: _CopyStatus
    match: _Match | None
    copy_id: str
    origin_id: str

    def response(self) -> AnnotationMigrationCandidate:
        return AnnotationMigrationCandidate(
            source_annotation_id=self.annotation.id,
            source_block_id=self.annotation.block_id,
            quote=self.quote,
            body=self.annotation.body or (self.comments[0].body if self.comments else ""),
            status=self.status,
            target_block_id=self.match.block_id if self.match else None,
        )


def _editions(
    session: Session, principal: RequestPrincipal, source_id: str, target_id: str
) -> tuple[PaperModel, PaperModel]:
    source = load_workspace_paper(session, principal, source_id)
    target = load_workspace_paper(session, principal, target_id)
    if source is None or target is None:
        raise HTTPException(404, detail={"code": "PAPER_NOT_FOUND"})
    first, second = source.paper, target.paper
    first_version = arxiv_version_number(first.arxiv_version)
    second_version = arxiv_version_number(second.arxiv_version)
    if (
        first.id == second.id
        or not first.arxiv_id
        or first.arxiv_id.casefold() != second.arxiv_id.casefold()
        or not second_version
        or first_version == second_version
    ):
        raise HTTPException(422, detail={"code": "ARXIV_EDITION_MISMATCH"})
    if any(edition.processing_stage not in _READING_STAGES for edition in (first, second)):
        raise HTTPException(409, detail={"code": "ARXIV_EDITION_NOT_READY"})
    return first, second


def _origin_id(annotation: AnnotationModel) -> str:
    selector = annotation.selector_json
    if isinstance(selector, dict):
        origin = selector.get("migration_origin_annotation_id")
        if isinstance(origin, str) and len(origin) == 36:
            return origin
    return annotation.id


def _copy_id(annotation_id: str, target_paper_id: str) -> str:
    # Stable across devices and idempotency keys. The source and target remain independent,
    # including when a reader intentionally deletes the copy.
    return str(uuid5(_MIGRATION_NAMESPACE, f"{annotation_id}:{target_paper_id}"))


def _load_blocks(session: Session, paper_id: str) -> list[ContentBlockModel]:
    return list(
        session.scalars(
            select(ContentBlockModel)
            .where(ContentBlockModel.paper_id == paper_id)
            .options(
                load_only(
                    ContentBlockModel.id,
                    ContentBlockModel.block_type,
                    ContentBlockModel.heading_level,
                    ContentBlockModel.source_text,
                )
            )
            .order_by(ContentBlockModel.ordinal_path, ContentBlockModel.id)
        )
    )


def _find_match(
    annotation: AnnotationModel,
    quote: str,
    source_text: str | None,
    target_texts: list[tuple[str, str]],
) -> tuple[_CopyStatus, _Match | None]:
    if source_text is None or not quote:
        return "MISSING", None
    original = resolve_selector(annotation.selector_json, source_text)
    if original.start_offset is None or original.end_offset is None:
        return "MISSING", None
    prefix = source_text[
        max(0, original.start_offset - MAX_CONTEXT_CODEPOINTS) : original.start_offset
    ]
    suffix = source_text[original.end_offset : original.end_offset + MAX_CONTEXT_CODEPOINTS]
    match = None
    for block_id, text in target_texts:
        start = text.find(quote)
        if start < 0:
            continue
        if match is not None or text.find(quote, start + 1) >= 0:
            return "AMBIGUOUS", None
        match = _Match(block_id, text, start, start + len(quote))
    if match is None:
        return "MISSING", None
    if not (
        match.text[max(0, match.start - len(prefix)) : match.start] == prefix
        and match.text[match.end : match.end + len(suffix)] == suffix
        and (prefix or suffix or match.text == quote)
    ):
        return "AMBIGUOUS", None
    return "MATCHED", match


def _candidates(
    session: Session,
    principal: RequestPrincipal,
    source: PaperModel,
    target: PaperModel,
    source_blocks: list[ContentBlockModel],
    target_blocks: list[ContentBlockModel],
    selected: set[str] | None = None,
) -> list[_Candidate]:
    notes = select(AnnotationModel).where(
        AnnotationModel.workspace_id == principal.active_workspace_id,
        AnnotationModel.creator_user_id == principal.user_id,
        AnnotationModel.creator_type == "USER",
        AnnotationModel.paper_id == source.id,
        AnnotationModel.deleted_at.is_(None),
        AnnotationModel.target == AnnotationTarget.SOURCE,
        AnnotationModel.annotation_type.in_(
            [AnnotationType.ANNOTATION, AnnotationType.HIGHLIGHT, AnnotationType.USER_NOTE]
        ),
    )
    if selected is not None:
        notes = notes.where(AnnotationModel.id.in_(selected))
    annotations = list(
        session.scalars(notes.order_by(AnnotationModel.created_at, AnnotationModel.id))
    )
    if not annotations:
        return []
    comments_by_note: dict[str, list[AnnotationCommentModel]] = defaultdict(list)
    comments = session.scalars(
        select(AnnotationCommentModel)
        .where(
            AnnotationCommentModel.annotation_id.in_(notes.with_only_columns(AnnotationModel.id)),
            AnnotationCommentModel.workspace_id == principal.active_workspace_id,
            AnnotationCommentModel.creator_user_id == principal.user_id,
            AnnotationCommentModel.deleted_at.is_(None),
        )
        .order_by(AnnotationCommentModel.created_at, AnnotationCommentModel.id)
    )
    for comment in comments:
        comments_by_note[comment.annotation_id].append(comment)
    origins = {note.id: _origin_id(note) for note in annotations}
    copies = {note.id: _copy_id(origins[note.id], target.id) for note in annotations}
    existing = set(
        session.scalars(
            select(AnnotationModel.id).where(
                AnnotationModel.paper_id == target.id,
                AnnotationModel.workspace_id == principal.active_workspace_id,
                AnnotationModel.creator_user_id == principal.user_id,
            )
        )
    )
    source_texts = {
        block.id: normalize_selector_text(block.source_text)
        for block in source_blocks
        if block.source_text
    }
    target_texts = [
        (block.id, normalize_selector_text(block.source_text))
        for block in target_blocks
        if block.source_text
    ]
    candidates = []
    for note in annotations:
        note_comments = comments_by_note[note.id]
        if not (note.is_highlighted or note.body.strip() or note_comments):
            continue  # An anchor used only by an AI question has no user annotation to copy.
        selector = note.selector_json
        exact = selector.get("exact_quote") if isinstance(selector, dict) else None
        quote = normalize_selector_text(exact) if isinstance(exact, str) else ""
        if copies[note.id] in existing or origins[note.id] in existing:
            status, match = "ALREADY_COPIED", None
        else:
            status, match = _find_match(note, quote, source_texts.get(note.block_id), target_texts)
        candidates.append(
            _Candidate(note, note_comments, quote, status, match, copies[note.id], origins[note.id])
        )
    return candidates


def _sections(
    blocks: list[ContentBlockModel],
) -> dict[tuple[str, int], tuple[str, list[tuple[str, str]]]]:
    levels = [block.heading_level or 1 for block in blocks if block.block_type == "SECTION"]
    if not levels:
        return {}
    top_level = min(levels)
    occurrences: dict[str, int] = defaultdict(int)
    sections = {}
    content = None
    for block in blocks:
        text = " ".join(normalize_selector_text(block.source_text or "").split())
        if block.block_type == "SECTION" and (block.heading_level or 1) == top_level:
            title = text.casefold()
            key = (title, occurrences[title])
            occurrences[title] += 1
            content = []
            sections[key] = (text, content)
        if content is not None:
            content.append((block.block_type, text))
    return sections


def _section_changes(
    source_blocks: list[ContentBlockModel], target_blocks: list[ContentBlockModel]
) -> list[AnnotationMigrationSection]:
    before = _sections(source_blocks)
    after = _sections(target_blocks)
    changes = []
    for key, (title, content) in after.items():
        if key not in before:
            changes.append(AnnotationMigrationSection(title=title, status="ADDED"))
        elif content != before[key][1]:
            changes.append(AnnotationMigrationSection(title=title, status="CHANGED"))
    for key, (title, _) in before.items():
        if key not in after:
            changes.append(AnnotationMigrationSection(title=title, status="REMOVED"))
    return changes


def _copy_annotation(
    session: Session, principal: RequestPrincipal, target_id: str, candidate: _Candidate
) -> None:
    assert candidate.match is not None
    block, text, start, end = (
        candidate.match.block_id,
        candidate.match.text,
        candidate.match.start,
        candidate.match.end,
    )
    original = candidate.annotation
    session.add(
        AnnotationModel(
            id=candidate.copy_id,
            workspace_id=principal.active_workspace_id,
            creator_user_id=principal.user_id,
            paper_id=target_id,
            block_id=block,
            annotation_type=AnnotationType.ANNOTATION,
            target=AnnotationTarget.SOURCE,
            creator_type="USER",
            body=original.body,
            is_highlighted=original.is_highlighted,
            selector_json={
                "version": SELECTOR_VERSION,
                "block_id": block,
                "start_offset": start,
                "end_offset": end,
                "exact_quote": text[start:end],
                "prefix": text[max(0, start - MAX_CONTEXT_CODEPOINTS) : start],
                "suffix": text[end : end + MAX_CONTEXT_CODEPOINTS],
                "text_hash": selector_text_hash(text),
                "migration_origin_annotation_id": candidate.origin_id,
            },
            resolution_status=AnnotationResolutionStatus.RESOLVED_OFFSETS,
            resolved_start_offset=start,
            resolved_end_offset=end,
            resolved_at=now_utc(),
            comments=[
                AnnotationCommentModel(
                    workspace_id=principal.active_workspace_id,
                    creator_user_id=principal.user_id,
                    body=comment.body,
                    created_at=comment.created_at,
                )
                for comment in candidate.comments
            ],
        )
    )


@router.get("/papers/{paper_id}/annotation-migrations", response_model=AnnotationMigrationPreview)
def preview_annotation_migration(
    paper_id: str,
    target_paper_id: Annotated[str, Query(min_length=1, max_length=36)],
    request: Request,
) -> AnnotationMigrationPreview:
    principal = require_request_permission(request, Permission.ANNOTATION_READ)
    with request.app.state.session_factory() as session:
        source, target = _editions(session, principal, paper_id, target_paper_id)
        before = _load_blocks(session, source.id)
        after = _load_blocks(session, target.id)
        return AnnotationMigrationPreview(
            candidates=[
                entry.response()
                for entry in _candidates(session, principal, source, target, before, after)
            ],
            sections=_section_changes(before, after),
        )


@router.post(
    "/papers/{paper_id}/annotation-migrations", response_model=AnnotationMigrationCopyResponse
)
def copy_annotations_between_editions(
    paper_id: str,
    payload: AnnotationMigrationCopyRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> AnnotationMigrationCopyResponse:
    if not idempotency_key:
        raise HTTPException(428, detail={"code": "IDEMPOTENCY_KEY_REQUIRED"})
    principal = require_request_permission(request, Permission.ANNOTATION_READ)
    require_request_permission(request, Permission.ANNOTATION_WRITE)
    requested = list(dict.fromkeys(payload.source_annotation_ids))
    with request.app.state.session_factory() as session:
        # Keep the re-check and copy atomic, even with distinct idempotency keys. PostgreSQL
        # callers lock common papers in the same order; SQLite serializes its write transaction.
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        source, target = _editions(session, principal, paper_id, payload.target_paper_id)
        if session.bind is not None and session.bind.dialect.name != "sqlite":
            session.scalars(
                select(PaperModel.id)
                .where(PaperModel.id.in_([source.id, target.id]))
                .order_by(PaperModel.id)
                .with_for_update()
            ).all()
        before = _load_blocks(session, source.id)
        after = _load_blocks(session, target.id)
        candidates = _candidates(session, principal, source, target, before, after, set(requested))
        matches = {
            item.annotation.id: item
            for item in candidates
            if item.status == "MATCHED"
        }
        copied = []
        copied_ids: set[str] = set()
        skipped = []
        for annotation_id in requested:
            candidate = matches.get(annotation_id)
            if candidate is None or candidate.copy_id in copied_ids:
                skipped.append(annotation_id)
                continue
            _copy_annotation(session, principal, target.id, candidate)
            copied.append(candidate.copy_id)
            copied_ids.add(candidate.copy_id)
        session.commit()
    return AnnotationMigrationCopyResponse(
        copied_annotation_ids=copied,
        skipped_source_annotation_ids=skipped,
    )
