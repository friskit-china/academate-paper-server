from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class InstallationMode(StrEnum):
    """Write-once product mode selected during the first-run setup ceremony."""

    SINGLE_USER = "SINGLE_USER"
    MULTI_USER = "MULTI_USER"


@dataclass(frozen=True, slots=True)
class IssuedBootstrapToken:
    """One-time bootstrap proof.

    ``raw_token`` is returned to the trusted launcher exactly once.  Its representation is
    deliberately redacted so an exception or debug log cannot accidentally disclose it.  Only
    ``token_digest`` and the lifecycle metadata belong in persistent storage.
    """

    raw_token: str = field(repr=False)
    token_digest: str
    expires_at: datetime


class BootstrapTokenIssuer(Protocol):
    def issue(self, *, now: datetime | None = None) -> IssuedBootstrapToken: ...

    def matches(self, raw_token: str, token_digest: str) -> bool: ...


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password: str, encoded_hash: str) -> bool: ...
