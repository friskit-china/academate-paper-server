from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IssuedLocalLaunchToken:
    """One-use browser launch proof handed only to a trusted launcher or CLI."""

    raw_token: str = field(repr=False)
    token_digest: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedLocalSessionToken:
    """Opaque browser session credential; persistence receives only ``token_digest``."""

    raw_token: str = field(repr=False)
    token_digest: str
    expires_at: datetime


class LocalSessionTokenCodec(Protocol):
    """Generate and domain-separate launch proofs from session credentials."""

    def issue_launch(self, *, now: datetime) -> IssuedLocalLaunchToken: ...

    def issue_session(self, *, now: datetime) -> IssuedLocalSessionToken: ...

    def launch_digest(self, raw_token: str) -> str: ...

    def session_digest(self, raw_token: str) -> str: ...
