"""Durable scoped resource changes, user reading state and read-only integration tokens."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.v2_jobs import begin_write
from app.application.libraries import (
    ensure_default_library,
    load_accessible_library_item,
    paper_access_predicate,
)
from app.domain.auth import Permission, RequestPrincipal
from app.infrastructure.db.library_models import LibraryGrantModel, LibraryModel
from app.infrastructure.db.models import (
    ContentBlockModel,
    InstallationStateModel,
    PaperModel,
    UserModel,
)
from app.infrastructure.db.sync_models import (
    IntegrationTokenModel,
    ReadingStateModel,
    ResourceChangeModel,
    SyncStateModel,
)
from app.infrastructure.identity import (
    AuthenticatedDatabaseIdentityProvider,
    IdentityStateError,
    UntrustedServiceIdentityError,
)

router = APIRouter(tags=["v2-sync"])

IntegrationReadScope = Literal["library:read", "jobs:read"]


class ResourceChangeResponse(BaseModel):
    resource_type: Literal[
        "job", "paper", "library_item", "annotation", "reading_state", "integration_token", "access"
    ]
    resource_id: str
    change_type: Literal["insert", "update", "delete", "access_changed"]
    revision: int | None
    status: str | None
    created_at: datetime


class ChangesResponse(BaseModel):
    items: list[ResourceChangeResponse]
    next_cursor: str = Field(
        description="Opaque authenticated cursor bound to the current user and scope"
    )
    has_more: bool
    full_sync_required: bool = False


class ReadingStateResponse(BaseModel):
    library_item_id: str
    block_id: str | None
    progress: float = Field(ge=0, le=1)
    revision: int = Field(ge=0)
    updated_at: datetime | None


class IntegrationTokenMetadataResponse(BaseModel):
    id: str
    label: str
    scopes: list[IntegrationReadScope]
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class IntegrationTokenListResponse(BaseModel):
    items: list[IntegrationTokenMetadataResponse]


class IntegrationTokenIssueResponse(IntegrationTokenMetadataResponse):
    token: str = Field(
        repr=False,
        description="Shown once at creation; never returned by list or detail APIs",
        json_schema_extra={"format": "password"},
    )


INTEGRATION_BINDING_SCOPE_KEY = "academatepaper.integration-binding.v2"
INTEGRATION_SCOPES = frozenset({"library:read", "jobs:read"})
CURSOR_TTL_SECONDS = 86400


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def digest_integration_token(raw: str) -> str:
    return (
        "sha256-i2:"
        + hashlib.sha256(b"academatepaper.integration-token.v2\0" + raw.encode("ascii")).hexdigest()
    )


@dataclass(frozen=True, slots=True)
class VerifiedIntegrationBinding:
    token_id: str
    user_id: str
    active_workspace_id: str
    installation_state_version: int
    auth_generation: int
    scopes: frozenset[str]


def _integration_in_session(session: Session, token_id: str, now: datetime):
    token = session.get(IntegrationTokenModel, token_id)
    installation = session.get(InstallationStateModel, "installation")
    user = session.get(UserModel, token.user_id) if token else None
    if (
        token is None
        or token.revoked_at is not None
        or aware(token.expires_at) <= now
        or installation is None
        or installation.state != "READY"
        or installation.edition != "SERVER"
        or token.installation_state_version != installation.state_version
        or user is None
        or user.status != "ACTIVE"
        or user.auth_generation != token.auth_generation
        or not token.scopes_json
        or not set(token.scopes_json) <= INTEGRATION_SCOPES
    ):
        raise HTTPException(401, detail={"code": "INTEGRATION_TOKEN_INVALID"})
    return token


def verify_integration_token(raw: str, session_factory) -> VerifiedIntegrationBinding:
    if not raw.startswith("ap_i2_") or not 40 <= len(raw) <= 128 or not raw.isascii():
        raise HTTPException(401, detail={"code": "INTEGRATION_TOKEN_INVALID"})
    digest = digest_integration_token(raw)
    with session_factory() as session:
        row = session.scalar(
            select(IntegrationTokenModel).where(
                IntegrationTokenModel.digest == digest,
            )
        )
        if row is None or not secrets.compare_digest(row.digest, digest):
            raise HTTPException(401, detail={"code": "INTEGRATION_TOKEN_INVALID"})
        token = _integration_in_session(session, row.id, datetime.now(UTC))
        binding = VerifiedIntegrationBinding(
            token.id,
            token.user_id,
            token.workspace_id,
            token.installation_state_version,
            token.auth_generation,
            frozenset(token.scopes_json),
        )
        # Verify current membership, single-user invariants and onboarding through the same
        # database identity policy as native sessions. The binding is never an owner shortcut.
        try:
            _integration_principal(session, session_factory, token)
        except (IdentityStateError, UntrustedServiceIdentityError):
            raise HTTPException(401, detail={"code": "INTEGRATION_TOKEN_INVALID"}) from None
        return binding


def _integration_principal(session, factory, token) -> RequestPrincipal:
    principal = AuthenticatedDatabaseIdentityProvider(factory).resolve_principal_in_session(
        session,
        user_id=token.user_id,
        workspace_id=token.workspace_id,
        auth_generation=token.auth_generation,
    )
    allowed: set[Permission] = set()
    if "library:read" in token.scopes_json:
        allowed.add(Permission.LIBRARY_READ)
    if "jobs:read" in token.scopes_json:
        allowed.add(Permission.TASK_READ)
    return replace(principal, permission_set=principal.permission_set & allowed)


def resolve_integration_binding(binding: VerifiedIntegrationBinding, session_factory):
    if not isinstance(binding, VerifiedIntegrationBinding):
        raise HTTPException(401, detail={"code": "INTEGRATION_TOKEN_INVALID"})
    with session_factory() as session:
        token = _integration_in_session(session, binding.token_id, datetime.now(UTC))
        if (
            token.user_id != binding.user_id
            or token.workspace_id != binding.active_workspace_id
            or token.auth_generation != binding.auth_generation
            or token.installation_state_version != binding.installation_state_version
            or frozenset(token.scopes_json) != binding.scopes
        ):
            raise HTTPException(401, detail={"code": "INTEGRATION_TOKEN_INVALID"})
        try:
            return _integration_principal(session, session_factory, token)
        except (IdentityStateError, UntrustedServiceIdentityError):
            raise HTTPException(401, detail={"code": "INTEGRATION_TOKEN_INVALID"}) from None


def append_change(
    session: Session,
    *,
    resource_type: str,
    resource_id: str,
    change_type: str = "update",
    workspace_id: str | None = None,
    user_id: str | None = None,
    paper_id: str | None = None,
    revision: int | None = None,
    status: str | None = None,
) -> None:
    """Call inside the resource's transaction. IDs/versions only; never accepts a payload."""
    if resource_type not in {
        "job",
        "paper",
        "library_item",
        "annotation",
        "reading_state",
        "integration_token",
        "access",
    } or change_type not in {"insert", "update", "delete"}:
        raise ValueError("unsupported resource change")
    session.add(
        ResourceChangeModel(
            resource_type=resource_type,
            resource_id=resource_id,
            change_type=change_type,
            workspace_id=workspace_id,
            user_id=user_id,
            paper_id=paper_id,
            revision=revision,
            status=status,
        )
    )


def prune_changes(session: Session, *, retained_after: int) -> None:
    """Operator retention hook; advance floor atomically so expired cursors yield 410."""
    state = session.get(SyncStateModel, "events")
    maximum = session.scalar(select(func.max(ResourceChangeModel.id))) or 0
    if retained_after < 0 or retained_after > maximum:
        raise ValueError("invalid retention watermark")
    if state is None:
        state = SyncStateModel(singleton_key="events", retained_after=0)
        session.add(state)
    if retained_after < state.retained_after:
        raise ValueError("retention floor cannot move backwards")
    session.execute(delete(ResourceChangeModel).where(ResourceChangeModel.id <= retained_after))
    state.retained_after = retained_after


def _cursor_key(request: Request) -> bytes:
    token = request.app.state.settings.internal_service_token
    if token is None:
        raise HTTPException(503, detail={"code": "SYNC_CONFIGURATION_UNAVAILABLE"})
    return hashlib.sha256(
        b"academatepaper.sync-cursor.v2\0" + token.get_secret_value().encode()
    ).digest()


def _scope_stamp(session: Session, principal: RequestPrincipal) -> str:
    grants = session.execute(
        select(
            LibraryModel.id,
            LibraryModel.revision,
            LibraryGrantModel.can_read,
            LibraryGrantModel.can_import,
            LibraryGrantModel.can_manage,
            LibraryGrantModel.can_remove,
        )
        .join(LibraryGrantModel, LibraryGrantModel.library_id == LibraryModel.id)
        .where(
            LibraryModel.workspace_id == principal.active_workspace_id,
            LibraryGrantModel.user_id == principal.user_id,
        )
        .order_by(LibraryModel.id)
    ).all()
    raw = json.dumps(
        [
            sorted(p.value for p in principal.permission_set),
            [
                (
                    row.id,
                    # Metadata changes matter only while the library is visible. Keeping the
                    # caller's grant bits still detects access gains/losses without revealing
                    # subsequent activity in an already-revoked private library.
                    row.revision if row.can_read or row.can_manage else None,
                    bool(row.can_read),
                    bool(row.can_import),
                    bool(row.can_manage),
                    bool(row.can_remove),
                )
                for row in grants
            ],
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _encode_cursor(request: Request, principal: RequestPrincipal, after: int, stamp: str) -> str:
    payload = json.dumps(
        {
            "v": 2,
            "user": principal.user_id,
            "workspace": principal.active_workspace_id,
            "after": after,
            "scope": stamp,
            "expires": int(time.time()) + CURSOR_TTL_SECONDS,
        },
        separators=(",", ":"),
    ).encode()
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_cursor_key(request)).encrypt(nonce, payload, b"academatepaper.sync.v2")
    return "c2_" + base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")


def _decode_cursor(request, principal, value: str | None, stamp: str, floor: int) -> int:
    if not value:
        if floor:
            raise HTTPException(
                410, detail={"code": "SYNC_CURSOR_EXPIRED", "full_sync_required": True}
            )
        return 0
    try:
        if len(value) > 2048:
            raise ValueError("oversized")
        if not value.startswith("c2_"):
            raise ValueError("unsupported cursor")
        encoded = value[3:]
        encrypted = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = AESGCM(_cursor_key(request)).decrypt(
            encrypted[:12],
            encrypted[12:],
            b"academatepaper.sync.v2",
        )
        data = json.loads(payload)
        if (
            data["v"] != 2
            or data["user"] != principal.user_id
            or data["workspace"] != principal.active_workspace_id
            or type(data["after"]) is not int
            or data["after"] < 0
        ):
            raise ValueError("scope")
        if data["expires"] < time.time() or data["after"] < floor or data["scope"] != stamp:
            raise HTTPException(
                410, detail={"code": "SYNC_CURSOR_EXPIRED", "full_sync_required": True}
            )
        return data["after"]
    except (ValueError, TypeError, KeyError, UnicodeError, InvalidTag):
        raise HTTPException(400, detail={"code": "INVALID_CURSOR"}) from None


def _change_predicate(principal: RequestPrincipal):
    visible_paper = exists(
        select(PaperModel.id).where(
            PaperModel.id == ResourceChangeModel.paper_id,
            paper_access_predicate(principal),
        )
    )
    return and_(
        or_(
            ResourceChangeModel.workspace_id.is_(None),
            ResourceChangeModel.workspace_id == principal.active_workspace_id,
        ),
        or_(
            ResourceChangeModel.user_id.is_(None), ResourceChangeModel.user_id == principal.user_id
        ),
        or_(
            and_(ResourceChangeModel.paper_id.is_not(None), visible_paper),
            and_(
                ResourceChangeModel.resource_type == "library_item",
                ResourceChangeModel.change_type.in_(["delete", "access_changed"]),
                ResourceChangeModel.user_id == principal.user_id,
            ),
            and_(
                ResourceChangeModel.paper_id.is_(None),
                ResourceChangeModel.user_id == principal.user_id,
            ),
        ),
    )


def _change_page(
    request: Request,
    principal: RequestPrincipal,
    cursor: str | None,
    limit: int,
    *,
    reset: bool = False,
) -> dict:
    if not principal.has(Permission.LIBRARY_READ):
        raise HTTPException(403, detail={"code": "SYNC_READ_REQUIRED"})
    with request.app.state.session_factory() as session:
        ensure_default_library(session, principal)
        stamp = _scope_stamp(session, principal)
        state = session.get(SyncStateModel, "events")
        floor = state.retained_after if state else 0
        maximum = session.scalar(select(func.max(ResourceChangeModel.id))) or floor
        if reset:
            return {
                "items": [],
                "next_cursor": _encode_cursor(request, principal, maximum, stamp),
                "has_more": False,
                "full_sync_required": True,
            }
        after = _decode_cursor(request, principal, cursor, stamp, floor)
        if after > maximum:
            raise HTTPException(400, detail={"code": "INVALID_CURSOR"})
        statement = select(ResourceChangeModel).where(
            ResourceChangeModel.id > after,
            _change_predicate(principal),
        )
        if not principal.has(Permission.TASK_READ):
            statement = statement.where(ResourceChangeModel.resource_type != "job")
        if INTEGRATION_BINDING_SCOPE_KEY in request.scope:
            statement = statement.where(
                ResourceChangeModel.resource_type.not_in(
                    ["reading_state", "integration_token", "annotation"],
                )
            )
        rows = session.scalars(statement.order_by(ResourceChangeModel.id).limit(limit + 1)).all()
        more = len(rows) > limit
        rows = rows[:limit]
        watermark = rows[-1].id if more and rows else maximum
        next_cursor = _encode_cursor(request, principal, watermark, stamp)
        return {
            "items": [
                {
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "change_type": row.change_type,
                    "revision": row.revision,
                    "status": row.status,
                    "created_at": aware(row.created_at).isoformat(),
                }
                for row in rows
            ],
            "next_cursor": next_cursor,
            "has_more": more,
        }


@router.get("/changes", response_model=ChangesResponse)
def get_changes(
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    reset: bool = False,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return _change_page(request, principal, cursor, limit, reset=reset)


@router.get(
    "/events",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE changes events contain a ChangesResponse JSON value; "
            "reset/auth-required events require re-synchronization or authentication",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def events(
    request: Request,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
    cursor: str | None = None,
) -> StreamingResponse:
    requested = cursor or request.headers.get("last-event-id")
    page = await asyncio.to_thread(_change_page, request, principal, requested, 100)

    async def stream():
        current = page
        deadline = time.monotonic() + 60
        while True:
            yield (
                "id: "
                + current["next_cursor"]
                + "\nevent: changes\ndata: "
                + json.dumps(current, separators=(",", ":"))
                + "\n\n"
            )
            if time.monotonic() >= deadline or await request.is_disconnected():
                return
            await asyncio.sleep(1)
            try:
                # Revalidates token/user/membership on every poll. Never retain the initial
                # principal as authority for a long-lived stream after revocation.
                active = await asyncio.to_thread(resolve_request_principal, request)
                current = await asyncio.to_thread(
                    _change_page,
                    request,
                    active,
                    current["next_cursor"],
                    100,
                )
            except HTTPException as error:
                event = "reset" if error.status_code == 410 else "auth-required"
                yield f'event: {event}\ndata: {{"reconnect_required":true}}\n\n'
                return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


class ReadingStateWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    block_id: str | None = Field(default=None, max_length=36)
    progress: float = Field(ge=0, le=1, allow_inf_nan=False)


def _reading_response(row: ReadingStateModel | None, item_id: str) -> dict:
    return {
        "library_item_id": item_id,
        "block_id": row.block_id if row else None,
        "progress": row.progress if row else 0,
        "revision": row.revision if row else 0,
        "updated_at": row.updated_at if row else None,
    }


def _require_item(session, principal, item_id):
    item = load_accessible_library_item(session, principal, item_id)
    if item is None:
        raise HTTPException(404, detail={"code": "LIBRARY_ITEM_NOT_FOUND"})
    return item


@router.get("/me/library-items/{item_id}/reading-state", response_model=ReadingStateResponse)
def get_reading_state(
    item_id: str,
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> dict:
    require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        ensure_default_library(session, principal)
        _require_item(session, principal, item_id)
        row = session.get(ReadingStateModel, (principal.user_id, item_id))
        response.headers["Cache-Control"] = "no-store"
        return _reading_response(row, item_id)


@router.put("/me/library-items/{item_id}/reading-state", response_model=ReadingStateResponse)
def put_reading_state(
    item_id: str,
    payload: ReadingStateWrite,
    request: Request,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> dict:
    require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        begin_write(session)
        ensure_default_library(session, principal)
        item = _require_item(session, principal, item_id)
        if payload.block_id and not session.scalar(
            select(ContentBlockModel.id).where(
                ContentBlockModel.id == payload.block_id,
                ContentBlockModel.paper_id == item.paper_id,
            )
        ):
            raise HTTPException(422, detail={"code": "READING_BLOCK_INVALID"})
        row = session.get(ReadingStateModel, (principal.user_id, item_id))
        if payload.revision != (row.revision if row else 0):
            raise HTTPException(409, detail={"code": "REVISION_CONFLICT"})
        if row is None:
            row = ReadingStateModel(user_id=principal.user_id, library_item_id=item_id, revision=0)
            session.add(row)
        row.block_id, row.progress = payload.block_id, payload.progress
        row.revision += 1
        row.updated_at = datetime.now(UTC)
        session.commit()
        return _reading_response(row, item_id)


class IntegrationTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=128)
    scopes: list[IntegrationReadScope] = Field(min_length=1, max_length=2)
    expires_in_days: int = Field(default=30, ge=1, le=90)


def _token_metadata(row: IntegrationTokenModel) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "scopes": row.scopes_json,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "created_at": row.created_at,
    }


@router.get("/me/integration-tokens", response_model=IntegrationTokenListResponse)
def list_integration_tokens(
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> dict:
    with request.app.state.session_factory() as session:
        rows = session.scalars(
            select(IntegrationTokenModel)
            .where(
                IntegrationTokenModel.user_id == principal.user_id,
                IntegrationTokenModel.workspace_id == principal.active_workspace_id,
            )
            .order_by(IntegrationTokenModel.created_at.desc())
            .limit(200)
        ).all()
        response.headers["Cache-Control"] = "no-store"
        return {"items": [_token_metadata(row) for row in rows]}


@router.post(
    "/me/integration-tokens",
    status_code=201,
    response_model=IntegrationTokenIssueResponse,
)
def create_integration_token(
    payload: IntegrationTokenCreate,
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> dict:
    # Credentials cannot grant a permission the issuing principal does not possess.
    if ("library:read" in payload.scopes and not principal.has(Permission.LIBRARY_READ)) or (
        "jobs:read" in payload.scopes and not principal.has(Permission.TASK_READ)
    ):
        raise HTTPException(403, detail={"code": "TOKEN_SCOPE_DENIED"})
    raw = "ap_i2_" + secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with request.app.state.session_factory() as session:
        begin_write(session)
        user = session.get(UserModel, principal.user_id)
        installation = session.get(InstallationStateModel, "installation")
        if user is None or user.status != "ACTIVE" or installation is None:
            raise HTTPException(401, detail={"code": "AUTHENTICATION_REQUIRED"})
        row = IntegrationTokenModel(
            user_id=principal.user_id,
            workspace_id=principal.active_workspace_id,
            installation_state_version=installation.state_version,
            auth_generation=user.auth_generation,
            label=payload.label,
            scopes_json=sorted(set(payload.scopes)),
            digest=digest_integration_token(raw),
            expires_at=now + timedelta(days=payload.expires_in_days),
        )
        session.add(row)
        session.commit()
        response.headers["Cache-Control"] = "no-store"
        return {**_token_metadata(row), "token": raw}


@router.delete("/me/integration-tokens/{token_id}", status_code=204)
def revoke_integration_token(
    token_id: str,
    request: Request,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> Response:
    with request.app.state.session_factory() as session:
        begin_write(session)
        row = session.scalar(
            select(IntegrationTokenModel).where(
                IntegrationTokenModel.id == token_id,
                IntegrationTokenModel.user_id == principal.user_id,
                IntegrationTokenModel.workspace_id == principal.active_workspace_id,
            )
        )
        if row is None:
            raise HTTPException(404, detail={"code": "INTEGRATION_TOKEN_NOT_FOUND"})
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
        session.commit()
        return Response(status_code=204)
