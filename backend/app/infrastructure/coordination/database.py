from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.coordination import (
    CoordinationLease,
    CoordinationLeaseLost,
    CoordinationLockUnavailable,
)
from app.domain.enums import TaskStatus
from app.domain.tasks import TaskScope
from app.infrastructure.db.models import ProcessingTaskModel

_COORDINATION_TASK_TYPE = "SYSTEM_COORDINATION_LOCK"
_IDEMPOTENCY_PREFIX = "system-coordination:"


def _now() -> datetime:
    return datetime.now(UTC)


class DatabaseCoordinationLock:
    """Named, renewable leases stored in the existing durable task database.

    Coordination rows use a terminal task status, so the normal task claim loop never sees them.
    Only the lease fields are mutable while an operation is active.  Each mutation runs in a short
    transaction; application work never runs while a database write transaction is held.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: float = 120,
        acquire_timeout_seconds: float = 300,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if acquire_timeout_seconds < 0:
            raise ValueError("acquire_timeout_seconds must not be negative")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    @contextmanager
    def acquire(self, name: str, owner_id: str) -> Iterator[CoordinationLease]:
        if not name or len(name) > 220:
            raise ValueError("coordination lock name must contain 1 to 220 characters")
        if not owner_id:
            raise ValueError("coordination lock owner_id must not be empty")

        owner_token = f"{owner_id[:90]}:{uuid4()}"
        deadline = time.monotonic() + self._acquire_timeout_seconds
        lease = self._try_acquire(name, owner_token)
        while lease is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CoordinationLockUnavailable(
                    f"timed out acquiring coordination lock {name!r}"
                )
            time.sleep(min(self._poll_interval_seconds, remaining))
            lease = self._try_acquire(name, owner_token)

        stop_renewal = Event()
        lease_lost = Event()
        renewal = Thread(
            target=self._renew_until_stopped,
            args=(lease, stop_renewal, lease_lost),
            name=f"coordination-lease-{name[:32]}",
            daemon=True,
        )
        renewal.start()
        try:
            yield lease
        finally:
            stop_renewal.set()
            renewal.join(timeout=max(1.0, self._lease_seconds))
            self._release(lease)
        if lease_lost.is_set():
            raise CoordinationLeaseLost(f"coordination lease {name!r} was lost")

    def _begin_write(self, session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        else:
            session.begin()

    def _try_acquire(self, name: str, owner_token: str) -> CoordinationLease | None:
        idempotency_key = f"{_IDEMPOTENCY_PREFIX}{name}"
        scope = TaskScope.system()
        now = _now()
        expires_at = now + timedelta(seconds=self._lease_seconds)
        with self._session_factory() as session:
            try:
                self._begin_write(session)
                claimed = session.execute(
                    update(ProcessingTaskModel)
                    .where(
                        ProcessingTaskModel.scope_key == scope.scope_key,
                        ProcessingTaskModel.idempotency_key == idempotency_key,
                        or_(
                            ProcessingTaskModel.lease_expires_at.is_(None),
                            ProcessingTaskModel.lease_expires_at <= now,
                        ),
                    )
                    .values(
                        worker_id=owner_token,
                        lease_expires_at=expires_at,
                        started_at=now,
                        finished_at=None,
                    )
                )
                if claimed.rowcount == 1:
                    session.commit()
                    return CoordinationLease(name, owner_token, expires_at)

                exists = session.scalar(
                    select(ProcessingTaskModel.id).where(
                        ProcessingTaskModel.scope_key == scope.scope_key,
                        ProcessingTaskModel.idempotency_key == idempotency_key
                    )
                )
                if exists is not None:
                    session.commit()
                    return None

                session.add(
                    ProcessingTaskModel(
                        visibility=scope.visibility,
                        scope_key=scope.scope_key,
                        workspace_id=scope.workspace_id,
                        requested_by_user_id=scope.requested_by_user_id,
                        credential_id=scope.credential_id,
                        task_type=_COORDINATION_TASK_TYPE,
                        priority=0,
                        status=TaskStatus.SUCCEEDED,
                        idempotency_key=idempotency_key,
                        payload_json={"coordination_name": name},
                        result_json={},
                        attempt_count=0,
                        max_attempts=0,
                        available_at=now,
                        lease_expires_at=expires_at,
                        worker_id=owner_token,
                        started_at=now,
                    )
                )
                session.commit()
                return CoordinationLease(name, owner_token, expires_at)
            except IntegrityError:
                session.rollback()
                return None
            except BaseException:
                session.rollback()
                raise

    def _renew_until_stopped(
        self,
        lease: CoordinationLease,
        stop: Event,
        lease_lost: Event,
    ) -> None:
        interval = max(0.01, self._lease_seconds / 3)
        while not stop.wait(interval):
            try:
                renewed = self._renew(lease)
            except Exception:
                lease_lost.set()
                return
            if not renewed:
                lease_lost.set()
                return

    def _renew(self, lease: CoordinationLease) -> bool:
        expires_at = _now() + timedelta(seconds=self._lease_seconds)
        idempotency_key = f"{_IDEMPOTENCY_PREFIX}{lease.name}"
        scope_key = TaskScope.system().scope_key
        with self._session_factory() as session:
            renewed = session.execute(
                update(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.scope_key == scope_key,
                    ProcessingTaskModel.idempotency_key == idempotency_key,
                    ProcessingTaskModel.worker_id == lease.owner_token,
                )
                .values(lease_expires_at=expires_at)
            )
            session.commit()
            return renewed.rowcount == 1

    def _release(self, lease: CoordinationLease) -> None:
        idempotency_key = f"{_IDEMPOTENCY_PREFIX}{lease.name}"
        scope_key = TaskScope.system().scope_key
        with self._session_factory() as session:
            session.execute(
                update(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.scope_key == scope_key,
                    ProcessingTaskModel.idempotency_key == idempotency_key,
                    ProcessingTaskModel.worker_id == lease.owner_token,
                )
                .values(
                    worker_id=None,
                    lease_expires_at=None,
                    finished_at=_now(),
                )
            )
            session.commit()
