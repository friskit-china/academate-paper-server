from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.application.web_sessions import WebLoginResult, WebSessionBinding
from app.domain.invitations import InvitationTokenCodec
from app.domain.setup import PasswordHasher
from app.domain.users import (
    normalized_display_name,
    normalized_username,
    validated_password,
)
from app.domain.web_sessions import IssuedWebSessionToken, WebSessionTokenCodec


class RegistrationInviteInvalidError(PermissionError):
    """The anonymous proof cannot be used; lifecycle details must not be disclosed."""


class RegistrationUnavailableError(PermissionError):
    """Persisted installation policy does not permit invitation registration."""


class RegistrationUsernameConflictError(RuntimeError):
    pass


class RegistrationStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RegistrationProvisioning:
    user_id: str
    username: str
    display_name: str
    password_hash: str = field(repr=False)
    invitation_digest: str = field(repr=False)
    session_id: str
    issued_session: IssuedWebSessionToken = field(repr=False)
    created_at: datetime


class RegistrationRepository(Protocol):
    def register(self, provisioning: RegistrationProvisioning) -> WebSessionBinding: ...


class IdentifierFactory(Protocol):
    def __call__(self) -> str: ...


def _new_identifier() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class RegistrationService:
    repository: RegistrationRepository
    invitation_codec: InvitationTokenCodec
    web_session_codec: WebSessionTokenCodec
    password_hasher: PasswordHasher
    identifier_factory: IdentifierFactory = _new_identifier

    def register(
        self,
        *,
        invite_token: str,
        username: str,
        display_name: str,
        password: str,
        now: datetime | None = None,
    ) -> WebLoginResult:
        registered_at = self._aware_time(now)
        if not self._valid_raw_invitation_token(invite_token):
            raise RegistrationInviteInvalidError(
                "This invitation cannot be used. Ask an administrator for a new invitation."
            )
        canonical_username = normalized_username(username)
        canonical_display_name = normalized_display_name(display_name)
        validated = validated_password(password)
        issued_session = self.web_session_codec.issue(now=registered_at)
        binding = self.repository.register(
            RegistrationProvisioning(
                user_id=self.identifier_factory(),
                username=canonical_username,
                display_name=canonical_display_name,
                password_hash=self.password_hasher.hash(validated),
                invitation_digest=self.invitation_codec.digest(invite_token),
                session_id=self.identifier_factory(),
                issued_session=issued_session,
                created_at=registered_at,
            )
        )
        return WebLoginResult(
            raw_session_token=issued_session.raw_token,
            user_id=binding.user_id,
            active_workspace_id=binding.active_workspace_id,
            expires_at=binding.expires_at,
        )

    @staticmethod
    def _valid_raw_invitation_token(value: str) -> bool:
        if not isinstance(value, str) or not value.isascii():
            return False
        encoded = value.encode("ascii")
        return 32 <= len(encoded) <= 512

    @staticmethod
    def _aware_time(value: datetime | None) -> datetime:
        resolved = value or datetime.now(UTC)
        if resolved.tzinfo is None:
            raise ValueError("Registration timestamps must be timezone-aware.")
        return resolved
