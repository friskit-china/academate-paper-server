from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Protocol, TypeVar

from app.domain.tasks import TaskScope, TaskVisibility

_MutationResult = TypeVar("_MutationResult")


class Storage(Protocol):
    def put(self, key: str, stream: BinaryIO, metadata: dict[str, str] | None = None) -> None: ...

    def get(self, key: str) -> BinaryIO: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def delete_prefix(self, prefix: str) -> int: ...

    def list(self, prefix: str = "") -> list[str]: ...

    def internal_url(self, key: str) -> str: ...


@dataclass(frozen=True, slots=True)
class QueuedTask:
    id: str
    paper_id: str | None
    task_type: str
    payload: dict[str, Any]
    priority: int
    idempotency_key: str
    available_at: datetime
    attempt_count: int = 0
    lease_generation: int = 0
    max_attempts: int = 3
    visibility: TaskVisibility = TaskVisibility.SYSTEM
    scope_key: str = "system:compatibility"
    workspace_id: str | None = None
    requested_by_user_id: str | None = None
    credential_id: str | None = None


class TaskQueue(Protocol):
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
    ) -> QueuedTask: ...

    def claim(self, worker_id: str) -> QueuedTask | None: ...

    def succeed(
        self,
        task_id: str,
        result: dict[str, Any] | None = None,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> None: ...

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
    ) -> bool: ...

    def release_claim(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> bool: ...

    def renew_claim(
        self,
        task_id: str,
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> bool: ...

    def execute_claim_mutation(
        self,
        task_id: str,
        mutation: Callable[[], _MutationResult],
        *,
        expected_worker_id: str,
        expected_lease_generation: int,
    ) -> _MutationResult: ...
