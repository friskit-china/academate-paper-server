from __future__ import annotations

import secrets

from pydantic import SecretStr
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.application.local_sessions import (
    LocalSessionInvalidError,
    LocalSessionStateError,
    LocalSessionVerifier,
)
from app.application.web_sessions import (
    WebSessionInvalidError,
    WebSessionStateError,
    WebSessionVerifier,
)

INTERNAL_SERVICE_TOKEN_HEADER = "x-academate-internal-service-token"
INTERNAL_SERVICE_IDENTITY_SCOPE_KEY = "academatepaper.internal_service_identity"
LOCAL_SESSION_ID_SCOPE_KEY = "academatepaper.local_session_id"
LOCAL_SESSION_COOKIE_NAME = "academate_local_session"
WEB_SESSION_COOKIE_NAME = "academate_session"
VERIFIED_BFF_IDENTITY = "supervisor-bff"

_BROWSER_AUTHORITY_HEADERS = frozenset(
    {
        INTERNAL_SERVICE_TOKEN_HEADER.encode("ascii"),
        b"x-user-id",
        b"x-workspace-id",
        b"x-group-id",
        b"x-permissions",
        b"x-academate-local-session-id",
        b"x-academate-web-session-id",
        b"cookie",
    }
)

_PUBLIC_REQUESTS = frozenset(
    {
        ("GET", "/api/v1/health/live"),
        ("HEAD", "/api/v1/health/live"),
        ("GET", "/api/v1/health/ready"),
        ("HEAD", "/api/v1/health/ready"),
        ("GET", "/api/v1/runtime/capabilities"),
        ("HEAD", "/api/v1/runtime/capabilities"),
        ("GET", "/api/v1/setup/status"),
        ("HEAD", "/api/v1/setup/status"),
        ("POST", "/api/v1/setup/owner"),
    }
)

_SERVICE_TOKEN_ONLY_REQUESTS = frozenset(
    {
        ("POST", "/api/v1/auth/automatic-owner-session"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/register"),
        ("POST", "/api/v1/local-session/exchange"),
    }
)

class InternalServiceTokenMiddleware:
    """Translate BFF and browser credentials into non-HTTP ASGI identity evidence.

    Browser-supplied identity, user, workspace, and permission headers are never interpreted.  The
    BFF service token proves only the private transport hop; the configured browser-session cookie
    independently selects a database session. Neither raw credential is copied into request state
    or a response.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        expected_token: SecretStr | None,
        session_verifier: LocalSessionVerifier | WebSessionVerifier | None,
        session_cookie_name: str = LOCAL_SESSION_COOKIE_NAME,
        service_identity: str = VERIFIED_BFF_IDENTITY,
    ) -> None:
        self._app = app
        self._expected_token = (
            expected_token.get_secret_value().encode("utf-8")
            if expected_token is not None
            else None
        )
        self._session_verifier = session_verifier
        self._session_cookie_name = session_cookie_name
        self._service_identity = service_identity

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        request_scope = dict(scope)
        request_scope.pop(INTERNAL_SERVICE_IDENTITY_SCOPE_KEY, None)
        request_scope.pop(LOCAL_SESSION_ID_SCOPE_KEY, None)
        method = str(request_scope.get("method", "")).upper()
        path = str(request_scope.get("path", ""))
        if (method, path) in _PUBLIC_REQUESTS:
            request_scope["headers"] = self._without_browser_authority_headers(request_scope)
            await self._app(request_scope, receive, send)
            return

        if self._expected_token is None:
            await self._reject(
                request_scope,
                receive,
                send,
                status_code=503,
                code="INTERNAL_SERVICE_AUTH_UNAVAILABLE",
                message="The internal application boundary is not configured.",
            )
            return

        candidates = [
            value
            for name, value in request_scope.get("headers", [])
            if name.lower() == INTERNAL_SERVICE_TOKEN_HEADER.encode("ascii")
        ]
        verified = len(candidates) == 1 and secrets.compare_digest(
            candidates[0], self._expected_token
        )
        if not verified:
            await self._reject(
                request_scope,
                receive,
                send,
                status_code=401,
                code="TRUSTED_APPLICATION_SESSION_REQUIRED",
                message="A trusted application session is required.",
            )
            return

        raw_session = (
            None
            if (method, path) in _SERVICE_TOKEN_ONLY_REQUESTS
            else self._single_cookie(request_scope, self._session_cookie_name)
        )
        request_scope["headers"] = self._without_browser_authority_headers(request_scope)
        request_scope[INTERNAL_SERVICE_IDENTITY_SCOPE_KEY] = self._service_identity
        if (method, path) in _SERVICE_TOKEN_ONLY_REQUESTS:
            await self._app(request_scope, receive, send)
            return

        if self._session_verifier is None:
            is_web_session = self._session_cookie_name == WEB_SESSION_COOKIE_NAME
            await self._reject(
                request_scope,
                receive,
                send,
                status_code=503,
                code=(
                    "WEB_SESSION_AUTH_UNAVAILABLE"
                    if is_web_session
                    else "LOCAL_SESSION_AUTH_UNAVAILABLE"
                ),
                message="The browser session boundary is not configured.",
            )
            return
        try:
            if raw_session is None:
                raise LocalSessionInvalidError("The Local browser session is missing.")
            session_id = self._session_verifier.verify(raw_session)
        except (LocalSessionInvalidError, WebSessionInvalidError):
            is_web_session = self._session_cookie_name == WEB_SESSION_COOKIE_NAME
            await self._reject(
                request_scope,
                receive,
                send,
                status_code=401,
                code=("WEB_SESSION_REQUIRED" if is_web_session else "LOCAL_SESSION_REQUIRED"),
                message="A valid browser session is required.",
            )
            return
        except (LocalSessionStateError, WebSessionStateError):
            is_web_session = self._session_cookie_name == WEB_SESSION_COOKIE_NAME
            await self._reject(
                request_scope,
                receive,
                send,
                status_code=503,
                code=(
                    "WEB_SESSION_STATE_UNAVAILABLE"
                    if is_web_session
                    else "LOCAL_SESSION_STATE_UNAVAILABLE"
                ),
                message="The browser session state is unavailable.",
            )
            return
        request_scope[LOCAL_SESSION_ID_SCOPE_KEY] = session_id
        await self._app(request_scope, receive, send)

    @staticmethod
    def _without_browser_authority_headers(scope: Scope) -> list[tuple[bytes, bytes]]:
        return [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() not in _BROWSER_AUTHORITY_HEADERS
            and not name.lower().endswith(b"-internal-service-identity")
        ]

    @staticmethod
    def _single_cookie(scope: Scope, cookie_name: str) -> str | None:
        candidates: list[str] = []
        for name, value in scope.get("headers", []):
            if name.lower() != b"cookie":
                continue
            try:
                cookie_header = value.decode("latin-1")
            except UnicodeDecodeError:
                return None
            for item in cookie_header.split(";"):
                key, separator, candidate = item.strip().partition("=")
                if separator and key == cookie_name:
                    candidates.append(candidate.strip())
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        if not 32 <= len(candidate) <= 512 or not candidate.isascii():
            return None
        return candidate

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = JSONResponse(
            {"detail": {"code": code, "message": message}},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
