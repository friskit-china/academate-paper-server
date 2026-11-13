from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.application.credentials import CredentialMetadata, CredentialService, CredentialWrite
from app.domain.auth import RequestPrincipal
from app.domain.credentials import DefaultCredentialFactory
from app.domain.users import normalized_display_name


class OnboardingState(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"


class OnboardingError(RuntimeError):
    """Base class for self-scoped onboarding failures."""


class OnboardingStateMissingError(OnboardingError):
    """Fail closed when an authenticated account lacks durable onboarding state."""

    def __init__(self) -> None:
        super().__init__("Onboarding state is unavailable for the active account.")


class OnboardingPrincipalDeniedError(OnboardingError):
    def __init__(self) -> None:
        super().__init__("The active account cannot access this onboarding profile.")


class OnboardingCredentialUnavailableError(OnboardingError):
    def __init__(self) -> None:
        super().__init__("Personal credential storage is unavailable for this installation.")


@dataclass(frozen=True, slots=True)
class OnboardingProfileRecord:
    """Non-secret, database-owned portion of the current user's onboarding profile."""

    username: str
    display_name: str
    state: OnboardingState
    automatic_translation_enabled: bool
    automatic_ai_repair_enabled: bool
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class OnboardingProfile:
    """Self-scoped onboarding view; credential data is masked metadata only."""

    username: str
    display_name: str
    state: OnboardingState
    automatic_translation_enabled: bool
    automatic_ai_repair_enabled: bool
    default_credential: CredentialMetadata | None
    completed_at: datetime | None

    @property
    def onboarding_required(self) -> bool:
        return self.state is OnboardingState.PENDING


@dataclass(frozen=True, slots=True)
class OnboardingCompletion:
    """The only user-editable values accepted by the first personal onboarding flow."""

    display_name: str
    automatic_translation_enabled: bool
    automatic_ai_repair_enabled: bool
    credential: CredentialWrite | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_name", normalized_display_name(self.display_name))
        if type(self.automatic_translation_enabled) is not bool:
            raise TypeError("Automatic translation preference must be a boolean.")
        if type(self.automatic_ai_repair_enabled) is not bool:
            raise TypeError("Automatic AI repair preference must be a boolean.")


@dataclass(frozen=True, slots=True)
class SelfProfileUpdate:
    """A deliberately narrow self-service profile mutation."""

    display_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_name", normalized_display_name(self.display_name))


class OnboardingRepository(Protocol):
    def load_self(self, *, principal: RequestPrincipal) -> OnboardingProfileRecord: ...

    def update_self(
        self,
        *,
        principal: RequestPrincipal,
        update: SelfProfileUpdate,
        updated_at: datetime,
    ) -> OnboardingProfileRecord: ...

    def complete_self(
        self,
        *,
        principal: RequestPrincipal,
        completion: OnboardingCompletion,
        completed_at: datetime,
        credential_factory: DefaultCredentialFactory | None,
    ) -> OnboardingProfileRecord: ...


@dataclass(frozen=True, slots=True)
class OnboardingService:
    """Complete profile, preferences, credential, and marker as one adapter transaction.

    The credential service supplies only a secret-safe encryption callback.  The onboarding
    repository invokes it after acquiring its serialized database transaction, so concurrent
    completion attempts cannot leave a credential that belongs to a losing profile request.
    Once the durable marker is COMPLETED, retries are read-only.
    """

    repository: OnboardingRepository
    credential_service: CredentialService | None

    def get_status(self, *, principal: RequestPrincipal) -> OnboardingProfile:
        return self._view(principal, self.repository.load_self(principal=principal))

    def update_profile(
        self,
        *,
        principal: RequestPrincipal,
        update: SelfProfileUpdate,
        now: datetime | None = None,
    ) -> OnboardingProfile:
        updated_at = self._aware_time(now, operation="profile update")
        record = self.repository.update_self(
            principal=principal,
            update=update,
            updated_at=updated_at,
        )
        return self._view(principal, record)

    def complete(
        self,
        *,
        principal: RequestPrincipal,
        completion: OnboardingCompletion,
        now: datetime | None = None,
    ) -> OnboardingProfile:
        before = self.repository.load_self(principal=principal)
        if before.state is OnboardingState.COMPLETED:
            return self._view(principal, before)

        completed_at = self._aware_time(now, operation="completion")

        credential_factory: DefaultCredentialFactory | None = None
        if completion.credential is not None:
            if self.credential_service is None:
                raise OnboardingCredentialUnavailableError
            credential_factory = self.credential_service.default_write_factory(
                principal.user_id,
                completion.credential,
                now=completed_at,
            )

        after = self.repository.complete_self(
            principal=principal,
            completion=completion,
            completed_at=completed_at,
            credential_factory=credential_factory,
        )
        return self._view(principal, after)

    def _view(
        self,
        principal: RequestPrincipal,
        record: OnboardingProfileRecord,
    ) -> OnboardingProfile:
        credential = (
            self.credential_service.default_for_user(principal.user_id)
            if self.credential_service is not None
            else None
        )
        return OnboardingProfile(
            username=record.username,
            display_name=record.display_name,
            state=record.state,
            automatic_translation_enabled=record.automatic_translation_enabled,
            automatic_ai_repair_enabled=record.automatic_ai_repair_enabled,
            default_credential=credential,
            completed_at=record.completed_at,
        )

    @staticmethod
    def _aware_time(value: datetime | None, *, operation: str) -> datetime:
        resolved = value or datetime.now(UTC)
        if resolved.tzinfo is None:
            raise ValueError(f"Onboarding {operation} time must be timezone-aware.")
        return resolved.astimezone(UTC)
