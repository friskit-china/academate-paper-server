from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IssuedWebSessionToken:
    """A browser credential whose raw value must never be persisted or logged."""

    raw_token: str = field(repr=False)
    token_digest: str
    expires_at: datetime


class WebSessionTokenCodec(Protocol):
    def issue(self, *, now: datetime) -> IssuedWebSessionToken: ...

    def digest(self, raw_token: str) -> str: ...
