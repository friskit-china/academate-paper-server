from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from app.api.v2_auth import ClientApiMiddleware
from app.api.v2_contracts import V2ContractMiddleware, error_response
from app.infrastructure.api_audit import ApiAuditMiddleware
from app.infrastructure.internal_transport import InternalServiceTokenMiddleware


class VersionedTransportMiddleware:
    """Keep v1 authentication unchanged; v2 accepts independently verified user credentials."""

    def __init__(
        self,
        app,
        *,
        client_sessions,
        session_factory,
        web_public_origin,
        allowed_web_origins=None,
        cookie_secure=False,
        allow_insecure_http=False,
        **v1_options,
    ):
        app = ApiAuditMiddleware(app, session_factory=session_factory)
        self.v1 = InternalServiceTokenMiddleware(app, **v1_options)
        self.v2 = ClientApiMiddleware(
            V2ContractMiddleware(app, session_factory=session_factory),
            service=client_sessions,
            internal_service_token=v1_options["expected_token"],
            web_public_origin=web_public_origin,
            allowed_web_origins=allowed_web_origins,
            cookie_secure=cookie_secure,
            allow_insecure_http=allow_insecure_http,
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope)
            scope["papermanager.request_id"] = uuid4().hex
        path = scope.get("path", "")
        target = self.v2 if path == "/api/v2" or path.startswith("/api/v2/") else self.v1

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > 2_000_000:
                    raise HTTPException(413, detail={"code": "COMMAND_TOO_LARGE"})
            return message

        async def private_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = message.get("headers", [])
                vary = {
                    part.strip().lower()
                    for key, value in headers
                    if key.lower() == b"vary"
                    for part in value.split(b",")
                    if part.strip()
                }
                vary.update({b"cookie", b"authorization"})
                message["headers"] = [
                    (key, value)
                    for key, value in headers
                    if key.lower() not in {b"cache-control", b"vary"}
                ] + [
                    (b"cache-control", b"private, no-store"),
                    (b"vary", b"*" if b"*" in vary else b", ".join(sorted(vary))),
                ]
            await send(message)

        if target is self.v2:
            received = 0
            try:
                await target(scope, limited_receive, private_send)
            except HTTPException as error:
                if error.status_code != 413:
                    raise
                await error_response(
                    "COMMAND_TOO_LARGE",
                    "Command is too large.",
                    413,
                    scope.get("papermanager.request_id", ""),
                )(scope, receive, private_send)
        else:
            await target(scope, receive, send)
