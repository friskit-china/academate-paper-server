from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from app.domain.auth import Permission, RequestPrincipal
from app.domain.invitations import (
    InvitationIssuerChannel,
    InvitationStatus,
    InvitationTokenCodec,
    derive_invitation_status,
)
from app.domain.runtime import IdentityMode


class InvitationDeniedError(PermissionError):
    pass


class InvitationModeError(PermissionError):
    """Invitation management is closed by persisted installation policy."""


class InvitationStateError(RuntimeError):
    """The persisted installation cannot safely manage invitations."""


class InvitationNotFoundError(LookupError):
    pass


class InvitationNotRevocableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InvitationActor:
    """Explicitly distinguish a browser administrator from Docker operator authority."""

    channel: InvitationIssuerChannel
    principal: RequestPrincipal | None = None

    def __post_init__(self) -> None:
        if self.channel is InvitationIssuerChannel.WEB_ADMIN:
            if self.principal is None:
                raise ValueError("Web invitation actors require a request principal.")
        elif self.principal is not None:
            raise ValueError("Operator invitation actors must not carry a browser principal.")

    @classmethod
    def web_admin(cls, principal: RequestPrincipal) -> InvitationActor:
        return cls(channel=InvitationIssuerChannel.WEB_ADMIN, principal=principal)

    @classmethod
    def operator_cli(cls) -> InvitationActor:
        return cls(channel=InvitationIssuerChannel.OPERATOR_CLI)


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    id: str
    workspace_id: str
    masked_hint: str
    created_by_user_id: str | None
    issuer_channel: InvitationIssuerChannel
    installation_state_version: int
    label: str | None
    expires_at: datetime
    redeemed_at: datetime | None
    redeemed_by_user_id: str | None
    redeemed_by_username: str | None
    redeemed_by_display_name: str | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    current_installation_state_version: int


@dataclass(frozen=True, slots=True)
class InvitationSummary:
    id: str
    workspace_id: str
    masked_hint: str
    issuer_channel: InvitationIssuerChannel
    label: str | None
    status: InvitationStatus
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    redeemed_by_user_id: str | None
    redeemed_by_username: str | None
    redeemed_by_display_name: str | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class CreatedInvitation:
    invitation: InvitationSummary
    registration_url: str = field(repr=False)


class InvitationRepository(Protocol):
    def create(
        self,
        *,
        actor: InvitationActor,
        invitation_id: str,
        token_digest: str,
        masked_hint: str,
        label: str | None,
        expires_at: datetime,
        now: datetime,
    ) -> InvitationRecord: ...

    def list(
        self,
        *,
        actor: InvitationActor,
    ) -> tuple[InvitationRecord, ...]: ...

    def get(
        self,
        *,
        actor: InvitationActor,
        invitation_id: str,
    ) -> InvitationRecord: ...

    def revoke(
        self,
        *,
        actor: InvitationActor,
        invitation_id: str,
        now: datetime,
    ) -> InvitationRecord: ...


class IdentifierFactory(Protocol):
    def __call__(self) -> str: ...


def _new_identifier() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class InvitationService:
    repository: InvitationRepository
    token_codec: InvitationTokenCodec
    web_public_origin: str
    identifier_factory: IdentifierFactory = _new_identifier

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "web_public_origin",
            self._canonical_origin(self.web_public_origin),
        )

    def create(
        self,
        *,
        actor: InvitationActor,
        expires_in: timedelta,
        label: str | None = None,
        now: datetime | None = None,
    ) -> CreatedInvitation:
        self._require_actor(actor)
        created_at = self._aware_time(now)
        if not timedelta(minutes=1) <= expires_in <= timedelta(days=90):
            raise ValueError("Invitation expiry must be between one minute and 90 days.")
        normalized_label = self._normalized_label(label)
        issued = self.token_codec.issue()
        record = self.repository.create(
            actor=actor,
            invitation_id=self.identifier_factory(),
            token_digest=issued.token_digest,
            masked_hint=issued.masked_hint,
            label=normalized_label,
            expires_at=created_at + expires_in,
            now=created_at,
        )
        return CreatedInvitation(
            invitation=self._summary(record, now=created_at),
            registration_url=(
                f"{self.web_public_origin}/register#invite="
                f"{quote(issued.raw_token, safe='')}"
            ),
        )

    def list(
        self,
        *,
        actor: InvitationActor,
        include_inactive: bool = True,
        now: datetime | None = None,
    ) -> tuple[InvitationSummary, ...]:
        self._require_actor(actor)
        listed_at = self._aware_time(now)
        invitations = tuple(
            self._summary(record, now=listed_at)
            for record in self.repository.list(actor=actor)
        )
        if include_inactive:
            return invitations
        return tuple(
            invitation
            for invitation in invitations
            if invitation.status is InvitationStatus.ACTIVE
        )

    def status(
        self,
        *,
        actor: InvitationActor,
        invitation_id: str,
        now: datetime | None = None,
    ) -> InvitationSummary:
        self._require_actor(actor)
        return self._summary(
            self.repository.get(
                actor=actor,
                invitation_id=self._normalized_identifier(invitation_id),
            ),
            now=self._aware_time(now),
        )

    def revoke(
        self,
        *,
        actor: InvitationActor,
        invitation_id: str,
        now: datetime | None = None,
    ) -> InvitationSummary:
        self._require_actor(actor)
        revoked_at = self._aware_time(now)
        return self._summary(
            self.repository.revoke(
                actor=actor,
                invitation_id=self._normalized_identifier(invitation_id),
                now=revoked_at,
            ),
            now=revoked_at,
        )

    @staticmethod
    def _require_actor(actor: InvitationActor) -> None:
        if actor.channel is InvitationIssuerChannel.OPERATOR_CLI:
            return
        principal = actor.principal
        if (
            principal is None
            or principal.identity_mode is not IdentityMode.AUTHENTICATED
            or "administrators" not in principal.group_keys
            or not principal.has(Permission.USER_MANAGE)
        ):
            raise InvitationDeniedError(
                "An authenticated administrators-group principal is required."
            )

    @staticmethod
    def _normalized_identifier(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvitationNotFoundError("Invitation was not found.") from error

    @staticmethod
    def _normalized_label(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if (
            len(normalized) > 128
            or len(normalized.encode("utf-8")) > 512
            or any(character in "\x00\r\n" for character in normalized)
        ):
            raise ValueError("Invitation label must be a single line of at most 128 characters.")
        return normalized

    @staticmethod
    def _aware_time(value: datetime | None) -> datetime:
        resolved = value or datetime.now(UTC)
        if resolved.tzinfo is None:
            raise ValueError("Invitation lifecycle timestamps must be timezone-aware.")
        return resolved

    @staticmethod
    def _canonical_origin(value: str) -> str:
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except (AttributeError, ValueError) as error:
            raise ValueError("WEB_PUBLIC_ORIGIN must be a canonical HTTP(S) origin.") from error
        if not (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        ):
            raise ValueError("WEB_PUBLIC_ORIGIN must be a canonical HTTP(S) origin.")
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _summary(record: InvitationRecord, *, now: datetime) -> InvitationSummary:
        expires_at = InvitationService._as_utc(record.expires_at)
        redeemed_at = (
            InvitationService._as_utc(record.redeemed_at)
            if record.redeemed_at is not None
            else None
        )
        revoked_at = (
            InvitationService._as_utc(record.revoked_at)
            if record.revoked_at is not None
            else None
        )
        return InvitationSummary(
            id=record.id,
            workspace_id=record.workspace_id,
            masked_hint=record.masked_hint,
            issuer_channel=record.issuer_channel,
            label=record.label,
            status=derive_invitation_status(
                invitation_generation=record.installation_state_version,
                current_generation=record.current_installation_state_version,
                expires_at=expires_at,
                redeemed_at=redeemed_at,
                revoked_at=revoked_at,
                now=now,
            ),
            created_at=InvitationService._as_utc(record.created_at),
            expires_at=expires_at,
            redeemed_at=redeemed_at,
            redeemed_by_user_id=record.redeemed_by_user_id,
            redeemed_by_username=record.redeemed_by_username,
            redeemed_by_display_name=record.redeemed_by_display_name,
            revoked_at=revoked_at,
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
