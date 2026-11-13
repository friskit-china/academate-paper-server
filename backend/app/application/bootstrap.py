from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.application.runtime import RuntimeSetupState
from app.domain.setup import BootstrapTokenIssuer, IssuedBootstrapToken


@dataclass(frozen=True, slots=True)
class BootstrapTokenRegistration:
    setup_state: RuntimeSetupState
    state_version: int


class BootstrapTokenRegistry(Protocol):
    """Trusted persistence port; it stores only digest and lifecycle metadata."""

    def register(
        self,
        issued: IssuedBootstrapToken,
        *,
        now: datetime,
    ) -> BootstrapTokenRegistration: ...


@dataclass(frozen=True, slots=True)
class TrustedBootstrapTokenService:
    """Issue a one-time owner token for a trusted launcher, never for a browser route."""

    issuer: BootstrapTokenIssuer
    registry: BootstrapTokenRegistry

    def issue_owner_token(
        self,
        *,
        now: datetime | None = None,
    ) -> IssuedBootstrapToken:
        issued_at = now or datetime.now(UTC)
        if issued_at.tzinfo is None:
            raise ValueError("Bootstrap issuance time must be timezone-aware.")
        issued = self.issuer.issue(now=issued_at)
        self.registry.register(issued, now=issued_at)
        return issued
