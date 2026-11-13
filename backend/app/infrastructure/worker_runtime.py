from __future__ import annotations

import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event, Thread
from types import FrameType
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.worker_runtime import WorkerLifecycle
from app.domain.enums import TaskStatus
from app.domain.tasks import TaskScope, require_secret_free_task_data
from app.domain.worker_runtime import (
    WorkerHeartbeat,
    WorkerRuntimeState,
)
from app.infrastructure.db.models import ProcessingTaskModel

WORKER_HEARTBEAT_TASK_TYPE = "SYSTEM_WORKER_HEARTBEAT"
_WORKER_HEARTBEAT_IDEMPOTENCY_PREFIX = "worker-runtime-heartbeat:"


class SignalStopRequest:
    """Translate process signals into the OS-neutral StopRequest contract."""

    def __init__(self) -> None:
        self._event = Event()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_seconds: float) -> bool:
        return self._event.wait(max(0.0, timeout_seconds))

    def request_stop(self, _signum: int = 0, _frame: FrameType | None = None) -> None:
        self._event.set()

    def install(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class WorkerHeartbeatPump:
    """Keep heartbeats fresh while the main thread performs a synchronous task."""

    def __init__(
        self,
        lifecycle: WorkerLifecycle,
        *,
        tick_seconds: float,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("heartbeat pump tick must be positive")
        self._lifecycle = lifecycle
        self._tick_seconds = tick_seconds
        self._on_tick = on_tick
        self._stop = Event()
        self._thread = Thread(
            target=self._run,
            name="worker-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._tick_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                if self._on_tick is not None:
                    self._on_tick()
                self._lifecycle.publish(force=False)
            except Exception:
                # The main worker remains authoritative.  A later tick retries persistence while
                # the Supervisor independently observes component liveness.
                continue


class DatabaseWorkerHeartbeatStore:
    """Persist safe worker summaries in terminal SYSTEM rows ignored by normal claims."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _idempotency_key(instance_id: str) -> str:
        return f"{_WORKER_HEARTBEAT_IDEMPOTENCY_PREFIX}{instance_id}"

    def publish(self, heartbeat: WorkerHeartbeat) -> None:
        payload: dict[str, object] = {}
        summary = heartbeat.safe_summary()
        require_secret_free_task_data(payload, field_name="worker heartbeat payload")
        require_secret_free_task_data(summary, field_name="worker heartbeat")
        scope = TaskScope.system()
        idempotency_key = self._idempotency_key(heartbeat.instance_id)
        with self._session_factory() as session:
            try:
                row = session.scalar(
                    select(ProcessingTaskModel).where(
                        ProcessingTaskModel.scope_key == scope.scope_key,
                        ProcessingTaskModel.idempotency_key == idempotency_key,
                    )
                )
                if row is None:
                    row = ProcessingTaskModel(
                        visibility=scope.visibility,
                        scope_key=scope.scope_key,
                        task_type=WORKER_HEARTBEAT_TASK_TYPE,
                        priority=0,
                        status=TaskStatus.SUCCEEDED,
                        idempotency_key=idempotency_key,
                        payload_json=payload,
                        result_json=summary,
                        attempt_count=0,
                        max_attempts=0,
                        available_at=heartbeat.observed_at,
                        started_at=heartbeat.observed_at,
                        finished_at=heartbeat.observed_at,
                    )
                    session.add(row)
                else:
                    row.status = TaskStatus.SUCCEEDED
                    row.result_json = summary
                    row.finished_at = heartbeat.observed_at
                    row.lease_expires_at = None
                    row.worker_id = None
                    row.error_code = None
                    row.error_message = None
                session.commit()
            except IntegrityError:
                # Concurrent publication can only race on first insert.  Retry as an update.
                session.rollback()
                row = session.scalar(
                    select(ProcessingTaskModel).where(
                        ProcessingTaskModel.scope_key == scope.scope_key,
                        ProcessingTaskModel.idempotency_key == idempotency_key,
                    )
                )
                if row is None:
                    raise
                row.status = TaskStatus.SUCCEEDED
                row.result_json = summary
                row.finished_at = heartbeat.observed_at
                row.lease_expires_at = None
                row.worker_id = None
                session.commit()

    def latest(self) -> WorkerHeartbeat | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ProcessingTaskModel)
                .where(ProcessingTaskModel.task_type == WORKER_HEARTBEAT_TASK_TYPE)
                .order_by(ProcessingTaskModel.finished_at.desc())
                .limit(1)
            )
            if row is None or row.result_json is None:
                return None
            summary: dict[str, Any] = row.result_json
        from app.domain.os_adapters import ToolchainCapability

        return WorkerHeartbeat(
            schema_version=int(summary["schema_version"]),
            instance_id=str(summary["instance_id"]),
            state=WorkerRuntimeState(str(summary["state"])),
            observed_at=datetime.fromisoformat(str(summary["observed_at"])),
            accepting_tasks=bool(summary["accepting_tasks"]),
            active_tasks=int(summary["active_tasks"]),
            task_capacity=int(summary["task_capacity"]),
            runtime_capabilities=tuple(
                ToolchainCapability(str(value))
                for value in summary.get("runtime_capabilities", [])
            ),
        )
