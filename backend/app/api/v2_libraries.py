"""Version-two library resources. Authentication is shared with the v2 transport adapter."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from app.api.dependencies import resolve_request_principal
from app.application.libraries import (
    ACTIONS,
    LibraryError,
    advance_revision,
    create_library,
    ensure_default_library,
    has_library_import_receipt,
    import_into_library,
    library_access_predicate,
    load_accessible_library_item,
    remove_entry,
    require_library,
    resolve_arxiv_import_version,
    revoke_grant,
    set_grant,
)
from app.domain.arxiv import (
    InvalidArxivIdentifier,
    arxiv_version_number,
    normalize_arxiv_identifier,
)
from app.domain.auth import Permission, RequestPrincipal
from app.infrastructure.db.library_models import LibraryEntryModel, LibraryGrantModel, LibraryModel
from app.infrastructure.db.models import LibraryItemModel, PaperModel, ProcessingTaskModel

router = APIRouter(tags=["libraries"], dependencies=[Depends(resolve_request_principal)])


class LibraryName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class GrantWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: bool = False
    import_: bool = Field(default=False, alias="import")
    manage: bool = False
    remove: bool = False


class LibraryImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=200)


class LibraryResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    is_default: bool
    revision: int = Field(ge=1)
    permissions: GrantWrite


class LibraryPage(BaseModel):
    items: list[LibraryResponse]
    next_cursor: str | None


class MemberResponse(BaseModel):
    user_id: str
    permissions: GrantWrite
    grant_source: str
    granted_by_user_id: str | None


class MemberPage(BaseModel):
    items: list[MemberResponse]
    revision: int
    next_cursor: str | None


class RevisionResponse(BaseModel):
    revision: int


class LibraryItemResponse(BaseModel):
    id: str
    paper_id: str
    library_id: str
    title: str
    arxiv_id: str
    arxiv_version: str
    processing_stage: str
    reading_status: str
    is_favorite: bool


class LibraryItemPage(BaseModel):
    items: list[LibraryItemResponse]
    next_cursor: str | None


class ArxivVersionsResponse(BaseModel):
    latest_version: str | None
    checked_at: datetime | None
    versions: list[LibraryItemResponse]


class LibraryImportResponse(BaseModel):
    item: LibraryItemResponse
    created: bool
    job_ids: list[str]


def _error(error: LibraryError):
    raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from error


def _principal(request: Request) -> RequestPrincipal:
    return request.state.request_principal


def _revision(if_match: str | None) -> int:
    if if_match is None:
        raise HTTPException(428, detail={"code": "IF_MATCH_REQUIRED"})
    try:
        if len(if_match) > 24 or not if_match.startswith('"') or not if_match.endswith('"'):
            raise ValueError
        result = int(if_match[1:-1])
        if result < 1:
            raise ValueError
        return result
    except ValueError:
        raise HTTPException(422, detail={"code": "IF_MATCH_INVALID"}) from None


def _cursor(cursor: str | None, scope: str) -> str | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 1024:
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if payload["scope"] != scope or not isinstance(payload["after"], str):
            raise ValueError
        return payload["after"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, detail={"code": "CURSOR_INVALID"}) from None


def _next_cursor(rows, limit: int, scope: str, key: str = "id"):
    if len(rows) <= limit:
        return None
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "scope": scope,
                    "after": getattr(rows[limit - 1], key),
                },
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )


def _library_response(session, library: LibraryModel, principal: RequestPrincipal):
    grant = session.get(LibraryGrantModel, (library.id, principal.user_id))
    permissions = {action: bool(grant and getattr(grant, "can_" + action)) for action in ACTIONS}
    return {
        "id": library.id,
        "workspace_id": library.workspace_id,
        "name": library.name,
        "is_default": library.key == "default",
        "revision": library.revision,
        "permissions": permissions,
    }


def _item_response(session, item, library_id):
    paper = session.get(PaperModel, item.paper_id)
    return {
        "id": item.id,
        "paper_id": item.paper_id,
        "library_id": library_id,
        "title": paper.title,
        "arxiv_id": paper.arxiv_id,
        "arxiv_version": paper.arxiv_version,
        "processing_stage": paper.processing_stage,
        "reading_status": item.reading_status,
        "is_favorite": item.is_favorite,
    }


@router.get("/libraries", response_model=LibraryPage)
def list_libraries(
    request: Request, limit: int = Query(50, ge=1, le=200), cursor: str | None = None
):
    principal = _principal(request)
    if not principal.has(Permission.LIBRARY_READ):
        raise HTTPException(403, detail={"code": "LIBRARY_ACCESS_DENIED"})
    scope = f"libraries:{principal.active_workspace_id}:{principal.user_id}"
    after = _cursor(cursor, scope)
    try:
        with request.app.state.session_factory() as session:
            ensure_default_library(session, principal)
            statement = (
                select(LibraryModel)
                .where(
                    or_(
                        library_access_predicate(principal),
                        library_access_predicate(principal, "manage"),
                    )
                )
                .order_by(LibraryModel.id)
                .limit(limit + 1)
            )
            if after:
                statement = statement.where(LibraryModel.id > after)
            rows = session.scalars(statement).all()
            result = {
                "items": [_library_response(session, row, principal) for row in rows[:limit]],
                "next_cursor": _next_cursor(rows, limit, scope),
            }
            session.commit()  # Persist a first-use default, never restore a revoked grant.
            return result
    except LibraryError as error:
        _error(error)


@router.post("/libraries", status_code=201, response_model=LibraryResponse)
def add_library(payload: LibraryName, request: Request, response: Response):
    principal = _principal(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, detail={"code": "LIBRARY_NAME_REQUIRED"})
    try:
        with request.app.state.session_factory() as session:
            ensure_default_library(session, principal)
            library = create_library(session, principal, name)
            result = _library_response(session, library, principal)
            response.headers["ETag"] = f'"{library.revision}"'
            session.commit()
            return result
    except LibraryError as error:
        _error(error)


@router.get("/libraries/{library_id}", response_model=LibraryResponse)
def get_library(library_id: str, request: Request, response: Response):
    principal = _principal(request)
    try:
        with request.app.state.session_factory() as session:
            library = require_library(session, principal, library_id)
            response.headers["ETag"] = f'"{library.revision}"'
            return _library_response(session, library, principal)
    except LibraryError as error:
        _error(error)


@router.patch("/libraries/{library_id}", response_model=LibraryResponse)
def rename_library(
    library_id: str,
    payload: LibraryName,
    request: Request,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
):
    expected = _revision(if_match)
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, detail={"code": "LIBRARY_NAME_REQUIRED"})
    principal = _principal(request)
    try:
        with request.app.state.session_factory() as session:
            library = require_library(session, principal, library_id, "manage")
            advance_revision(session, library, expected)
            library.name = name
            session.flush()
            response.headers["ETag"] = f'"{library.revision}"'
            result = _library_response(session, library, principal)
            session.commit()
            return result
    except LibraryError as error:
        _error(error)


@router.get("/libraries/{library_id}/members", response_model=MemberPage)
def list_members(
    library_id: str, request: Request,
    limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
):
    principal = _principal(request)
    scope = f"members:{library_id}:{principal.user_id}"
    after = _cursor(cursor, scope)
    try:
        with request.app.state.session_factory() as session:
            library = require_library(session, principal, library_id, "manage")
            statement = select(LibraryGrantModel).where(
                LibraryGrantModel.library_id == library.id,
            ).order_by(LibraryGrantModel.user_id).limit(limit + 1)
            if after:
                statement = statement.where(LibraryGrantModel.user_id > after)
            grants = session.scalars(statement).all()
            return {
                "items": [{
                    "user_id": grant.user_id,
                    "permissions": {action: getattr(grant, "can_" + action) for action in ACTIONS},
                    "grant_source": grant.grant_source,
                    "granted_by_user_id": grant.granted_by_user_id,
                } for grant in grants[:limit]],
                "revision": library.revision,
                "next_cursor": _next_cursor(grants, limit, scope, "user_id"),
            }
    except LibraryError as error:
        _error(error)


@router.put("/libraries/{library_id}/members/{user_id}", response_model=RevisionResponse)
def write_member(
    library_id: str,
    user_id: str,
    payload: GrantWrite,
    request: Request,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
):
    try:
        with request.app.state.session_factory() as session:
            library = set_grant(
                session,
                _principal(request),
                library_id,
                user_id,
                payload.model_dump(by_alias=True),
                expected_revision=_revision(if_match),
            )
            response.headers["ETag"] = f'"{library.revision}"'
            session.commit()
            return {"revision": library.revision}
    except LibraryError as error:
        _error(error)


@router.delete("/libraries/{library_id}/members/{user_id}", status_code=204)
def delete_member(
    library_id: str,
    user_id: str,
    request: Request,
    if_match: Annotated[str | None, Header()] = None,
):
    try:
        with request.app.state.session_factory() as session:
            library = revoke_grant(
                session,
                _principal(request),
                library_id,
                user_id,
                expected_revision=_revision(if_match),
            )
            session.commit()
            return Response(status_code=204, headers={"ETag": f'"{library.revision}"'})
    except LibraryError as error:
        _error(error)


@router.get("/libraries/{library_id}/items", response_model=LibraryItemPage)
def list_items(
    library_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
):
    principal = _principal(request)
    scope = f"items:{library_id}:{principal.user_id}"
    after = _cursor(cursor, scope)
    try:
        with request.app.state.session_factory() as session:
            require_library(session, principal, library_id)
            statement = (
                select(LibraryItemModel)
                .join(
                    LibraryEntryModel,
                    LibraryEntryModel.library_item_id == LibraryItemModel.id,
                )
                .where(
                    LibraryEntryModel.library_id == library_id,
                    LibraryItemModel.workspace_id == principal.active_workspace_id,
                )
                .order_by(LibraryItemModel.id)
                .limit(limit + 1)
            )
            if after:
                statement = statement.where(LibraryItemModel.id > after)
            rows = session.scalars(statement).all()
            return {
                "items": [_item_response(session, row, library_id) for row in rows[:limit]],
                "next_cursor": _next_cursor(rows, limit, scope),
            }
    except LibraryError as error:
        _error(error)


@router.get(
    "/libraries/{library_id}/items/{item_id}/arxiv-versions",
    response_model=ArxivVersionsResponse,
)
def get_arxiv_versions(
    library_id: str, item_id: str, request: Request, check: bool = False,
):
    principal = _principal(request)

    def read_editions():
        with request.app.state.session_factory() as session:
            require_library(session, principal, library_id)
            items = (
                select(LibraryItemModel)
                .join(PaperModel, LibraryItemModel.paper_id == PaperModel.id)
                .join(LibraryEntryModel, LibraryEntryModel.library_item_id == LibraryItemModel.id)
                .where(
                    LibraryEntryModel.library_id == library_id,
                    LibraryItemModel.workspace_id == principal.active_workspace_id,
                )
            )
            current = session.scalar(items.where(LibraryItemModel.id == item_id))
            if current is None:
                raise LibraryError("LIBRARY_ITEM_NOT_FOUND")
            arxiv_id = current.paper.arxiv_id
            rows = session.scalars(
                items.where(func.lower(PaperModel.arxiv_id) == arxiv_id.lower())
            ).all()
            if not any(row.id == item_id for row in rows):
                raise LibraryError("LIBRARY_ITEM_NOT_FOUND")
            return arxiv_id, [_item_response(session, row, library_id) for row in rows]

    try:
        arxiv_id, versions = read_editions()
        try:
            base = normalize_arxiv_identifier(arxiv_id)
        except InvalidArxivIdentifier:
            base = None  # Historic records may have identifiers arXiv no longer accepts.
        known = (
            request.app.state.arxiv_versions.lookup(base.arxiv_id, check=check)
            if base is not None and not base.version
            else None
        )
        if check and base is not None and not base.version:
            # Network checks run outside the DB session. Check the current grant and
            # entries again before releasing a result from the earlier snapshot.
            confirmed_arxiv_id, versions = read_editions()
            if confirmed_arxiv_id.casefold() != arxiv_id.casefold():
                known = None
    except LibraryError as error:
        _error(error)

    versions.sort(
        key=lambda version: (
            -arxiv_version_number(version["arxiv_version"]),
            version["id"],
        )
    )
    latest = max(
        (known.version if known else "", versions[0]["arxiv_version"]),
        key=arxiv_version_number,
    )
    return ArxivVersionsResponse(
        latest_version=latest if arxiv_version_number(latest) else None,
        checked_at=known.checked_at if known else None,
        versions=versions,
    )


@router.post(
    "/libraries/{library_id}/imports", status_code=202, response_model=LibraryImportResponse,
)
def import_item(
    library_id: str,
    payload: LibraryImport,
    request: Request,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
):
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(428, detail={"code": "IDEMPOTENCY_KEY_REQUIRED"})
    principal = _principal(request)
    try:
        identifier = normalize_arxiv_identifier(payload.source)
        resolved_version = None
        if not identifier.version:
            # Authorize and check historical receipts before upstream I/O. No database
            # transaction is held during the metadata request.
            with request.app.state.session_factory() as session:
                replay = has_library_import_receipt(
                    session, principal, library_id, identifier, idempotency_key
                )
            if not replay:
                resolved_version = resolve_arxiv_import_version(
                    identifier, request.app.state.arxiv_versions
                )
        with request.app.state.session_factory() as session:
            item, created = import_into_library(
                session,
                principal,
                library_id,
                payload.source,
                idempotency_key=idempotency_key,
                resolved_version=resolved_version,
            )
            jobs = session.scalars(
                select(ProcessingTaskModel.id).where(
                    ProcessingTaskModel.paper_id == item.paper_id,
                    ProcessingTaskModel.task_type.in_(["FETCH_METADATA", "FETCH_SOURCE"]),
                )
            ).all()
            result = {
                "item": _item_response(session, item, library_id),
                "created": created,
                "job_ids": list(jobs),
            }
            session.commit()
            return result
    except LibraryError as error:
        _error(error)
    except InvalidArxivIdentifier:
        raise HTTPException(422, detail={"code": "ARXIV_IDENTIFIER_INVALID"}) from None


@router.get("/library-items/{item_id}", response_model=LibraryItemResponse)
def get_item(item_id: str, request: Request, library_id: str | None = None):
    principal = _principal(request)
    with request.app.state.session_factory() as session:
        item = load_accessible_library_item(session, principal, item_id)
        if item is None:
            raise HTTPException(404, detail={"code": "LIBRARY_ITEM_NOT_FOUND"})
        statement = (
            select(LibraryModel.id)
            .join(
                LibraryEntryModel,
                LibraryEntryModel.library_id == LibraryModel.id,
            )
            .where(
                LibraryEntryModel.library_item_id == item.id,
                library_access_predicate(principal),
            )
            .order_by(LibraryModel.id)
        )
        if library_id:
            statement = statement.where(LibraryModel.id == library_id)
        visible_library = session.scalar(statement)
        if not visible_library:
            raise HTTPException(404, detail={"code": "LIBRARY_ITEM_NOT_FOUND"})
        return _item_response(session, item, visible_library)


@router.delete("/library-items/{item_id}", status_code=204)
def delete_item(
    item_id: str,
    request: Request,
    library_id: str = Query(...),
    if_match: Annotated[str | None, Header()] = None,
):
    try:
        with request.app.state.session_factory() as session:
            library = remove_entry(
                session,
                _principal(request),
                library_id,
                item_id,
                expected_revision=_revision(if_match),
            )
            session.commit()
            return Response(status_code=204, headers={"ETag": f'"{library.revision}"'})
    except LibraryError as error:
        _error(error)
