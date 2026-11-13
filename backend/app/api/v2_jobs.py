"""Scoped durable job operations; never accepts executable commands or task payloads."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, exists, or_, select

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.application.libraries import ensure_default_library, paper_access_predicate
from app.domain.auth import Permission, RequestPrincipal
from app.domain.enums import TaskStatus
from app.domain.tasks import TaskVisibility
from app.infrastructure.db.models import PaperModel, ProcessingTaskModel, UserLlmCredentialModel
from app.infrastructure.db.sync_models import JobCancellationModel
from app.infrastructure.tasks.database import DatabaseTaskQueue

router = APIRouter(prefix="/jobs", tags=["v2-jobs"])

JobStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


class JobResponse(BaseModel):
    id: str
    paper_id: str | None
    type: str
    status: JobStatus
    attempt_count: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None


class JobCommandResponse(BaseModel):
    job_id: str
    status: JobStatus
    status_url: str


DETERMINISTIC_JOBS = frozenset(
    {
        "FETCH_METADATA",
        "FETCH_SOURCE",
        "PARSE_SOURCE",
        "RENDER_TABLE",
        "RENDER_FIGURE",
        "RENDER_LATEX_BLOCK",
        "RENDER_LATEX_FORMULA",
    }
)
PAID_JOBS = frozenset(
    {
        "TRANSLATE_BLOCK",
        "REPAIR_RENDER",
        "ANSWER_QUESTION",
        "PLAN_PAPER_MAP",
        "ANSWER_MAP_QUESTION",
    }
)


def begin_write(session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


def visible_jobs(principal: RequestPrincipal):
    return and_(
        ProcessingTaskModel.paper_id.is_not(None),
        exists(
            select(PaperModel.id).where(
                PaperModel.id == ProcessingTaskModel.paper_id,
                paper_access_predicate(principal),
            )
        ),
        or_(
            ProcessingTaskModel.visibility == TaskVisibility.GLOBAL_DERIVED,
            and_(
                ProcessingTaskModel.visibility == TaskVisibility.WORKSPACE_DERIVED,
                ProcessingTaskModel.workspace_id == principal.active_workspace_id,
            ),
            and_(
                ProcessingTaskModel.visibility == TaskVisibility.USER_PRIVATE,
                ProcessingTaskModel.workspace_id == principal.active_workspace_id,
                ProcessingTaskModel.requested_by_user_id == principal.user_id,
            ),
        ),
    )


def job_response(task: ProcessingTaskModel) -> dict:
    # Deliberately omit payload/result/error_message: those can contain user paper context.
    return {
        "id": task.id,
        "paper_id": task.paper_id,
        "type": task.task_type,
        "status": task.status,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "error_code": task.error_code,
    }


def _cursor(task: ProcessingTaskModel) -> str:
    raw = json.dumps([task.created_at.isoformat(), task.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        if len(value) > 512:
            raise ValueError("oversized")
        created, identifier = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 36:
            raise ValueError("identifier")
        return datetime.fromisoformat(created), identifier
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, detail={"code": "INVALID_CURSOR"}) from None


@router.get("", response_model=JobListResponse)
def list_jobs(
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> dict:
    require_request_permission(request, Permission.TASK_READ)
    with request.app.state.session_factory() as session:
        ensure_default_library(session, principal)
        statement = select(ProcessingTaskModel).where(visible_jobs(principal))
        if cursor:
            created, identifier = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    ProcessingTaskModel.created_at < created,
                    and_(
                        ProcessingTaskModel.created_at == created,
                        ProcessingTaskModel.id < identifier,
                    ),
                )
            )
        tasks = session.scalars(
            statement.order_by(
                ProcessingTaskModel.created_at.desc(),
                ProcessingTaskModel.id.desc(),
            ).limit(limit + 1)
        ).all()
        response.headers["Cache-Control"] = "no-store"
        return {
            "items": [job_response(task) for task in tasks[:limit]],
            "next_cursor": _cursor(tasks[limit - 1]) if len(tasks) > limit else None,
        }


def _load_job(session, principal: RequestPrincipal, job_id: str) -> ProcessingTaskModel:
    task = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.id == job_id,
            visible_jobs(principal),
        )
    )
    if task is None:
        raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
    return task


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> dict:
    require_request_permission(request, Permission.TASK_READ)
    with request.app.state.session_factory() as session:
        ensure_default_library(session, principal)
        task = _load_job(session, principal, job_id)
        response.headers["Cache-Control"] = "no-store"
        return job_response(task)


class RetryJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_paid: bool = False


def _require_mutation_owner(principal: RequestPrincipal, task: ProcessingTaskModel) -> None:
    if task.visibility == TaskVisibility.GLOBAL_DERIVED:
        if not principal.has(Permission.USER_MANAGE):
            raise HTTPException(403, detail={"code": "SHARED_JOB_MANAGEMENT_REQUIRED"})
    elif task.requested_by_user_id != principal.user_id and not principal.has(
        Permission.USER_MANAGE
    ):
        raise HTTPException(403, detail={"code": "JOB_OWNER_REQUIRED"})


@router.post("/{job_id}/retry", status_code=202, response_model=JobCommandResponse)
def retry_job(
    job_id: str,
    payload: RetryJobRequest,
    request: Request,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> dict:
    require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        begin_write(session)
        ensure_default_library(session, principal)
        task = _load_job(session, principal, job_id)
        _require_mutation_owner(principal, task)
        if task.status != TaskStatus.FAILED:
            raise HTTPException(409, detail={"code": "JOB_NOT_RETRYABLE"})
        if task.task_type in PAID_JOBS:
            if not payload.confirm_paid:
                raise HTTPException(409, detail={"code": "PAID_OPERATION_CONFIRMATION_REQUIRED"})
            credential = (
                session.get(UserLlmCredentialModel, task.credential_id)
                if task.credential_id
                else None
            )
            if credential is None or credential.user_id != principal.user_id:
                raise HTTPException(409, detail={"code": "JOB_CREDENTIAL_UNAVAILABLE"})
            service = getattr(request.app.state, "credential_service", None)
            if service is None:
                raise HTTPException(409, detail={"code": "JOB_CREDENTIAL_UNAVAILABLE"})
            try:
                service.resolve_for_task(DatabaseTaskQueue._as_task(task))
            except Exception:
                raise HTTPException(409, detail={"code": "JOB_CREDENTIAL_UNAVAILABLE"}) from None
        elif task.task_type not in DETERMINISTIC_JOBS:
            raise HTTPException(409, detail={"code": "JOB_TYPE_NOT_RETRYABLE"})
        task.status = TaskStatus.QUEUED
        task.attempt_count = 0
        task.available_at = datetime.now(UTC)
        task.started_at = task.finished_at = task.lease_expires_at = None
        task.worker_id = task.error_code = task.error_message = None
        task.lease_generation += 1
        session.commit()
        return {"job_id": task.id, "status": task.status, "status_url": f"/api/v2/jobs/{task.id}"}


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: str,
    request: Request,
    principal: Annotated[RequestPrincipal, Depends(resolve_request_principal)],
) -> dict:
    require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        begin_write(session)
        ensure_default_library(session, principal)
        task = _load_job(session, principal, job_id)
        _require_mutation_owner(principal, task)
        if task.status == TaskStatus.CANCELLED:
            return job_response(task)
        if task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            raise HTTPException(409, detail={"code": "JOB_ALREADY_TERMINAL"})
        now = datetime.now(UTC)
        session.add(
            JobCancellationModel(
                task_id=task.id,
                requested_by_user_id=principal.user_id,
                requested_at=now,
                acknowledged_at=now,
            )
        )
        # The existing handler/storage fences require this RUNNING lease generation.
        # Revoking it atomically prevents subsequent publication; an in-flight external HTTP
        # call can continue until its normal timeout and any incurred cost cannot be undone.
        task.status = TaskStatus.CANCELLED
        task.lease_generation += 1
        task.worker_id = None
        task.lease_expires_at = None
        task.finished_at = now
        task.error_code = "CANCELLED"
        task.error_message = None
        session.commit()
        return job_response(task)
