"""Stable v2 transport facade over the shared application/domain services.

The v1 graph remains intact for an M7 compatibility cycle. Per-method route copies give
GET/HEAD assets unique operation IDs without buffering or duplicating storage implementations.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.v1 import (
    admin_invitations_router,
    admin_users_router,
    annotations_router,
    credentials_router,
    me_router,
    onboarding_router,
    paper_maps_router,
    papers,
    papers_router,
    runtime_router,
    settings_router,
    setup_router,
)
from app.api.v1.runtime import runtime_capabilities
from app.domain.auth import Permission
from app.infrastructure.db.models import PaperSourceModel


def _operation_id(method: str, path: str) -> str:
    return "v2_" + method.lower() + "_" + re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")


def include_versioned(router: APIRouter, source: APIRouter) -> None:
    for route in source.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.endpoint in {papers.retry_paper, runtime_capabilities}:
            # The v2 compatibility path must share the same paid retry contract as /jobs.
            continue
        for method in sorted(route.methods):
            cloned = copy.copy(route)
            cloned.methods = {method}
            cloned.operation_id = _operation_id(method, route.path)
            cloned.unique_id = cloned.operation_id
            cloned.tags = ["v2", *route.tags]
            router.routes.append(cloned)


resources = APIRouter(dependencies=[Depends(resolve_request_principal)])


@resources.get("/papers/{paper_id}/sources")
def sources(paper_id: str, request: Request):
    require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        papers._require_paper(session, request, paper_id)
        rows = session.scalars(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == paper_id,
            )
        ).all()
        return {
            "items": [
                {
                    "source_type": row.source_type,
                    "sha256": row.sha256,
                    "byte_size": row.byte_size,
                    "mime_type": row.mime_type,
                    "download_url": f"/api/v2/papers/{paper_id}/sources/{row.source_type}/download",
                }
                for row in rows
            ]
        }


@resources.get("/papers/{paper_id}/assets")
def assets(
    paper_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
):
    require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        papers._require_paper(session, request, paper_id)
        source = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
        if source is None:
            return {"items": [], "next_cursor": None}
        with request.app.state.storage.get(source.storage_key) as stream:
            raw = stream.read(8_000_001)
        if len(raw) > 8_000_000:
            raise HTTPException(413, detail={"code": "ASSET_MANIFEST_TOO_LARGE"})
        try:
            manifest = json.loads(raw)
            files = sorted(manifest["files"], key=lambda item: item["path"])
            offset = 0
            if cursor:
                revision, offset = json.loads(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                )
                if revision != source.sha256:
                    raise HTTPException(409, detail={"code": "ASSET_MANIFEST_CHANGED"})
                if type(offset) is not int or not 0 <= offset <= len(files):
                    raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise HTTPException(400, detail={"code": "INVALID_ASSET_CURSOR"}) from None
        output = []
        for item in files[offset : offset + limit]:
            path = item["path"]
            token = base64.urlsafe_b64encode(f"{paper_id}\0{path}".encode()).decode().rstrip("=")
            output.append(
                {
                    "id": token,
                    "path": path,
                    "byte_size": item.get("byte_size"),
                    "mime_type": item.get("mime_type"),
                    "content_url": f"/api/v2/assets/{token}/content",
                }
            )
        next_offset = offset + len(output)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [
                        source.sha256,
                        next_offset,
                    ]
                ).encode()
            )
            .decode()
            .rstrip("=")
            if next_offset < len(files)
            else None
        )
        return {"items": output, "next_cursor": next_cursor}


@resources.api_route("/assets/{asset_id}/content", methods=["GET", "HEAD"])
def asset_content(asset_id: str, request: Request) -> Response:
    try:
        if len(asset_id) > 4096:
            raise ValueError
        paper_id, path = (
            base64.b64decode(asset_id + "=" * (-len(asset_id) % 4), altchars=b"-_", validate=True)
            .decode()
            .split("\0", 1)
        )
    except (ValueError, UnicodeError):
        raise HTTPException(404, detail={"code": "ASSET_NOT_FOUND"}) from None
    return papers.get_paper_asset(paper_id=paper_id, asset_path=path, request=request)


def build_api_v2_router() -> APIRouter:
    from app.api.v2_annotation_migrations import router as annotation_migrations
    from app.api.v2_auth import router as auth
    from app.api.v2_health import router as health
    from app.api.v2_jobs import router as jobs
    from app.api.v2_libraries import router as libraries
    from app.api.v2_offline import router as offline
    from app.api.v2_registration import router as registration
    from app.api.v2_server import router as server
    from app.api.v2_sync import router as sync

    result = APIRouter()
    # Shared domain routes keep their established DTO shapes for client compatibility; the new
    # resource families use their own bounded pagination and command contracts.
    for source in (
        auth,
        server,
        registration,
        libraries,
        jobs,
        sync,
        health,
        runtime_router,
        setup_router,
        me_router,
        onboarding_router,
        admin_users_router,
        admin_invitations_router,
        credentials_router,
        settings_router,
        papers_router,
        annotations_router,
        annotation_migrations,
        paper_maps_router,
        offline,
        resources,
    ):
        include_versioned(result, source)
    return result
