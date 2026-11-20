"""Revision probes for the native reader's per-account offline snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.llm_credentials import request_llm_task_credential
from app.api.v1 import paper_maps, papers
from app.application.access import load_workspace_paper
from app.application.paper_maps import current_paper_map
from app.domain.auth import Permission, RequestPrincipal
from app.infrastructure.db.models import (
    ContentBlockModel,
    PaperModel,
    TranslationModel,
    WorkspaceBlockRenderOverlayModel,
)

router = APIRouter(dependencies=[Depends(resolve_request_principal)])


class PaperOfflineRevisionResponse(BaseModel):
    paper: str
    translations: str
    map: str


def _hash_revision(*values: object) -> str:
    def timestamp(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Unsupported revision value: {type(value).__name__}")

    payload = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=timestamp
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _paper_revision(
    session: Session, paper: PaperModel, workspace_id: str, toolchain_fingerprint: str | None
) -> str:
    blocks = session.execute(
        select(
            ContentBlockModel.id,
            ContentBlockModel.ordinal_path,
            ContentBlockModel.parent_id,
            ContentBlockModel.block_type,
            ContentBlockModel.heading_level,
            ContentBlockModel.content_hash,
            ContentBlockModel.updated_at,
            WorkspaceBlockRenderOverlayModel.id,
            WorkspaceBlockRenderOverlayModel.updated_at,
        )
        .outerjoin(
            WorkspaceBlockRenderOverlayModel,
            (WorkspaceBlockRenderOverlayModel.workspace_id == workspace_id)
            & (WorkspaceBlockRenderOverlayModel.block_id == ContentBlockModel.id)
            & (
                WorkspaceBlockRenderOverlayModel.base_content_hash == ContentBlockModel.content_hash
            ),
        )
        .where(ContentBlockModel.paper_id == paper.id)
        .order_by(ContentBlockModel.ordinal_path, ContentBlockModel.id)
    )
    return _hash_revision(
        "paper-1",
        toolchain_fingerprint,
        paper.id,
        paper.arxiv_id,
        paper.arxiv_version,
        paper.title,
        paper.abstract,
        paper.submitted_at,
        paper.processing_stage,
        paper.processing_health,
        paper.updated_at,
        [(author.position, author.name) for author in paper.authors],
        sorted(
            (source.source_type, source.sha256, source.byte_size, source.mime_type)
            for source in paper.sources
        ),
        [tuple(row) for row in blocks],
    )


def _translations_revision(
    session: Session, request: Request, paper_id: str, principal: RequestPrincipal
) -> str:
    settings = request.app.state.settings
    translations = session.execute(
        select(
            TranslationModel.id,
            TranslationModel.block_id,
            TranslationModel.input_hash,
            TranslationModel.created_at,
            TranslationModel.updated_at,
            TranslationModel.status,
            TranslationModel.provider,
            TranslationModel.model,
        )
        .join(ContentBlockModel, ContentBlockModel.id == TranslationModel.block_id)
        .where(
            ContentBlockModel.paper_id == paper_id,
            ContentBlockModel.content_hash == TranslationModel.source_hash,
            TranslationModel.language == settings.translation_target_language,
            papers._translation_visibility_predicate(principal),
        )
        .order_by(TranslationModel.id)
    )
    return _hash_revision(
        "translations-1",
        settings.toolchain_fingerprint,
        settings.translation_target_language,
        settings.translation_configured,
        settings.llm_provider,
        settings.llm_model,
        [tuple(row) for row in translations],
    )


def _map_revision(
    session: Session, request: Request, paper_id: str, principal: RequestPrincipal
) -> str:
    if not principal.has(Permission.PAPER_MAP_READ):
        return _hash_revision("map-1", "unavailable")
    current = current_paper_map(session, paper_id, workspace_id=principal.active_workspace_id)
    paper_map = (
        session.scalar(paper_maps._map_query(current.id, principal.active_workspace_id))
        if current is not None
        else None
    )
    credential = request_llm_task_credential(request, user_id=principal.user_id, required=False)
    payload = paper_maps._serialize_map(paper_map).model_dump(mode="json") if paper_map else None
    if payload is not None:
        # The relationship loader does not guarantee row order for nodes and edges.
        for key in ("questions", "nodes", "edges"):
            payload[key].sort(key=lambda item: item["id"])
    return _hash_revision(
        "map-1", credential is not None, credential.provider if credential else None,
        credential.model if credential else None, payload,
    )


@router.get("/papers/{paper_id}/offline-revision", response_model=PaperOfflineRevisionResponse)
def paper_offline_revision(paper_id: str, request: Request) -> PaperOfflineRevisionResponse:
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        entry = load_workspace_paper(session, principal, paper_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        return PaperOfflineRevisionResponse(
            paper=_paper_revision(
                session,
                entry.paper,
                principal.active_workspace_id,
                request.app.state.settings.toolchain_fingerprint,
            ),
            translations=_translations_revision(session, request, paper_id, principal),
            map=_map_revision(session, request, paper_id, principal),
        )
