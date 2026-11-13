from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infrastructure.db.models import (
    ContentBlockModel,
    WorkspaceBlockRenderOverlayModel,
)


@dataclass(frozen=True, slots=True)
class EffectiveBlockRender:
    payload: dict[str, Any]
    overlay: WorkspaceBlockRenderOverlayModel | None


def load_workspace_render_overlay(
    session: Session,
    *,
    workspace_id: str,
    block: ContentBlockModel,
) -> WorkspaceBlockRenderOverlayModel | None:
    """Load only the overlay derived from the block's current canonical revision."""

    return session.scalar(
        select(WorkspaceBlockRenderOverlayModel).where(
            WorkspaceBlockRenderOverlayModel.workspace_id == workspace_id,
            WorkspaceBlockRenderOverlayModel.block_id == block.id,
            WorkspaceBlockRenderOverlayModel.base_content_hash == block.content_hash,
        )
    )


def effective_block_render(
    session: Session,
    *,
    workspace_id: str,
    block: ContentBlockModel,
) -> EffectiveBlockRender:
    overlay = load_workspace_render_overlay(
        session,
        workspace_id=workspace_id,
        block=block,
    )
    source = overlay.render_payload_json if overlay is not None else block.render_payload_json
    return EffectiveBlockRender(
        payload=deepcopy(source) if isinstance(source, dict) else {},
        overlay=overlay,
    )


def save_workspace_render_overlay(
    session: Session,
    *,
    workspace_id: str,
    requested_by_user_id: str,
    block: ContentBlockModel,
    render_payload: dict[str, Any],
    metadata: dict[str, Any],
) -> WorkspaceBlockRenderOverlayModel:
    """Create or replace the current workspace render view for one immutable base hash."""

    overlay = load_workspace_render_overlay(
        session,
        workspace_id=workspace_id,
        block=block,
    )
    if overlay is None:
        overlay = WorkspaceBlockRenderOverlayModel(
            workspace_id=workspace_id,
            block_id=block.id,
            requested_by_user_id=requested_by_user_id,
            base_content_hash=block.content_hash,
            render_payload_json=deepcopy(render_payload),
            metadata_json=deepcopy(metadata),
        )
        session.add(overlay)
    else:
        overlay.requested_by_user_id = requested_by_user_id
        overlay.render_payload_json = deepcopy(render_payload)
        overlay.metadata_json = deepcopy(metadata)
    session.flush()
    return overlay


def workspace_render_asset_key(
    *,
    workspace_id: str,
    paper_id: str,
    asset_directory: str,
    asset_name: str,
) -> str:
    return (
        f"workspaces/{workspace_id}/papers/{paper_id}/derived/"
        f"{asset_directory}/{asset_name}"
    )
