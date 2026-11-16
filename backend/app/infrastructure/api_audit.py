"""Audit successful API mutations without copying bodies, queries, cookies or headers."""

from __future__ import annotations

import re
from uuid import uuid4

from app.domain.auth import RequestPrincipal
from app.infrastructure.db.audit_models import AuditEventModel


class ApiAuditMiddleware:
    def __init__(self, app, *, session_factory):
        self.app, self.session_factory = app, session_factory

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        scope = dict(scope)
        scope.setdefault("state", {})
        scope.setdefault("academatepaper.request_id", uuid4().hex)

        async def audited_send(message):
            if (
                message["type"] == "http.response.start"
                and scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}
                and 200 <= message["status"] < 300
            ):
                state = scope["state"]
                principal = state.get("request_principal")
                actor = principal.user_id if isinstance(principal, RequestPrincipal) else None
                workspace = (
                    principal.active_workspace_id
                    if isinstance(principal, RequestPrincipal)
                    else None
                )
                from app.infrastructure.client_sessions import (
                    CLIENT_BINDING_SCOPE_KEY,
                    VerifiedClientSessionBinding,
                )

                binding = scope.get(CLIENT_BINDING_SCOPE_KEY)
                if isinstance(binding, VerifiedClientSessionBinding):
                    actor, workspace = binding.user_id, binding.active_workspace_id
                actor = actor or state.get("audit_actor_user_id")
                workspace = workspace or state.get("audit_workspace_id")
                if actor:
                    route = scope.get("route")
                    # Route templates contain resource names/IDs, not query strings or proof.
                    path = str(getattr(route, "path", scope.get("path", "")))[:512]
                    with self.session_factory() as session:
                        session.add(
                            AuditEventModel(
                                actor_user_id=actor,
                                workspace_id=workspace,
                                operation=f"{scope['method']} {getattr(route, 'name', 'api')}"[
                                    :128
                                ],
                                resource_path=path,
                                request_id=scope["academatepaper.request_id"],
                                targets={
                                    key: str(value)
                                    for key, value in scope.get("path_params", {}).items()
                                    if key
                                    in {
                                        "user_id",
                                        "library_id",
                                        "paper_id",
                                        "annotation_id",
                                        "job_id",
                                        "session_id",
                                        "library_item_id",
                                        "token_id",
                                        "node_id",
                                        "edge_id",
                                        "map_id",
                                        "question_id",
                                        "block_id",
                                    }
                                    and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(value))
                                },
                                status_code=message["status"],
                            )
                        )
                        session.commit()
            await send(message)

        await self.app(scope, receive, audited_send)
