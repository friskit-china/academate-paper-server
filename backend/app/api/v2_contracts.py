"""v2 request IDs and bounded, authorization-scoped command idempotency.

Reserve before executing. A crash never transfers a PENDING command to a new attempt: its
side effects may already have committed. Receipts never contain request bodies or secrets.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from app.api.dependencies import resolve_request_principal
from app.application.libraries import LibraryError, ensure_default_library
from app.domain.tasks import TaskSecretError, require_secret_free_task_data
from app.infrastructure.db.api_contract_models import ApiIdempotencyModel
from app.infrastructure.db.library_models import LibraryEntryModel, LibraryGrantModel, LibraryModel
from app.infrastructure.db.models import LibraryItemModel

MAX_COMMAND_BYTES = 2_000_000
RECEIPT_PATHS = (
    "/papers",
    "/annotations",
    "/annotation-comments",
    "/libraries",
    "/library-items",
    "/jobs",
    "/maps",
    "/paper-maps",
    "/paper-map-nodes",
    "/paper-map-edges",
    "/paper-map-questions",
    "/me/library-items",
    "/me/bookmarks",
)
SECRET_PATH_SEGMENTS = frozenset(
    {
        "auth",
        "credentials",
        "integration-tokens",
        "setup",
        "invitations",
        "sessions",
    }
)
SEMANTIC_HEADERS = frozenset(
    {
        b"content-type",
        b"content-encoding",
        b"accept",
        b"if-match",
        b"if-none-match",
        b"if-unmodified-since",
    }
)


def error_response(code: str, message: str, status: int, request_id: str = ""):
    return JSONResponse(
        {"code": code, "message": message, "request_id": request_id, "details": None},
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
    )


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _begin_write(session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


def _request_digest(scope, body: bytes) -> str:
    metadata = [
        _digest(body),
        _digest(scope.get("query_string", b"")),
        sorted(
            (key.lower().decode("latin1"), value.decode("latin1"))
            for key, value in scope.get("headers", [])
            if key.lower() in SEMANTIC_HEADERS
        ),
    ]
    return _digest(json.dumps(metadata, separators=(",", ":")))


def _uses_receipt(scope) -> bool:
    path = scope.get("path", "")
    if not path.startswith("/api/v2/") or scope.get("method") not in {
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        return False
    path = path[len("/api/v2") :]
    if any(segment in SECRET_PATH_SEGMENTS for segment in path.split("/")):
        return False
    return any(path == prefix or path.startswith(prefix + "/") for prefix in RECEIPT_PATHS)


def _json_response(content_type: bytes) -> bool:
    media = content_type.split(b";", 1)[0].strip().lower()
    return media == b"application/json" or media.endswith(b"+json")


def _allows_import_item_growth(scope) -> bool:
    if scope.get("method") != "POST":
        return False
    path = scope.get("path", "")
    parts = path.split("/")
    return path == "/api/v2/papers/import" or (
        len(parts) == 6
        and parts[:4] == ["", "api", "v2", "libraries"]
        and bool(parts[4])
        and parts[5] == "imports"
    )


@dataclass(frozen=True)
class _AuthorizationState:
    policy: tuple
    items: frozenset[tuple[str, str, str]]

    @property
    def digest(self) -> str:
        # Preserve the existing persisted digest format; no receipt schema change is needed.
        value = [*self.policy, sorted(self.items)]
        return _digest(json.dumps(value, separators=(",", ":"), default=str))


class V2ContractMiddleware:
    def __init__(self, app, *, session_factory):
        self.app = app
        self.session_factory = session_factory

    def _authorization_state(self, session, principal) -> _AuthorizationState:
        # Only this caller's effective library policy and accessible item set matter. A change
        # to another user's unrelated grant must not create a cross-user activity side channel.
        grants = session.execute(
            select(
                LibraryModel.id,
                LibraryModel.owner_user_id,
                LibraryGrantModel.can_read,
                LibraryGrantModel.can_import,
                LibraryGrantModel.can_manage,
                LibraryGrantModel.can_remove,
            )
            .outerjoin(
                LibraryGrantModel,
                and_(
                    LibraryGrantModel.library_id == LibraryModel.id,
                    LibraryGrantModel.user_id == principal.user_id,
                ),
            )
            .where(
                LibraryModel.workspace_id == principal.active_workspace_id,
                or_(
                    LibraryGrantModel.user_id == principal.user_id,
                    LibraryModel.owner_user_id == principal.user_id,
                ),
            )
            .order_by(LibraryModel.id)
        ).all()
        items = session.execute(
            select(
                LibraryItemModel.id,
                LibraryItemModel.paper_id,
                LibraryEntryModel.library_id,
            )
            .join(LibraryEntryModel, LibraryEntryModel.library_item_id == LibraryItemModel.id)
            .join(
                LibraryGrantModel,
                LibraryGrantModel.library_id == LibraryEntryModel.library_id,
            )
            .where(
                LibraryItemModel.workspace_id == principal.active_workspace_id,
                LibraryGrantModel.user_id == principal.user_id,
                LibraryGrantModel.can_read.is_(True),
            )
            .order_by(LibraryItemModel.id, LibraryEntryModel.library_id)
        ).all()
        policy = (
            principal.user_id,
            principal.active_workspace_id,
            tuple(sorted(permission.value for permission in principal.permission_set)),
            tuple(tuple(row) for row in grants),
        )
        return _AuthorizationState(policy, frozenset(tuple(row) for row in items))

    def _prepare_libraries(self, principal) -> None:
        with self.session_factory() as session:
            _begin_write(session)
            ensure_default_library(session, principal)
            session.commit()

    def _reserve(self, principal, key_digest, request_digest, request_id):
        now = datetime.now(UTC)
        with self.session_factory() as session:
            _begin_write(session)
            reservation = self._authorization_state(session, principal)
            authorization = reservation.digest
            existing = session.get(ApiIdempotencyModel, key_digest)
            if existing is not None:
                if (
                    existing.user_id != principal.user_id
                    or existing.workspace_id != principal.active_workspace_id
                    or existing.request_digest != request_digest
                    or existing.authorization_digest != authorization
                ):
                    return error_response(
                        "IDEMPOTENCY_CONFLICT",
                        "Request or current authorization differs.",
                        409,
                        request_id,
                    )
                if _aware(existing.expires_at) <= now:
                    return error_response(
                        "IDEMPOTENCY_RECEIPT_EXPIRED",
                        "Inspect the resource before using a new key.",
                        409,
                        request_id,
                    )
                if existing.state != "COMPLETE":
                    return error_response(
                        "COMMAND_RESULT_UNAVAILABLE",
                        "Inspect the resource before retrying this command.",
                        409,
                        request_id,
                    )
                payload = json.loads(existing.response_json)
                if isinstance(payload, dict) and "request_id" in payload:
                    payload["request_id"] = request_id
                headers = json.loads(existing.response_headers_json or "{}")
                headers.update(
                    {
                        "Idempotency-Replayed": "true",
                        "Cache-Control": "no-store",
                        "X-Request-ID": request_id,
                    }
                )
                return JSONResponse(payload, status_code=existing.status_code, headers=headers)
            session.add(
                ApiIdempotencyModel(
                    key_digest=key_digest,
                    user_id=principal.user_id,
                    workspace_id=principal.active_workspace_id,
                    request_digest=request_digest,
                    authorization_digest=authorization,
                    state="PENDING",
                    created_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )
            session.commit()
        return reservation

    def _finish(
        self,
        principal,
        key_digest,
        *,
        reservation,
        allow_import_item_growth,
        status_code,
        headers,
        body,
        replayable,
    ):
        payload = None
        import_preflight_failed = False
        if replayable and allow_import_item_growth and status_code == 503:
            with suppress(ValueError, UnicodeError):
                error = json.loads(body)
                import_preflight_failed = (
                    isinstance(error, dict) and error.get("code") == "ARXIV_VERSION_UNAVAILABLE"
                )
        if replayable and 200 <= status_code < 300:
            try:
                payload = json.loads(body)
                require_secret_free_task_data(payload, field_name="idempotency response")
            except (ValueError, UnicodeError, TaskSecretError):
                replayable = False
        else:
            replayable = False
        safe_headers = {}
        if replayable:
            for key, value in headers:
                key = key.lower()
                if key == b"set-cookie" or key == b"authorization":
                    replayable = False
                    break
                if key in {b"etag", b"content-language", b"location"} and len(value) <= 1024:
                    if key == b"location" and (
                        not value.startswith(b"/api/v2/") or b"?" in value or b"#" in value
                    ):
                        replayable = False
                        break
                    safe_headers[key.decode("ascii")] = value.decode("latin1")
        with self.session_factory() as session:
            _begin_write(session)
            record = session.get(ApiIdempotencyModel, key_digest)
            if record is None or record.state != "PENDING":
                return
            current = self._authorization_state(session, principal)
            # A successful import may add its first visible entry. Only these exact import
            # routes may advance the item digest, with every prior entry and all original
            # policy retained. Never rebind after revocation, removal or self-grant changes.
            unchanged = current.digest == record.authorization_digest
            if import_preflight_failed and unchanged:
                # The version lookup failed before the import transaction. Only this
                # precise preflight result can release a reservation for a safe retry.
                session.delete(record)
                session.commit()
                return
            import_growth = (
                allow_import_item_growth
                and record.authorization_digest == reservation.digest
                and current.policy == reservation.policy
                and reservation.items <= current.items
            )
            replayable = replayable and (unchanged or import_growth)
            if replayable:
                record.authorization_digest = current.digest
                record.response_json = json.dumps(payload, separators=(",", ":"))
                record.response_headers_json = json.dumps(safe_headers, separators=(",", ":"))
                record.status_code = status_code
                record.state = "COMPLETE"
            else:
                record.state = "NON_REPLAYABLE"
            session.commit()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        scope = dict(scope)
        request_id = scope.get("academatepaper.request_id") or uuid4().hex
        scope["academatepaper.request_id"] = request_id

        async def identified_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode())]
            await send(message)

        keys = [
            value for key, value in scope.get("headers", []) if key.lower() == b"idempotency-key"
        ]
        if not keys or not _uses_receipt(scope):
            return await self.app(scope, receive, identified_send)
        if (
            len(keys) != 1
            or not 1 <= len(keys[0]) <= 128
            or any(character < 33 or character > 126 for character in keys[0])
        ):
            return await error_response(
                "INVALID_IDEMPOTENCY_KEY",
                "A single bounded ASCII key is required.",
                400,
                request_id,
            )(scope, receive, send)
        try:
            principal = resolve_request_principal(Request(scope, receive))
        except HTTPException as error:
            status = error.status_code if error.status_code in {401, 403} else 503
            return await error_response(
                "AUTHORIZATION_REQUIRED",
                "The command cannot be authorized.",
                status,
                request_id,
            )(scope, receive, send)
        except Exception:
            return await error_response(
                "AUTHORIZATION_UNAVAILABLE",
                "Authorization is unavailable.",
                503,
                request_id,
            )(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_COMMAND_BYTES:
                return await error_response(
                    "COMMAND_TOO_LARGE",
                    "Command is too large.",
                    413,
                    request_id,
                )(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body"):
                break
        raw_body = bytes(body)
        namespace = [
            principal.user_id,
            principal.active_workspace_id,
            scope["method"],
            scope["path"],
            keys[0].decode(),
        ]
        key_digest = _digest(json.dumps(namespace, separators=(",", ":")))
        try:
            self._prepare_libraries(principal)
            reservation = self._reserve(
                principal, key_digest, _request_digest(scope, raw_body), request_id
            )
        except LibraryError:
            return await error_response(
                "AUTHORIZATION_REQUIRED",
                "The command cannot be authorized.",
                403,
                request_id,
            )(scope, receive, send)
        except (SQLAlchemyError, ValueError, TypeError):
            return await error_response(
                "IDEMPOTENCY_UNAVAILABLE",
                "Command receipts are unavailable.",
                503,
                request_id,
            )(scope, receive, send)
        if not isinstance(reservation, _AuthorizationState):
            return await reservation(scope, receive, send)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return await receive()

        status_code = 500
        response_headers = []
        capture = True
        response_started = False
        response_body = bytearray()

        async def record_send(message):
            nonlocal status_code, response_headers, capture, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                response_headers = message.get("headers", [])
                content_type = dict((k.lower(), v) for k, v in response_headers).get(
                    b"content-type", b""
                )
                capture = _json_response(content_type)
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                # Streaming responses pass through without buffering/caching, even if JSON.
                if message.get("more_body") or len(response_body) + len(chunk) > MAX_COMMAND_BYTES:
                    capture = False
                    response_body.clear()
                if capture:
                    response_body.extend(chunk)
                if not message.get("more_body"):
                    # A reserved command must never execute twice because a later receipt
                    # write failed. Retain PENDING and deliver the already executed response.
                    with suppress(SQLAlchemyError):
                        self._finish(
                            principal,
                            key_digest,
                            reservation=reservation,
                            allow_import_item_growth=_allows_import_item_growth(scope),
                            status_code=status_code,
                            headers=response_headers,
                            body=bytes(response_body),
                            replayable=capture,
                        )
            await identified_send(message)

        try:
            await self.app(scope, replay_receive, record_send)
        except Exception:
            if response_started:
                raise
            # Do not delete PENDING: the handler may have committed before failing.
            return await error_response(
                "COMMAND_OUTCOME_UNKNOWN",
                "Inspect the resource before retrying this command.",
                500,
                request_id,
            )(scope, receive, send)
