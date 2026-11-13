from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class InstallationIdentityInvariantError(RuntimeError):
    """The durable Local owner/permission graph is incomplete or ambiguous."""


@dataclass(frozen=True, slots=True)
class InstallationIdentitySnapshot:
    owner_user_id: str
    workspace_id: str


class InstallationIdentityInvariant(Protocol):
    def verify(self) -> InstallationIdentitySnapshot: ...
