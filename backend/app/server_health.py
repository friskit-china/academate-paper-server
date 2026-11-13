"""Read-only health check for the complete, single-container Server runtime.

This is deliberately separate from v1 /health/ready: that existing endpoint is also used by
legacy BFF deployments before the first-run ceremony has completed.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.application.runtime import RuntimeSetupState
from app.config import Settings, get_settings
from app.domain.runtime import RuntimeEdition
from app.domain.worker_runtime import WorkerRuntimeState
from app.infrastructure.db import build_session_factory
from app.infrastructure.runtime_installation import DatabaseWorkerReadinessProbe
from app.infrastructure.schema_revision import DatabaseSchemaRevisionInvariant
from app.infrastructure.worker_runtime import DatabaseWorkerHeartbeatStore

DEFAULT_STATUS_FILE = Path("/tmp/academate-server-status.json")
STATUS_MAX_AGE_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ServerHealth:
    ok: bool
    code: str
    installation_state: str | None = None
    worker_state: str | None = None


class HealthCheckFailed(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _read_status(
    status_file: Path,
    *,
    now: float,
    pid_alive: Callable[[int], bool],
) -> dict[str, Any]:
    try:
        with status_file.open("rb") as source:
            encoded = source.read(65537)
        if len(encoded) > 65536:
            raise ValueError("oversized")
        state = json.loads(encoded)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("unsupported")
        observed = state.get("observed_at")
        if not _number(observed) or not 0 <= now - observed <= STATUS_MAX_AGE_SECONDS:
            raise HealthCheckFailed("SUPERVISOR_STATUS_STALE")
        if state.get("phase") != "running":
            raise HealthCheckFailed("SERVER_NOT_RUNNING")
        supervisor_pid = state.get("supervisor_pid")
        if type(supervisor_pid) is not int or supervisor_pid < 1 or not pid_alive(supervisor_pid):
            raise HealthCheckFailed("SUPERVISOR_UNAVAILABLE")
        components = state["components"]
        optional = [name for name in ("web", "https") if name in components]
        for name in ("api", "worker", "verifier", *optional):
            component = components[name]
            if not isinstance(component, dict):
                raise ValueError("invalid component")
            returncode = component.get("returncode")
            if name == "verifier" and type(returncode) is int and returncode == 0:
                continue
            if returncode is not None:
                raise HealthCheckFailed("SERVER_COMPONENT_EXITED")
            pid = component.get("pid")
            if type(pid) is not int or pid < 1 or not pid_alive(pid):
                raise HealthCheckFailed("SERVER_COMPONENT_UNAVAILABLE")
        started = components["worker"].get("started_at")
        if not _number(started) or now - started < 0:
            raise ValueError("invalid start time")
        return components
    except HealthCheckFailed:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise HealthCheckFailed("SUPERVISOR_STATUS_INVALID") from None


def _api_ready(port: int) -> bool:
    # HTTPConnection does not follow redirects or read deployment credentials. The target is
    # always this container's numerical loopback; no caller-controlled hostname is accepted.
    connection = http.client.HTTPConnection("127.0.0.1", port=port, timeout=1)
    try:
        connection.request("GET", "/api/v1/health/ready")
        response = connection.getresponse()
        body = response.read(4097)
        return (
            response.status == 200
            and len(body) <= 4096
            and json.loads(body).get("status") == "ok"
        )
    except (OSError, ValueError, http.client.HTTPException, AttributeError):
        return False
    finally:
        connection.close()


def _web_ready(port: int) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port=port, timeout=1)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status in {200, 301, 302, 303, 307, 308}
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _readonly_engine(database_url: str):
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise HealthCheckFailed("SERVER_DATABASE_UNSUPPORTED")
    uri = Path(url.database).resolve().as_uri() + "?mode=ro"

    def connect():
        connection = sqlite3.connect(uri, uri=True, timeout=1, check_same_thread=False)
        connection.execute("PRAGMA query_only=ON")
        return connection

    return create_engine("sqlite://", creator=connect, poolclass=NullPool)


def probe_server(
    settings: Settings,
    *,
    status_file: Path = DEFAULT_STATUS_FILE,
    api_port: int = 8000,
    web_enabled: bool = False,
    web_port: int = 3000,
    now: float | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
    api_ready: Callable[[int], bool] = _api_ready,
    web_ready: Callable[[int], bool] = _web_ready,
) -> ServerHealth:
    """Observe existing state only; never create/upgrade a database or issue bootstrap proof."""
    clock = time.time() if now is None else now
    installation_state = None
    worker_state = None
    engine = None
    try:
        profile = settings.validate_runtime_configuration()
        if profile.edition is not RuntimeEdition.SERVER:
            raise HealthCheckFailed("SERVER_CONFIGURATION_INVALID")
        components = _read_status(status_file, now=clock, pid_alive=pid_alive)
        if web_enabled and "web" not in components:
            raise HealthCheckFailed("SERVER_WEB_UNAVAILABLE")
        if (web_enabled or "web" in components) and not web_ready(web_port):
            raise HealthCheckFailed("SERVER_WEB_UNAVAILABLE")
        if not api_ready(api_port):
            raise HealthCheckFailed("SERVER_API_UNAVAILABLE")
        engine = _readonly_engine(settings.database_url)
        factory = build_session_factory(engine)
        DatabaseSchemaRevisionInvariant(factory).verify()
        readiness = DatabaseWorkerReadinessProbe(factory, profile)
        snapshot = readiness.inspect()
        installation_state = snapshot.setup_state.value
        if snapshot.setup_state in {
            RuntimeSetupState.BLOCKED, RuntimeSetupState.RECOVERABLE_FAILURE,
        }:
            raise HealthCheckFailed("INSTALLATION_REQUIRES_ATTENTION")
        if snapshot.setup_state is not RuntimeSetupState.READY:
            return ServerHealth(True, "AWAITING_SETUP", installation_state)
        readiness.is_ready()  # Reuse the worker's full owner/permission invariants.
        heartbeat = DatabaseWorkerHeartbeatStore(factory).latest()
        if heartbeat is None:
            raise HealthCheckFailed("WORKER_HEARTBEAT_MISSING")
        worker_state = heartbeat.state.value
        observed = heartbeat.observed_at.timestamp()
        max_age = max(15.0, settings.worker_heartbeat_seconds * 3)
        if (
            observed < components["worker"]["started_at"]
            or not -5 <= clock - observed <= max_age
        ):
            raise HealthCheckFailed("WORKER_HEARTBEAT_STALE")
        if heartbeat.state is not WorkerRuntimeState.READY or not heartbeat.accepting_tasks:
            raise HealthCheckFailed("WORKER_NOT_READY")
        return ServerHealth(True, "READY", installation_state, worker_state)
    except HealthCheckFailed as error:
        return ServerHealth(False, error.code, installation_state, worker_state)
    except Exception:
        # Avoid echoing URLs, environment values, user identifiers or SQL diagnostics.
        return ServerHealth(False, "SERVER_STATE_INVALID", installation_state, worker_state)
    finally:
        if engine is not None:
            engine.dispose()


def main() -> int:
    try:
        settings = get_settings()
        port = int(os.environ.get("SERVER_API_PORT", str(settings.app_port)))
        web_port = int(os.environ.get("SERVER_WEB_PORT", "3000"))
        if not 1 <= port <= 65535 or not 1 <= web_port <= 65535:
            raise ValueError("invalid port")
        report = probe_server(
            settings,
            status_file=Path(os.environ.get("SERVER_STATUS_FILE", str(DEFAULT_STATUS_FILE))),
            api_port=port,
            web_enabled=os.environ.get("SERVER_WEB_ENABLED", "false").lower() == "true",
            web_port=web_port,
        )
    except Exception:
        report = ServerHealth(False, "SERVER_CONFIGURATION_INVALID")
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
