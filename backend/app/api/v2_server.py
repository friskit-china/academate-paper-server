"""Server discovery, administration and constrained processing commands."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.v1 import paper_maps, papers
from app.api.v1.runtime import RuntimeCapabilitiesResponse
from app.application.runtime import APPLICATION_VERSION, DATABASE_SCHEMA_REVISION
from app.application.task_access import TaskAccessRevoked
from app.domain.auth import Permission
from app.domain.enums import TaskStatus
from app.domain.setup import InstallationMode
from app.domain.tasks import TaskPaidConfirmationRequired, TaskScope
from app.infrastructure.db.audit_models import AuditEventModel
from app.infrastructure.db.models import (
    GroupMembershipModel,
    GroupModel,
    InstallationStateModel,
    PaperSourceModel,
    ProcessingTaskModel,
    UserModel,
    WorkspaceMembershipModel,
)
from app.infrastructure.worker_runtime import DatabaseWorkerHeartbeatStore

router = APIRouter(tags=["server"])


class ServerInfo(BaseModel):
    server_id: str
    product: str = "AcadeMate Paper Server"
    version: str
    api_versions: list[str]
    setup_state: str
    installation_mode: InstallationMode | None


@router.api_route("/server/info", methods=["GET", "HEAD"], response_model=ServerInfo)
def info(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return ServerInfo(
        server_id=request.app.state.client_sessions.server_id(),
        version=APPLICATION_VERSION,
        api_versions=["v1", "v2"],
        setup_state=request.app.state.runtime_setup_state_provider.get_setup_state().value,
        installation_mode=request.app.state.runtime_installation_mode_provider.get_installation_mode(),
    )


@router.get("/runtime/capabilities", response_model=RuntimeCapabilitiesResponse)
def runtime_capabilities_v2(request: Request, response: Response):
    from app.api.v1.runtime import runtime_capabilities

    value = runtime_capabilities(request, response)
    value.version.api = "v2"
    return value


@router.get("/server/capabilities", dependencies=[Depends(resolve_request_principal)])
def capabilities(request: Request):
    return {
        "schema_version": 1,
        "authentication": ["native-bearer", "web-cookie"],
        "features": [
            "libraries",
            "library-grants",
            "jobs",
            "job-cancellation",
            "events",
            "reading-state",
            "annotations",
            "translations",
            "paper-maps",
            "credentials",
        ],
        "events": {"transport": "sse", "replay": True, "cursor_reset": True},
        "limits": {"page_default": 50, "page_max": 200},
        "v1_compatibility": True,
        "installation_mode": (
            request.app.state.runtime_installation_mode_provider.get_installation_mode()
        ),
    }


@router.get("/admin/runtime", dependencies=[Depends(resolve_request_principal)])
def runtime(request: Request, response: Response):
    from datetime import UTC

    require_request_permission(request, Permission.USER_MANAGE)
    with request.app.state.session_factory() as session:
        oldest_queued = session.scalar(
            select(func.min(ProcessingTaskModel.created_at)).where(
                ProcessingTaskModel.visibility != "SYSTEM",
                ProcessingTaskModel.status == TaskStatus.QUEUED,
            )
        )
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        counts = session.execute(
            select(ProcessingTaskModel.status, func.count())
            .where(
                ProcessingTaskModel.visibility != "SYSTEM",
            )
            .group_by(ProcessingTaskModel.status)
        ).all()
    heartbeat = DatabaseWorkerHeartbeatStore(request.app.state.session_factory).latest()
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema": revision,
        "required_schema": DATABASE_SCHEMA_REVISION,
        "installation_state": request.app.state.runtime_setup_state_provider.get_setup_state(),
        "worker": heartbeat.safe_summary() if heartbeat else None,
        "jobs": {str(status): count for status, count in counts},
        "oldest_queued_age_seconds": (
            max(
                0,
                (
                    datetime.now(UTC)
                    - (
                        oldest_queued.replace(tzinfo=UTC)
                        if oldest_queued.tzinfo is None
                        else oldest_queued.astimezone(UTC)
                    )
                ).total_seconds(),
            )
            if oldest_queued
            else None
        ),
    }


@router.get("/admin/audit-events", dependencies=[Depends(resolve_request_principal)])
def audit_events(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
):
    principal = require_request_permission(request, Permission.USER_MANAGE)
    with request.app.state.session_factory() as session:
        query = select(AuditEventModel).where(
            AuditEventModel.workspace_id == principal.active_workspace_id,
        )
        if cursor:
            try:
                created, identifier = json.loads(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                )
                timestamp = datetime.fromisoformat(created)
                if not isinstance(identifier, str) or len(identifier) != 36:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                raise HTTPException(400, detail={"code": "INVALID_CURSOR"}) from None
            query = query.where(
                (AuditEventModel.created_at < timestamp)
                | ((AuditEventModel.created_at == timestamp) & (AuditEventModel.id < identifier))
            )
        rows = session.scalars(
            query.order_by(
                AuditEventModel.created_at.desc(),
                AuditEventModel.id.desc(),
            ).limit(limit + 1)
        ).all()
        more = len(rows) > limit
        rows = rows[:limit]
        token = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [
                        rows[-1].created_at.isoformat(),
                        rows[-1].id,
                    ]
                ).encode()
            )
            .decode()
            .rstrip("=")
            if more
            else None
        )
        response.headers["Cache-Control"] = "no-store"
        return {
            "items": [
                {
                    "id": row.id,
                    "actor_user_id": row.actor_user_id,
                    "operation": row.operation,
                    "resource_path": row.resource_path,
                    "request_id": row.request_id,
                    "status_code": row.status_code,
                    "created_at": row.created_at,
                    "targets": row.targets,
                }
                for row in rows
            ],
            "next_cursor": token,
        }


class RoleWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["SERVER_ADMIN", "MEMBER"]


@router.put("/admin/users/{user_id}/roles", dependencies=[Depends(resolve_request_principal)])
def roles(user_id: str, payload: RoleWrite, request: Request):
    principal = require_request_permission(request, Permission.USER_MANAGE)
    with request.app.state.session_factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        current = request.app.state.identity_provider.resolve_principal_in_session(
            session,
            user_id=principal.user_id,
            workspace_id=principal.active_workspace_id,
        )
        if not current.has(Permission.USER_MANAGE):
            raise HTTPException(403, detail={"code": "FORBIDDEN"})
        installation = session.get(InstallationStateModel, "installation")
        if installation.installation_mode != "MULTI_USER":
            raise HTTPException(409, detail={"code": "MULTI_USER_REQUIRED"})
        if user_id == installation.owner_user_id:
            raise HTTPException(409, detail={"code": "INSTALLATION_OWNER_IMMUTABLE"})
        target = session.get(UserModel, user_id)
        member = session.get(WorkspaceMembershipModel, (principal.active_workspace_id, user_id))
        if target is None or member is None or target.status != "ACTIVE":
            raise HTTPException(404, detail={"code": "USER_NOT_FOUND"})
        group = session.scalar(select(GroupModel).where(GroupModel.key == "administrators"))
        existing = session.get(GroupMembershipModel, (user_id, group.id))
        if payload.role == "SERVER_ADMIN" and existing is None:
            session.add(GroupMembershipModel(user_id=user_id, group_id=group.id))
        elif payload.role == "MEMBER" and existing is not None:
            session.delete(existing)
        session.commit()
    return {"user_id": user_id, "role": payload.role}


class StatusWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ACTIVE", "DISABLED"]


@router.patch("/admin/users/{user_id}/status", dependencies=[Depends(resolve_request_principal)])
def user_status(user_id: str, payload: StatusWrite, request: Request):
    principal = require_request_permission(request, Permission.USER_MANAGE)
    if payload.status == "DISABLED":
        from app.api.v1.admin_users import disable_managed_user

        return disable_managed_user(user_id=user_id, request=request)
    with request.app.state.session_factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        current = request.app.state.identity_provider.resolve_principal_in_session(
            session,
            user_id=principal.user_id,
            workspace_id=principal.active_workspace_id,
        )
        if not current.has(Permission.USER_MANAGE):
            raise HTTPException(403, detail={"code": "FORBIDDEN"})
        installation = session.get(InstallationStateModel, "installation")
        if installation.installation_mode != "MULTI_USER":
            raise HTTPException(409, detail={"code": "MULTI_USER_REQUIRED"})
        if user_id == installation.owner_user_id:
            raise HTTPException(409, detail={"code": "INSTALLATION_OWNER_IMMUTABLE"})
        user = session.get(UserModel, user_id)
        if (
            user is None
            or session.get(
                WorkspaceMembershipModel,
                (principal.active_workspace_id, user_id),
            )
            is None
        ):
            raise HTTPException(404, detail={"code": "USER_NOT_FOUND"})
        user.status = "ACTIVE"
        session.commit()
    return {"user_id": user_id, "status": "ACTIVE"}


class ProcessingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "parse", "retry_failed", "translate_remaining", "retranslate", "generate_map"
    ]
    confirm_paid: bool = False
    system_prompt: str | None = Field(default=None, max_length=6000)


@router.post(
    "/papers/{paper_id}/processing-jobs",
    status_code=202,
    dependencies=[Depends(resolve_request_principal)],
)
def processing_command(
    paper_id: str,
    payload: ProcessingCommand,
    request: Request,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    require_request_permission(request, Permission.TASK_WRITE)
    if payload.operation not in {"parse", "retry_failed"} and not payload.confirm_paid:
        raise HTTPException(409, detail={"code": "PAID_OPERATION_CONFIRMATION_REQUIRED"})
    if payload.operation == "parse":
        from app.workers.handlers import _ensure_parse_task

        with request.app.state.session_factory() as session:
            papers._require_paper(session, request, paper_id, permission=Permission.TASK_WRITE)
            source = session.scalar(
                select(PaperSourceModel).where(
                    PaperSourceModel.paper_id == paper_id,
                    PaperSourceModel.source_type == "EXPANDED_SOURCE",
                )
            )
            if source is None:
                raise HTTPException(409, detail={"code": "SOURCE_NOT_READY"})
            _ensure_parse_task(
                session,
                paper_id=paper_id,
                manifest_key=source.storage_key,
                source_hash=source.sha256,
            )
            session.flush()
            task = session.scalar(
                select(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == paper_id,
                    ProcessingTaskModel.task_type == "PARSE_SOURCE",
                )
                .order_by(ProcessingTaskModel.created_at.desc())
            )
            session.commit()
            return {
                "job_id": task.id,
                "status": task.status,
                "status_url": f"/api/v2/jobs/{task.id}",
            }
    if payload.operation == "retry_failed":
        return retry_failed_paper(paper_id, request, RetryFailed(confirm_paid=payload.confirm_paid))
    if payload.operation == "translate_remaining":
        return papers.translate_remaining_paper(paper_id=paper_id, request=request)
    if payload.operation == "retranslate":
        return papers.retranslate_paper(
            paper_id=paper_id,
            request=request,
            payload=papers.TranslationRetranslateRequest(system_prompt=payload.system_prompt),
        )
    return paper_maps.generate_visual_map(paper_id=paper_id, request=request)


class RetryFailed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_paid: bool = False


@router.post("/papers/{paper_id}/retry", dependencies=[Depends(resolve_request_principal)])
def retry_failed_paper(
    paper_id: str,
    request: Request,
    payload: Annotated[RetryFailed | None, Body()] = None,
):
    principal = require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        papers._require_paper(session, request, paper_id, permission=Permission.TASK_WRITE)
    try:
        retried = request.app.state.task_queue.retry_failed(
            paper_id,
            requested_by_user_id=principal.user_id,
            visible_scope_keys=frozenset(
                {
                    *(
                        {TaskScope.global_derived().scope_key}
                        if principal.has(Permission.USER_MANAGE)
                        else set()
                    ),
                    TaskScope.workspace_derived(
                        workspace_id=principal.active_workspace_id,
                        requested_by_user_id=principal.user_id,
                    ).scope_key,
                    TaskScope.user_private(
                        workspace_id=principal.active_workspace_id,
                        requested_by_user_id=principal.user_id,
                    ).scope_key,
                }
            ),
            allow_paid=bool(payload and payload.confirm_paid),
            enforce_access=True,
        )
    except TaskPaidConfirmationRequired:
        raise HTTPException(409, detail={"code": "PAID_OPERATION_CONFIRMATION_REQUIRED"}) from None
    except TaskAccessRevoked:
        raise HTTPException(403, detail={"code": "TASK_AUTHORIZATION_REVOKED"}) from None
    return {"retried_tasks": retried, "paper": papers.get_paper(paper_id=paper_id, request=request)}


@router.get("/openapi.json", dependencies=[Depends(resolve_request_principal)])
def contract(request: Request):
    from fastapi.openapi.utils import get_openapi

    # FastAPI's pinned release stores included routers lazily. Let get_openapi traverse the
    # prefixed v2 router rather than filtering app.routes by concrete APIRoute type.
    prefixed = APIRouter()
    prefixed.include_router(request.app.state.v2_contract_router, prefix="/api/v2")
    schema = get_openapi(
        title="AcadeMate Paper Server API",
        version="2.0.0",
        description="Independent user API; v1 remains available for the M7 compatibility cycle.",
        routes=prefixed.routes,
    )
    schema["info"]["x-server-version"] = APPLICATION_VERSION
    schema.setdefault("components", {}).setdefault("securitySchemes", {}).update(
        {
            "UserBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"},
            "WebCookie": {"type": "apiKey", "in": "cookie", "name": "academate_v2_access"},
        }
    )
    public = {
        "/api/v2/server/info",
        "/api/v2/health/live",
        "/api/v2/health/ready",
        "/api/v2/runtime/capabilities",
        "/api/v2/setup/status",
        "/api/v2/setup/owner",
        "/api/v2/auth/sessions",
        "/api/v2/auth/refresh",
        "/api/v2/auth/registrations",
    }
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method in {"get", "head", "post", "put", "patch", "delete", "options"}:
                operation["security"] = (
                    [] if path in public else [{"UserBearer": []}, {"WebCookie": []}]
                )
    return schema
