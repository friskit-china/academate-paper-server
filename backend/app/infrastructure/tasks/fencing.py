from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.domain.enums import TaskStatus
from app.domain.tasks import TaskLeaseLost
from app.infrastructure.db.models import ProcessingTaskModel

_SESSION_FENCE_KEY = "processing_task_claim_fence"


@dataclass(frozen=True, slots=True)
class TaskClaimFence:
    task_id: str
    worker_id: str
    lease_generation: int
    lease_seconds: int
    enforce_access: bool = False

    def __post_init__(self) -> None:
        if (
            not self.task_id
            or not self.worker_id
            or self.lease_generation < 1
            or self.lease_seconds < 1
        ):
            raise ValueError(
                "A task claim fence requires a task, worker, positive generation, and lease."
            )


@dataclass(frozen=True, slots=True)
class ClaimFencedSessionFactory:
    """Decorate every handler session so commits remain owned by the live claim."""

    base_factory: Any
    fence: TaskClaimFence

    def __call__(self, *args: Any, **kwargs: Any) -> Session:
        supplied_info = dict(kwargs.pop("info", {}) or {})
        supplied_info[_SESSION_FENCE_KEY] = self.fence
        return self.base_factory(*args, info=supplied_info, **kwargs)


@event.listens_for(Session, "before_commit")
def _fence_handler_commit(session: Session) -> None:
    fence = session.info.get(_SESSION_FENCE_KEY)
    if not isinstance(fence, TaskClaimFence):
        return

    # Flush business mutations first. On SQLite this obtains the single-writer lock; on
    # row-locking databases the subsequent SELECT FOR UPDATE serializes with reclaim. A failed
    # fence rolls the entire transaction back, including this flush.
    session.flush()
    now = datetime.now(UTC)
    task = session.scalar(
        select(ProcessingTaskModel)
        .where(
            ProcessingTaskModel.id == fence.task_id,
            ProcessingTaskModel.status == TaskStatus.RUNNING,
            ProcessingTaskModel.worker_id == fence.worker_id,
            ProcessingTaskModel.lease_generation == fence.lease_generation,
            ProcessingTaskModel.lease_expires_at.is_not(None),
            ProcessingTaskModel.lease_expires_at > now,
        )
        .with_for_update()
        .execution_options(autoflush=False)
    )
    if task is None:
        raise TaskLeaseLost(
            f"task {fence.task_id} is no longer leased by worker {fence.worker_id}"
        )
    if fence.enforce_access:
        from app.application.task_access import require_task_access

        require_task_access(session, task)
    task.lease_expires_at = now + timedelta(seconds=fence.lease_seconds)
    session.flush()


def claim_fenced_session_factory(
    base_factory: Any,
    *,
    task_id: str,
    worker_id: str,
    lease_generation: int,
    lease_seconds: int,
    enforce_access: bool = False,
) -> ClaimFencedSessionFactory:
    return ClaimFencedSessionFactory(
        base_factory=base_factory,
        fence=TaskClaimFence(
            task_id=task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            lease_seconds=lease_seconds,
            enforce_access=enforce_access,
        ),
    )
