import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from typing import Any, TypeVar

from app.domain.ports import QueuedTask
from app.domain.tasks import (
    TaskLeaseLost,
    TaskScope,
    redact_task_error_message,
    require_secret_free_task_data,
)

_MutationResult = TypeVar("_MutationResult")


class InMemoryTaskQueue:
    """Deterministic adapter for tests and early application wiring."""

    def __init__(self) -> None:
        self._tasks: list[QueuedTask] = []
        self._by_idempotency_key: dict[tuple[str, str], QueuedTask] = {}
        self._claims: dict[str, tuple[QueuedTask, str]] = {}
        self._lock = Lock()

    def _queue(self, task: QueuedTask) -> None:
        self._tasks.append(task)
        self._tasks.sort(key=lambda item: (item.priority, item.available_at, item.id))
        self._by_idempotency_key[(task.scope_key, task.idempotency_key)] = task

    def _require_owned_claim(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> QueuedTask:
        claimed = self._claims.get(task_id)
        if (
            claimed is None
            or claimed[1] != expected_worker_id
            or expected_lease_generation < 1
            or claimed[0].lease_generation != expected_lease_generation
        ):
            raise TaskLeaseLost(
                f"task {task_id} is no longer leased by worker {expected_worker_id}"
            )
        return claimed[0]

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
        with self._lock:
            identity = (scope.scope_key, idempotency_key)
            existing = self._by_idempotency_key.get(identity)
            if existing is not None:
                return existing
            task = QueuedTask(
                id=str(uuid.uuid4()),
                visibility=scope.visibility,
                scope_key=scope.scope_key,
                workspace_id=scope.workspace_id,
                requested_by_user_id=scope.requested_by_user_id,
                credential_id=scope.credential_id,
                paper_id=paper_id,
                task_type=task_type,
                payload=payload,
                priority=priority,
                idempotency_key=idempotency_key,
                available_at=datetime.now(UTC),
                max_attempts=max_attempts,
            )
            self._queue(task)
            return task

    def claim(self, worker_id: str) -> QueuedTask | None:
        with self._lock:
            if not self._tasks:
                return None
            task = self._tasks.pop(0)
            claimed = replace(
                task,
                attempt_count=task.attempt_count + 1,
                lease_generation=task.lease_generation + 1,
            )
            self._claims[task.id] = (claimed, worker_id)
            self._by_idempotency_key[(task.scope_key, task.idempotency_key)] = claimed
            return claimed

    def succeed(
        self,
        task_id: str,
        result: dict[str, Any] | None = None,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> None:
        require_secret_free_task_data(result, field_name="result")
        with self._lock:
            self._require_owned_claim(
                task_id,
                expected_worker_id=expected_worker_id,
                expected_lease_generation=expected_lease_generation,
            )
            self._claims.pop(task_id)

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
        del error_code, retry_delay_seconds
        redact_task_error_message(error_message)
        with self._lock:
            task = self._require_owned_claim(
                task_id,
                expected_worker_id=expected_worker_id,
                expected_lease_generation=expected_lease_generation,
            )
            self._claims.pop(task_id)
            will_retry = retryable and task.attempt_count < task.max_attempts
            if will_retry:
                self._queue(replace(task, available_at=datetime.now(UTC)))
            return will_retry

    def release_claim(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> bool:
        with self._lock:
            try:
                task = self._require_owned_claim(
                    task_id,
                    expected_worker_id=expected_worker_id,
                    expected_lease_generation=expected_lease_generation,
                )
            except TaskLeaseLost:
                return False
            self._claims.pop(task_id)
            self._queue(
                replace(
                    task,
                    attempt_count=max(0, task.attempt_count - 1),
                    available_at=datetime.now(UTC),
                )
            )
            return True

    def renew_claim(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> bool:
        with self._lock:
            try:
                self._require_owned_claim(
                    task_id,
                    expected_worker_id=expected_worker_id,
                    expected_lease_generation=expected_lease_generation,
                )
            except TaskLeaseLost:
                return False
            return True

    def execute_claim_mutation(
        self,
        task_id: str,
        mutation: Callable[[], _MutationResult],
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> _MutationResult:
        with self._lock:
            self._require_owned_claim(
                task_id,
                expected_worker_id=expected_worker_id,
                expected_lease_generation=expected_lease_generation,
            )
            return mutation()
