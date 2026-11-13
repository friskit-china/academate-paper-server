from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CoordinationLease:
    """An owned, expiring lease for one named coordination operation."""

    name: str
    owner_token: str
    expires_at: datetime


class CoordinationLockUnavailable(RuntimeError):
    """Raised when a named coordination lease cannot be acquired in time."""


class CoordinationLeaseLost(RuntimeError):
    """Raised when an acquired coordination lease can no longer be renewed."""


class CoordinationLock(Protocol):
    """OS-neutral port for serializing short application coordination work."""

    def acquire(
        self,
        name: str,
        owner_id: str,
    ) -> AbstractContextManager[CoordinationLease]: ...
