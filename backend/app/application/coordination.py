from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from app.domain.coordination import CoordinationLock

ResultT = TypeVar("ResultT")


class StartupBackfillCoordinator:
    """Run idempotent startup publication behind a named database lease."""

    def __init__(self, coordination_lock: CoordinationLock) -> None:
        self._coordination_lock = coordination_lock

    def run(
        self,
        *,
        name: str,
        owner_id: str,
        operation: Callable[[], ResultT],
    ) -> ResultT:
        with self._coordination_lock.acquire(name, owner_id):
            return operation()
