from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class InvitationIssuerChannel(StrEnum):
    WEB_ADMIN = "WEB_ADMIN"
    OPERATOR_CLI = "OPERATOR_CLI"


class InvitationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REDEEMED = "REDEEMED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class IssuedInvitationToken:
    """One-time invitation proof; only its digest and masked hint may be persisted."""

    raw_token: str = field(repr=False)
    token_digest: str
    masked_hint: str


class InvitationTokenCodec(Protocol):
    def issue(self) -> IssuedInvitationToken: ...

    def digest(self, raw_token: str) -> str: ...

    def matches(self, raw_token: str, token_digest: str) -> bool: ...


def derive_invitation_status(
    *,
    invitation_generation: int,
    current_generation: int,
    expires_at: datetime,
    redeemed_at: datetime | None,
    revoked_at: datetime | None,
    now: datetime,
) -> InvitationStatus:
    """Derive lifecycle state without persisting a second, drift-prone status column."""

    if redeemed_at is not None:
        return InvitationStatus.REDEEMED
    if revoked_at is not None:
        return InvitationStatus.REVOKED
    if invitation_generation != current_generation or now >= expires_at:
        return InvitationStatus.EXPIRED
    return InvitationStatus.ACTIVE
