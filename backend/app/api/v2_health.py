"""Read-only v2 readiness: schema, durable setup, identity and verified worker runtime.

v1 retains its lightweight pre-setup probe for legacy Web startup. This API never writes a
storage sentinel, runs TeX, starts a worker, migrates a database or manufactures READY.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from app.application.runtime import RuntimeSetupState
from app.application.runtime_verification import SchemaRevisionInvariantError
from app.domain.os_adapters import ToolchainCapability
from app.domain.runtime import IdentityMode, RuntimeEdition
from app.domain.worker_runtime import WorkerRuntimeState
from app.infrastructure.db import build_session_factory
from app.infrastructure.runtime_installation import DatabaseWorkerReadinessProbe
from app.infrastructure.schema_revision import DatabaseSchemaRevisionInvariant
from app.infrastructure.worker_runtime import DatabaseWorkerHeartbeatStore
from app.server_health import _readonly_engine

router = APIRouter(prefix="/health", tags=["v2-health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    code: str
    installation_state: str | None = None
    worker_state: str | None = None


def inspect_readiness(settings, *, now: datetime | None = None) -> ReadyResponse:
    current = now if now is not None else datetime.now(UTC)
    installation_state = None
    worker_state = None
    engine = None

    def unavailable(code: str):
        return ReadyResponse(
            status="not_ready",
            code=code,
            installation_state=installation_state,
            worker_state=worker_state,
        )

    try:
        profile = settings.validate_runtime_configuration()
        if (
            profile.edition is not RuntimeEdition.SERVER
            or profile.identity_mode is not IdentityMode.AUTHENTICATED
        ):
            return unavailable("SERVER_CONFIGURATION_INVALID")
        if current.tzinfo is None or current.utcoffset() is None:
            return unavailable("SERVER_CLOCK_INVALID")
        current = current.astimezone(UTC)
        engine = _readonly_engine(settings.database_url)
        factory = build_session_factory(engine)
        DatabaseSchemaRevisionInvariant(factory).verify()
        readiness = DatabaseWorkerReadinessProbe(factory, profile)
        snapshot = readiness.inspect()
        installation_state = snapshot.setup_state.value
        if snapshot.setup_state is not RuntimeSetupState.READY:
            return unavailable("INSTALLATION_NOT_READY")
        if not readiness.is_ready():
            return unavailable("INSTALLATION_NOT_READY")
        heartbeat = DatabaseWorkerHeartbeatStore(factory).latest()
        if heartbeat is None:
            return unavailable("WORKER_HEARTBEAT_MISSING")
        worker_state = heartbeat.state.value
        age = (current - heartbeat.observed_at.astimezone(UTC)).total_seconds()
        if not -5 <= age <= max(15, settings.worker_heartbeat_seconds * 3):
            return unavailable("WORKER_HEARTBEAT_STALE")
        if heartbeat.state is not WorkerRuntimeState.READY or not heartbeat.accepting_tasks:
            return unavailable("WORKER_NOT_READY")
        if not set(ToolchainCapability) <= set(heartbeat.runtime_capabilities):
            return unavailable("WORKER_RUNTIME_UNVERIFIED")
        return ReadyResponse(
            status="ready",
            code="READY",
            installation_state=installation_state,
            worker_state=worker_state,
        )
    except SchemaRevisionInvariantError:
        return unavailable("SCHEMA_NOT_READY")
    except Exception:
        # No SQL diagnostics, host paths, user IDs, credentials or tool output in public probes.
        return unavailable("SERVER_STATE_INVALID")
    finally:
        if engine is not None:
            engine.dispose()


@router.get("/live", response_model=LiveResponse)
def liveness() -> LiveResponse:
    return LiveResponse()


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "Server is not ready"}},
)
def readiness(request: Request) -> JSONResponse:
    report = inspect_readiness(request.app.state.settings)
    return JSONResponse(
        report.model_dump(mode="json"),
        status_code=200 if report.status == "ready" else 503,
        headers={"Cache-Control": "no-store"},
    )
