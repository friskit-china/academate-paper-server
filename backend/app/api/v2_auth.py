"""v2 user authentication adapters. Never expose web credentials in JSON."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.dependencies import resolve_request_principal, resolve_session_request_principal
from app.api.v1.auth import WebSessionStatusResponse
from app.domain.auth import Permission
from app.infrastructure.client_sessions import (
    ACCESS_COOKIE,
    CLIENT_BINDING_SCOPE_KEY,
    CLIENT_TRANSPORT_SCOPE_KEY,
    REFRESH_COOKIE,
    ClientAuthenticationError,
    ClientRateLimitError,
    ClientSessionService,
    ClientTokenPair,
    VerifiedClientSessionBinding,
)
from app.infrastructure.db.client_models import ClientAccessCredentialModel
from app.infrastructure.db.models import UserModel, WorkspaceModel
from app.server_https import authority as https_authority

router = APIRouter(tags=["client-authentication"])
_TRANSPORT_HEADER = b"x-academate-client-transport"
_SERVICE_HEADER = b"x-academate-internal-service-token"
_PUBLIC = {
    ("GET", "/api/v2/server/info"),
    ("HEAD", "/api/v2/server/info"),
    ("GET", "/api/v2/health/live"),
    ("GET", "/api/v2/health/ready"),
    ("POST", "/api/v2/setup/owner"),
    ("GET", "/api/v2/setup/status"),
    ("GET", "/api/v2/runtime/capabilities"),
}
_AUTH_ANONYMOUS = {
    ("POST", "/api/v2/auth/sessions"),
    ("POST", "/api/v2/auth/refresh"),
    ("POST", "/api/v2/auth/registrations"),
    ("POST", "/api/v2/auth/legacy-exchange"),
}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: SecretStr
    workspace_id: str | None = Field(default=None, max_length=36)
    client_name: str = Field(default="", max_length=128)
    device_name: str = Field(default="", max_length=128)


class LegacyExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: SecretStr | None = None


class WebTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authenticated: Literal[True]
    user_id: str
    active_workspace_id: str
    session_id: str
    expires_at: datetime
    server_id: str


class NativeTokenResponse(WebTokenResponse):
    access_token: str = Field(json_schema_extra={"readOnly": True})
    refresh_token: str = Field(json_schema_extra={"readOnly": True})
    token_type: Literal["Bearer"]
    expires_in: int = Field(ge=0)
    refresh_expires_at: datetime


class ClientStatusResponse(WebSessionStatusResponse):
    session_id: str
    expires_at: datetime


class DeviceSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    client_kind: Literal["native", "web"]
    client_name: str
    device_name: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked: bool
    current: bool


class DeviceSessionListResponse(BaseModel):
    items: list[DeviceSessionResponse]
    next_cursor: str | None


async def _payload(request: Request, model):
    # Parse inside the handler so validation errors never echo secrets as input values.
    try:
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8192:
                raise ValueError
            chunks.append(chunk)
        raw = b"".join(chunks)
        return model.model_validate(json.loads(raw or b"{}"))
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(400, detail={"code": "INVALID_AUTH_REQUEST"}) from None


def _binding(request: Request) -> VerifiedClientSessionBinding:
    value = request.scope.get(CLIENT_BINDING_SCOPE_KEY)
    if not isinstance(value, VerifiedClientSessionBinding):
        raise HTTPException(401, detail={"code": "AUTHENTICATION_REQUIRED"})
    return value


def _service(request: Request) -> ClientSessionService:
    return request.app.state.client_sessions


def _cookies(request: Request) -> dict[str, str]:
    return request.scope.get("academatepaper.client-cookies.v2", {})


def _integration_read_allowed(method: str, path: str, scopes: frozenset[str]) -> bool:
    import re

    if method not in {"GET", "HEAD"}:
        return False
    resource_id = r"[A-Za-z0-9_-]{1,128}"
    asset_id = r"[A-Za-z0-9_-]{1,4096}"
    library_paths = (
        r"/api/v2/libraries",
        rf"/api/v2/libraries/{resource_id}(?:/items)?",
        rf"/api/v2/library-items/{resource_id}",
        r"/api/v2/papers",
        rf"/api/v2/papers/{resource_id}(?:/(?:outline|blocks|bibliography|sources|assets))?",
        rf"/api/v2/papers/{resource_id}/sources/"
        r"(?:ARXIV_TARBALL|ARXIV_PDF|EXPANDED_SOURCE)/download",
        rf"/api/v2/assets/{asset_id}/content",
    )
    if "library:read" in scopes and any(re.fullmatch(pattern, path) for pattern in library_paths):
        return True
    return (
        "jobs:read" in scopes
        and re.fullmatch(rf"/api/v2/jobs(?:/{resource_id})?", path) is not None
    )


def _configured_web_origin(
    raw: str, *, cookie_secure: bool, allow_insecure_http: bool = False
) -> tuple[str, str]:
    """Canonicalize explicit origins, rejecting wildcard, credentials and unsafe HTTP."""
    try:
        if (
            not isinstance(raw, str)
            or not raw
            or "?" in raw
            or "#" in raw
            or any(character.isspace() for character in raw)
        ):
            raise ValueError
        parsed = urlsplit(raw)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or "*" in raw
            or "%" in host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
            host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) is None:
                raise ValueError from None
            host = host.lower()
            loopback = host == "localhost"
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parsed.scheme == "http" and not loopback and not allow_insecure_http:
            raise ValueError
        if parsed.scheme == "https" and not cookie_secure:
            raise ValueError
        default_port = 80 if parsed.scheme == "http" else 443
        authority = host if port is None or port == default_port else f"{host}:{port}"
        return f"{parsed.scheme}://{authority}", authority
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("INVALID_WEB_CLIENT_ORIGIN_CONFIGURATION") from None


class ClientApiMiddleware:
    """No v1 hop-token requirement for native users, and no browser identity headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: ClientSessionService,
        internal_service_token=None,
        web_public_origin: str | None = None,
        allowed_web_origins: list[str] | tuple[str, ...] | None = None,
        any_host_web_ports: list[int] | tuple[int, ...] = (),
        cookie_secure: bool = False,
        allow_insecure_http: bool = False,
    ) -> None:
        self.app, self.service = app, service
        self.internal_token = (
            internal_service_token.get_secret_value()
            if hasattr(internal_service_token, "get_secret_value")
            else internal_service_token
        )
        if allowed_web_origins is not None and not isinstance(allowed_web_origins, (list, tuple)):
            raise ValueError("INVALID_WEB_CLIENT_ORIGIN_CONFIGURATION")
        configured = ([web_public_origin] if web_public_origin else []) + (
            allowed_web_origins or []
        )
        self.web_origins = dict(
            _configured_web_origin(
                origin, cookie_secure=cookie_secure, allow_insecure_http=allow_insecure_http
            )
            for origin in configured
        )
        self.web_authorities = frozenset(self.web_origins.values())
        self.any_host_web_ports = frozenset(any_host_web_ports) if cookie_secure else frozenset()
        self.cookie_secure = cookie_secure

    def _valid_web_origin(self, host: str, origin: str | None, method: str) -> bool:
        if origin is None and method not in {"GET", "HEAD", "OPTIONS"}:
            return False
        explicit = (host in self.web_authorities if origin is None
                    else self.web_origins.get(origin) == host)
        if explicit:
            return True
        if self.any_host_web_ports:
            try:
                name, port = https_authority(f"https://{host}")
            except (ValueError, UnicodeError):
                return False
            name = f"[{name}]" if ":" in name else name
            canonical = name if port == 443 else f"{name}:{port}"
            return (port in self.any_host_web_ports and host == canonical
                    and (origin is None or origin == f"https://{canonical}"))
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/v2"):
            await self.app(scope, receive, send)
            return
        scope.pop("academatepaper.integration-binding.v2", None)
        scope.pop(CLIENT_BINDING_SCOPE_KEY, None)
        scope.pop("academatepaper.internal_service_identity", None)
        scope.pop("academatepaper.local_session_id", None)
        headers = scope.get("headers", [])
        values: dict[bytes, list[str]] = {}
        for name, value in headers:
            values.setdefault(name.lower(), []).append(value.decode("latin1"))
        try:
            if any(
                len(values.get(key, [])) > 1
                for key in (
                    b"authorization",
                    _TRANSPORT_HEADER,
                    _SERVICE_HEADER,
                    b"host",
                    b"origin",
                )
            ):
                raise ClientAuthenticationError("AMBIGUOUS_CREDENTIALS")
            cookies: dict[str, str] = {}
            relevant = {
                ACCESS_COOKIE,
                REFRESH_COOKIE,
                "academate_session",
                "academate_local_session",
            }
            for header in values.get(b"cookie", []):
                for part in header.split(";"):
                    name, sep, value = part.strip().partition("=")
                    if name in relevant and sep:
                        if name in cookies:
                            raise ClientAuthenticationError("AMBIGUOUS_CREDENTIALS")
                        cookies[name] = value
            authorization = values.get(b"authorization", [None])[0]
            if authorization is not None and cookies:
                raise ClientAuthenticationError("AMBIGUOUS_CREDENTIALS")
            transport = values.get(_TRANSPORT_HEADER, ["native"])[0]
            if transport not in {"native", "web"}:
                raise ClientAuthenticationError("INVALID_TRANSPORT")
            if transport == "web":
                supplied = values.get(_SERVICE_HEADER, [""])[0]
                origin = values.get(b"origin", [None])[0]
                authority = values.get(b"host", [""])[0]
                if (
                    authorization is not None
                    or not self.internal_token
                    or not secrets.compare_digest(supplied, self.internal_token)
                    or not self._valid_web_origin(authority, origin, scope["method"])
                ):
                    raise ClientAuthenticationError("INVALID_WEB_TRANSPORT")
            elif cookies and (scope["method"], scope["path"]) != (
                "POST",
                "/api/v2/auth/legacy-exchange",
            ):
                raise ClientAuthenticationError("INVALID_COOKIE_TRANSPORT")
            scope[CLIENT_TRANSPORT_SCOPE_KEY] = transport
            scope["academatepaper.client-cookies.v2"] = cookies
            operation = (scope["method"], scope["path"])
            if operation not in _PUBLIC and operation not in _AUTH_ANONYMOUS:
                if transport == "native":
                    scheme, separator, raw = (authorization or "").partition(" ")
                    if not separator or scheme.casefold() != "bearer":
                        raise ClientAuthenticationError("AUTHENTICATION_REQUIRED")
                else:
                    raw = cookies.get(ACCESS_COOKIE, "")
                if transport == "native" and raw.startswith("ap_i2_"):
                    from app.api.v2_sync import (
                        INTEGRATION_BINDING_SCOPE_KEY,
                        verify_integration_token,
                    )

                    try:
                        integration = await run_in_threadpool(
                            verify_integration_token,
                            raw,
                            self.service.session_factory,
                        )
                    except HTTPException:
                        raise ClientAuthenticationError("INVALID_INTEGRATION_TOKEN") from None
                    if not _integration_read_allowed(
                        scope["method"], scope["path"], integration.scopes
                    ):
                        request_id = scope.get("academatepaper.request_id", "")
                        await JSONResponse(
                            {
                                "code": "INTEGRATION_SCOPE_FORBIDDEN",
                                "message": "This integration token does not allow this operation.",
                                "request_id": request_id,
                                "details": None,
                            },
                            status_code=403,
                            headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
                        )(scope, receive, send)
                        return
                    scope[INTEGRATION_BINDING_SCOPE_KEY] = integration
                else:
                    scope[CLIENT_BINDING_SCOPE_KEY] = await run_in_threadpool(
                        self.service.verify_access,
                        raw,
                        client_kind=transport,
                    )
            elif authorization is not None:
                raise ClientAuthenticationError("UNEXPECTED_AUTHORIZATION")
        except (ClientAuthenticationError, ValueError, UnicodeError, TypeError):
            request_id = scope.get("academatepaper.request_id", "")
            await JSONResponse(
                {
                    "code": "INVALID_CLIENT_AUTHENTICATION",
                    "message": "A valid application session is required.",
                    "request_id": request_id,
                    "details": None,
                },
                status_code=401,
                headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _token_response(request: Request, pair: ClientTokenPair) -> JSONResponse:
    request.state.audit_actor_user_id = pair.user_id
    request.state.audit_workspace_id = pair.active_workspace_id
    web = request.scope.get(CLIENT_TRANSPORT_SCOPE_KEY) == "web"
    body = {
        "authenticated": True,
        "user_id": pair.user_id,
        "active_workspace_id": pair.active_workspace_id,
        "session_id": pair.session_id,
        "expires_at": pair.access_expires_at.isoformat(),
        "server_id": pair.server_id,
    }
    if not web:
        body.update(
            {
                "access_token": pair.access_token,
                "refresh_token": pair.refresh_token,
                "token_type": "Bearer",
                "expires_in": max(
                    0, int((pair.access_expires_at - datetime.now(UTC)).total_seconds())
                ),
                "refresh_expires_at": pair.refresh_expires_at.isoformat(),
            }
        )
    response = JSONResponse(body, headers={"Cache-Control": "no-store"})
    if web:
        for legacy_name in ("academate_session", "academate_local_session"):
            response.delete_cookie(legacy_name, path="/", httponly=True, samesite="strict")
        now = datetime.now(UTC)
        for name, token, expires, path in (
            (ACCESS_COOKIE, pair.access_token, pair.access_expires_at, "/"),
            (REFRESH_COOKIE, pair.refresh_token, pair.refresh_expires_at, "/api/v2/auth/refresh"),
        ):
            response.set_cookie(
                name,
                token,
                httponly=True,
                secure=request.app.state.settings.web_session_cookie_secure,
                samesite="strict",
                path=path,
                expires=expires,
                max_age=max(1, int((expires - now).total_seconds())),
            )
    return response


def _clear_cookies(response: Response) -> None:
    for name, path in (
        (ACCESS_COOKIE, "/"),
        (REFRESH_COOKIE, "/api/v2/auth/refresh"),
        ("academate_session", "/"),
        ("academate_local_session", "/"),
    ):
        response.delete_cookie(name, path=path, httponly=True, samesite="strict")


@router.post(
    "/auth/sessions",
    response_model=NativeTokenResponse | WebTokenResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": LoginRequest.model_json_schema()},
            },
        }
    },
)
async def login(request: Request):
    payload = await _payload(request, LoginRequest)
    try:
        pair = await run_in_threadpool(
            _service(request).login,
            username=payload.username,
            password=payload.password.get_secret_value(),
            client_kind=request.scope[CLIENT_TRANSPORT_SCOPE_KEY],
            requested_workspace_id=payload.workspace_id,
            client_name=payload.client_name,
            device_name=payload.device_name,
            peer=request.client.host if request.client else "unknown",
        )
    except ClientRateLimitError:
        raise HTTPException(
            429, detail={"code": "LOGIN_RATE_LIMITED"}, headers={"Retry-After": "60"}
        ) from None
    except ClientAuthenticationError:
        raise HTTPException(401, detail={"code": "INVALID_LOGIN"}) from None
    return _token_response(request, pair)


@router.post(
    "/auth/refresh",
    response_model=NativeTokenResponse | WebTokenResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": RefreshRequest.model_json_schema()},
            },
        }
    },
)
async def refresh(request: Request):
    payload = await _payload(request, RefreshRequest)
    web = request.scope[CLIENT_TRANSPORT_SCOPE_KEY] == "web"
    if web and payload.refresh_token is not None:
        raise HTTPException(400, detail={"code": "AMBIGUOUS_REFRESH_CREDENTIAL"})
    raw = (
        _cookies(request).get(REFRESH_COOKIE, "")
        if web
        else (payload.refresh_token.get_secret_value() if payload.refresh_token else "")
    )
    try:
        pair = await run_in_threadpool(
            _service(request).refresh,
            raw,
            client_kind="web" if web else "native",
        )
    except ClientAuthenticationError:
        raise HTTPException(401, detail={"code": "INVALID_REFRESH_TOKEN"}) from None
    return _token_response(request, pair)


@router.get("/auth/status", response_model=ClientStatusResponse)
def status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    principal = resolve_session_request_principal(request)
    binding = _binding(request)
    with _service(request).session_factory() as session:
        user = session.get(UserModel, principal.user_id)
        workspace = session.get(WorkspaceModel, principal.active_workspace_id)
        credential = session.get(ClientAccessCredentialModel, binding.credential_id)
        return {
            "authenticated": True,
            "user_id": principal.user_id,
            "active_workspace_id": principal.active_workspace_id,
            "session_id": binding.session_id,
            "expires_at": credential.expires_at.replace(tzinfo=UTC).isoformat(),
            "user": {"id": user.id, "username": user.username, "display_name": user.display_name},
            "workspace": {"id": workspace.id, "name": workspace.name},
            "onboarding_required": principal.onboarding_required,
            "can_manage_users": principal.has(Permission.USER_MANAGE),
            "can_manage_invitations": principal.has(Permission.USER_MANAGE),
        }


@router.delete("/auth/sessions/current", status_code=204)
def logout(request: Request):
    resolve_session_request_principal(request)
    _service(request).revoke(_binding(request))
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if request.scope.get(CLIENT_TRANSPORT_SCOPE_KEY) == "web":
        _clear_cookies(response)
    return response


@router.get("/me/sessions", response_model=DeviceSessionListResponse)
def sessions(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
):
    response.headers["Cache-Control"] = "no-store"
    resolve_request_principal(request)
    try:
        return _service(request).list_sessions(_binding(request), limit=limit, cursor=cursor)
    except ValueError:
        raise HTTPException(400, detail={"code": "INVALID_SESSION_CURSOR"}) from None


@router.delete("/me/sessions/{session_id}", status_code=204)
def revoke_session(session_id: str, request: Request):
    resolve_request_principal(request)
    binding = _binding(request)
    if not _service(request).revoke(binding, session_id):
        raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND"})
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if session_id == binding.session_id and request.scope.get(CLIENT_TRANSPORT_SCOPE_KEY) == "web":
        _clear_cookies(response)
    return response


@router.post("/me/sessions/revoke-all", status_code=204)
def revoke_all(request: Request):
    resolve_request_principal(request)
    _service(request).revoke_all(_binding(request))
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if request.scope.get(CLIENT_TRANSPORT_SCOPE_KEY) == "web":
        _clear_cookies(response)
    return response


@router.post(
    "/auth/legacy-exchange",
    response_model=WebTokenResponse,
    include_in_schema=False,
    openapi_extra={
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {"schema": LegacyExchangeRequest.model_json_schema()},
            },
        }
    },
)
async def exchange_legacy_web_session(request: Request):
    # This is a private Web compatibility ceremony, never an automatic/native login.
    if request.scope.get(CLIENT_TRANSPORT_SCOPE_KEY) != "web":
        raise HTTPException(403, detail={"code": "LEGACY_WEB_EXCHANGE_ONLY"})
    await _payload(request, LegacyExchangeRequest)
    if any(name in _cookies(request) for name in (ACCESS_COOKIE, REFRESH_COOKIE)):
        raise HTTPException(401, detail={"code": "AMBIGUOUS_LEGACY_CREDENTIALS"})
    try:
        pair = await run_in_threadpool(
            _service(request).exchange_legacy_web_session,
            _cookies(request).get("academate_session", ""),
        )
    except ClientAuthenticationError:
        raise HTTPException(401, detail={"code": "INVALID_LEGACY_WEB_SESSION"}) from None
    return _token_response(request, pair)
