from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.application.onboarding import (
    OnboardingCompletion,
    OnboardingPrincipalDeniedError,
    OnboardingProfileRecord,
    OnboardingRepository,
    OnboardingState,
    OnboardingStateMissingError,
    SelfProfileUpdate,
)
from app.application.settings import (
    get_automatic_ai_repair_enabled,
    get_automatic_translation_enabled,
    save_automatic_ai_repair_enabled,
    save_automatic_translation_enabled,
)
from app.domain.auth import RequestPrincipal
from app.domain.credentials import DefaultCredentialFactory
from app.infrastructure.credentials.database import (
    upsert_default_credential_in_session,
)
from app.infrastructure.db.models import (
    UserModel,
    UserOnboardingStateModel,
    WorkspaceMembershipModel,
)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _begin_serialized(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


@dataclass(slots=True)
class DatabaseOnboardingRepository(OnboardingRepository):
    """Persist only the authenticated principal's own onboarding profile."""

    session_factory: sessionmaker[Session]

    def load_self(self, *, principal: RequestPrincipal) -> OnboardingProfileRecord:
        with self.session_factory() as session:
            user, onboarding = self._load_active_self(session, principal)
            return self._record(session, user=user, onboarding=onboarding)

    def update_self(
        self,
        *,
        principal: RequestPrincipal,
        update: SelfProfileUpdate,
        updated_at: datetime,
    ) -> OnboardingProfileRecord:
        if updated_at.tzinfo is None:
            raise ValueError("Onboarding profile update time must be timezone-aware.")
        session = self.session_factory()
        try:
            _begin_serialized(session)
            user, onboarding = self._load_active_self(session, principal)
            user.display_name = update.display_name
            user.updated_at = updated_at.astimezone(UTC)
            session.flush()
            result = self._record(session, user=user, onboarding=onboarding)
            session.commit()
            return result
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def complete_self(
        self,
        *,
        principal: RequestPrincipal,
        completion: OnboardingCompletion,
        completed_at: datetime,
        credential_factory: DefaultCredentialFactory | None,
    ) -> OnboardingProfileRecord:
        if completed_at.tzinfo is None:
            raise ValueError("Onboarding completion time must be timezone-aware.")
        session = self.session_factory()
        try:
            _begin_serialized(session)
            user, onboarding = self._load_active_self(session, principal)
            # A completed marker is immutable through the onboarding boundary.  This makes client
            # retries idempotent even if they carry stale or different form values.
            if onboarding.status == OnboardingState.COMPLETED.value:
                result = self._record(session, user=user, onboarding=onboarding)
                session.commit()
                return result

            timestamp = completed_at.astimezone(UTC)
            if credential_factory is not None:
                upsert_default_credential_in_session(
                    session,
                    user_id=principal.user_id,
                    factory=credential_factory,
                )
            user.display_name = completion.display_name
            user.updated_at = timestamp
            save_automatic_translation_enabled(
                session,
                completion.automatic_translation_enabled,
                user_id=principal.user_id,
            )
            save_automatic_ai_repair_enabled(
                session,
                completion.automatic_ai_repair_enabled,
                user_id=principal.user_id,
            )
            onboarding.status = OnboardingState.COMPLETED.value
            onboarding.completed_at = timestamp
            onboarding.updated_at = timestamp
            session.flush()
            result = self._record(session, user=user, onboarding=onboarding)
            session.commit()
            return result
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _load_active_self(
        session: Session,
        principal: RequestPrincipal,
    ) -> tuple[UserModel, UserOnboardingStateModel]:
        user = session.get(UserModel, principal.user_id)
        membership = session.get(
            WorkspaceMembershipModel,
            (principal.active_workspace_id, principal.user_id),
        )
        if user is None or user.status != "ACTIVE" or membership is None:
            raise OnboardingPrincipalDeniedError
        onboarding = session.get(UserOnboardingStateModel, principal.user_id)
        if onboarding is None:
            raise OnboardingStateMissingError
        try:
            OnboardingState(onboarding.status)
        except ValueError as error:
            raise OnboardingStateMissingError from error
        return user, onboarding

    @staticmethod
    def _record(
        session: Session,
        *,
        user: UserModel,
        onboarding: UserOnboardingStateModel,
    ) -> OnboardingProfileRecord:
        state = OnboardingState(onboarding.status)
        return OnboardingProfileRecord(
            username=user.username,
            display_name=user.display_name,
            state=state,
            automatic_translation_enabled=get_automatic_translation_enabled(
                session,
                user_id=user.id,
            ),
            automatic_ai_repair_enabled=get_automatic_ai_repair_enabled(
                session,
                user_id=user.id,
            ),
            completed_at=(
                _aware_utc(onboarding.completed_at)
                if onboarding.completed_at is not None
                else None
            ),
        )
