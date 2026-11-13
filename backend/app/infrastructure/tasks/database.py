from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import TaskStatus
from app.domain.ports import QueuedTask
from app.domain.tasks import (
    TaskLeaseLost,
    TaskPaidConfirmationRequired,
    TaskScope,
    TaskVisibility,
    redact_task_error_message,
    require_secret_free_task_data,
)
from app.infrastructure.db.models import ProcessingTaskModel

_MutationResult = TypeVar("_MutationResult")


def _now() -> datetime:
    return datetime.now(UTC)


class DatabaseTaskQueue:
    """SQLite-safe durable queue using short transactions and expiring leases."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int = 120,
        enforce_access: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds
        self._enforce_access = enforce_access

    @staticmethod
    def _as_task(model: ProcessingTaskModel) -> QueuedTask:
        return QueuedTask(
            id=model.id,
            visibility=TaskVisibility(model.visibility),
            scope_key=model.scope_key,
            workspace_id=model.workspace_id,
            requested_by_user_id=model.requested_by_user_id,
            credential_id=model.credential_id,
            paper_id=model.paper_id,
            task_type=model.task_type,
            payload=model.payload_json,
            priority=model.priority,
            idempotency_key=model.idempotency_key,
            available_at=model.available_at,
            attempt_count=model.attempt_count,
            lease_generation=model.lease_generation,
            max_attempts=model.max_attempts,
        )

    def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int,
        idempotency_key: str,
        *,
        scope: TaskScope,
        paper_id: str | None = None,
        max_attempts: int = 3,
    ) -> QueuedTask:
        require_secret_free_task_data(payload, field_name="payload")
        session = self._session_factory()
        try:
            self._begin_write(session)
            existing = session.scalar(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.scope_key == scope.scope_key,
                    ProcessingTaskModel.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return self._as_task(existing)
            task = ProcessingTaskModel(
                visibility=scope.visibility,
                scope_key=scope.scope_key,
                workspace_id=scope.workspace_id,
                requested_by_user_id=scope.requested_by_user_id,
                credential_id=scope.credential_id,
                paper_id=paper_id,
                task_type=task_type,
                payload_json=payload,
                priority=priority,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return self._as_task(task)
        except IntegrityError:
            # Another process may win the same scoped idempotency key between the
            # read and insert on databases without SQLite's BEGIN IMMEDIATE lock.
            session.rollback()
            with self._session_factory() as retry_session:
                existing = retry_session.scalar(
                    select(ProcessingTaskModel).where(
                        ProcessingTaskModel.scope_key == scope.scope_key,
                        ProcessingTaskModel.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                return self._as_task(existing)
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def resume_unfinished(self) -> int:
        """Requeue abandoned attempts in place. Live leases and retry schedules stay intact."""
        with self._session_factory() as session:
            self._begin_write(session)
            now = _now()
            task = ProcessingTaskModel
            changed = session.execute(
                update(task)
                .where(
                    task.status == TaskStatus.RUNNING,
                    or_(
                        task.worker_id.is_(None),
                        task.lease_expires_at.is_(None),
                        task.lease_expires_at <= now,
                    ),
                )
                .values(
                    status=TaskStatus.QUEUED, worker_id=None, lease_expires_at=None,
                    available_at=now, lease_generation=task.lease_generation + 1,
                )
            )
            session.commit()
            return changed.rowcount

    def claim(self, worker_id: str) -> QueuedTask | None:
        session = self._session_factory()
        try:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            else:
                session.begin()
            now = _now()
            claim_statement = (
                select(ProcessingTaskModel)
                .where(
                    or_(
                        and_(
                            ProcessingTaskModel.status == TaskStatus.QUEUED,
                            ProcessingTaskModel.available_at <= now,
                        ),
                        and_(
                            ProcessingTaskModel.status == TaskStatus.RUNNING,
                            ProcessingTaskModel.lease_expires_at.is_not(None),
                            ProcessingTaskModel.lease_expires_at <= now,
                        ),
                    )
                )
                .order_by(
                    ProcessingTaskModel.priority,
                    ProcessingTaskModel.available_at,
                    ProcessingTaskModel.created_at,
                )
                .limit(1)
            )
            if session.bind is not None and session.bind.dialect.name != "sqlite":
                claim_statement = claim_statement.with_for_update(skip_locked=True)
            task = session.scalar(claim_statement)
            if task is None:
                session.commit()
                return None
            task.status = TaskStatus.RUNNING
            task.worker_id = worker_id
            task.started_at = task.started_at or now
            task.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            task.attempt_count += 1
            task.lease_generation += 1
            task.error_code = None
            task.error_message = None
            session.commit()
            return self._as_task(task)
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _begin_write(session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        else:
            session.begin()

    @staticmethod
    def _require_owned_claim(
        task: ProcessingTaskModel | None,
        *,
        task_id: str,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> ProcessingTaskModel | None:
        lease_expires_at = task.lease_expires_at if task is not None else None
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            # SQLite returns timezone-aware columns as naive UTC datetimes.
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if (
            task is None
            or task.status != TaskStatus.RUNNING
            or task.worker_id != expected_worker_id
            or expected_lease_generation < 1
            or task.lease_generation != expected_lease_generation
            or lease_expires_at is None
            or lease_expires_at <= _now()
        ):
            raise TaskLeaseLost(
                f"task {task_id} is no longer leased by worker {expected_worker_id}"
            )
        return task

    def validate_provider_access(
        self, task_id: str, *, expected_worker_id: str, expected_lease_generation: int,
    ) -> None:
        """Short read-only preflight immediately before a provider call.

        No database lock is held across network I/O. Revocation cannot retract a remote call
        already in flight; result and filesystem publication are fenced again afterward.
        """
        with self._session_factory() as session:
            task = self._require_owned_claim(
                session.get(ProcessingTaskModel, task_id), task_id=task_id,
                expected_worker_id=expected_worker_id,
                expected_lease_generation=expected_lease_generation,
            )
            if self._enforce_access:
                from app.application.task_access import require_task_access

                require_task_access(session, task)

    def succeed(
        self,
        task_id: str,
        result: dict[str, Any] | None = None,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> None:
        require_secret_free_task_data(result, field_name="result")
        session = self._session_factory()
        try:
            self._begin_write(session)
            task = session.scalar(
                select(ProcessingTaskModel)
                .where(ProcessingTaskModel.id == task_id)
                .with_for_update()
            )
            task = self._require_owned_claim(
                task,
                task_id=task_id,
                expected_worker_id=expected_worker_id,
                expected_lease_generation=expected_lease_generation,
            )
            if task is None:
                session.commit()
                return
            if task.status == TaskStatus.CANCELLED:
                session.commit()
                return
            if self._enforce_access:
                from app.application.task_access import require_task_access

                require_task_access(session, task)
            task.status = TaskStatus.SUCCEEDED
            task.result_json = result or {}
            task.finished_at = _now()
            task.lease_expires_at = None
            task.worker_id = None
            task.error_code = None
            task.error_message = None
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def fail(
        self,
        task_id: str,
        error_code: str,
        error_message: str,
        *,
        retryable: bool = True,
        retry_delay_seconds: int | None = None,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> bool:
        """Record a failure and return True when the task was scheduled to retry."""
        session = self._session_factory()
        try:
            self._begin_write(session)
            task = session.scalar(
                select(ProcessingTaskModel)
                .where(ProcessingTaskModel.id == task_id)
                .with_for_update()
            )
            task = self._require_owned_claim(
                task,
                task_id=task_id,
                expected_worker_id=expected_worker_id,
                expected_lease_generation=expected_lease_generation,
            )
            if task is None:
                session.commit()
                return False
            if task.status == TaskStatus.CANCELLED:
                session.commit()
                return False
            task.error_code = error_code[:64]
            task.error_message = redact_task_error_message(error_message)
            task.lease_expires_at = None
            task.worker_id = None
            will_retry = retryable and task.attempt_count < task.max_attempts
            if will_retry:
                task.status = TaskStatus.QUEUED
                if retry_delay_seconds is None:
                    base_delay = min(60, 2**task.attempt_count)
                    delay = base_delay + random.uniform(0, base_delay * 0.25)
                else:
                    delay = min(300, max(0, retry_delay_seconds))
                task.available_at = _now() + timedelta(seconds=delay)
            else:
                task.status = TaskStatus.FAILED
                task.finished_at = _now()
            session.commit()
            return will_retry
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def release_claim(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> bool:
        """Undo a stop-race claim before task execution starts."""

        session = self._session_factory()
        try:
            self._begin_write(session)
            task = session.scalar(
                select(ProcessingTaskModel)
                .where(ProcessingTaskModel.id == task_id)
                .with_for_update()
            )
            try:
                task = self._require_owned_claim(
                    task,
                    task_id=task_id,
                    expected_worker_id=expected_worker_id,
                    expected_lease_generation=expected_lease_generation,
                )
            except TaskLeaseLost:
                session.commit()
                return False
            assert task is not None
            task.status = TaskStatus.QUEUED
            task.worker_id = None
            task.lease_expires_at = None
            task.available_at = _now()
            task.attempt_count = max(0, task.attempt_count - 1)
            if task.attempt_count == 0:
                task.started_at = None
            session.commit()
            return True
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def renew_claim(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> bool:
        """Extend a live claim without allowing a stale worker to retake ownership."""

        session = self._session_factory()
        try:
            self._begin_write(session)
            task = session.scalar(
                select(ProcessingTaskModel)
                .where(ProcessingTaskModel.id == task_id)
                .with_for_update()
            )
            try:
                task = self._require_owned_claim(
                    task,
                    task_id=task_id,
                    expected_worker_id=expected_worker_id,
                    expected_lease_generation=expected_lease_generation,
                )
            except TaskLeaseLost:
                session.commit()
                return False
            assert task is not None
            task.lease_expires_at = _now() + timedelta(seconds=self._lease_seconds)
            session.commit()
            return True
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def execute_claim_mutation(
        self,
        task_id: str,
        mutation: Callable[[], _MutationResult],
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> _MutationResult:
        """Run one short local mutation while the live claim remains serialized.

        The callback is deliberately limited to an already-prepared atomic filesystem mutation.
        Holding the SQLite write transaction (or the task row lock on other databases) across that
        callback closes the validation-to-publish gap without putting file preparation or other
        long-running I/O inside the database critical section.
        """

        session = self._session_factory()
        try:
            self._begin_write(session)
            task = session.scalar(
                select(ProcessingTaskModel)
                .where(ProcessingTaskModel.id == task_id)
                .with_for_update()
            )
            task = self._require_owned_claim(
                task,
                task_id=task_id,
                expected_worker_id=expected_worker_id,
                expected_lease_generation=expected_lease_generation,
            )
            assert task is not None
            if self._enforce_access:
                from app.application.task_access import require_task_access

                require_task_access(session, task)
            result = mutation()
            task.lease_expires_at = _now() + timedelta(seconds=self._lease_seconds)
            session.commit()
            return result
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def retry_failed(
        self,
        paper_id: str,
        *,
        visible_scope_keys: frozenset[str],
        requested_by_user_id: str,
        allow_paid: bool = True,
        enforce_access: bool = False,
    ) -> int:
        if not requested_by_user_id:
            raise ValueError("The retrying user id is required.")
        with self._session_factory() as session:
            self._begin_write(session)
            tasks = session.scalars(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.paper_id == paper_id,
                    ProcessingTaskModel.scope_key.in_(visible_scope_keys),
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                    or_(
                        ProcessingTaskModel.visibility.in_(
                            (
                                TaskVisibility.GLOBAL_DERIVED.value,
                                TaskVisibility.SYSTEM.value,
                            )
                        ),
                        and_(
                            ProcessingTaskModel.visibility.in_(
                                (
                                    TaskVisibility.WORKSPACE_DERIVED.value,
                                    TaskVisibility.USER_PRIVATE.value,
                                )
                            ),
                            ProcessingTaskModel.requested_by_user_id
                            == requested_by_user_id,
                        ),
                    ),
                )
            ).all()
            paid_types = {
                "TRANSLATE_BLOCK", "REPAIR_RENDER", "ANSWER_QUESTION",
                "PLAN_PAPER_MAP", "ANSWER_MAP_QUESTION",
            }
            if not allow_paid and any(task.task_type in paid_types for task in tasks):
                raise TaskPaidConfirmationRequired(
                    "Billable retries require explicit confirmation."
                )
            if enforce_access:
                from app.application.task_access import require_task_access
                for task in tasks:
                    require_task_access(session, task)
            now = _now()
            for task in tasks:
                task.status = TaskStatus.QUEUED
                task.attempt_count = 0
                task.available_at = now
                task.started_at = None
                task.finished_at = None
                task.error_code = None
                task.error_message = None
            session.commit()
            return len(tasks)
