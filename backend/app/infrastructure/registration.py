from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.registration import (
    RegistrationInviteInvalidError,
    RegistrationProvisioning,
    RegistrationRepository,
    RegistrationStateError,
    RegistrationUnavailableError,
    RegistrationUsernameConflictError,
)
from app.application.web_sessions import WebSessionBinding
from app.domain.invitations import InvitationStatus, derive_invitation_status
from app.infrastructure.db.models import (
    InstallationStateModel,
    UserModel,
    UserOnboardingStateModel,
    WebSessionModel,
    WorkspaceInvitationModel,
    WorkspaceMembershipModel,
)

_INSTALLATION_KEY = "installation"
_INVALID_INVITATION_MESSAGE = (
    "This invitation cannot be used. Ask an administrator for a new invitation."
)


def _begin_serialized(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(slots=True)
class DatabaseRegistrationRepository(RegistrationRepository):
    session_factory: sessionmaker[Session]

    def register(self, provisioning: RegistrationProvisioning) -> WebSessionBinding:
        issued_session = provisioning.issued_session
        if (
            issued_session.expires_at.tzinfo is None
            or issued_session.expires_at <= provisioning.created_at
        ):
            raise ValueError("Web session expiry must be timezone-aware and in the future.")

        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = self._ready_multi_user_installation(session)
            invitation = session.scalar(
                select(WorkspaceInvitationModel).where(
                    WorkspaceInvitationModel.token_digest
                    == provisioning.invitation_digest
                )
            )
            if invitation is None or not secrets.compare_digest(
                invitation.token_digest,
                provisioning.invitation_digest,
            ):
                raise RegistrationInviteInvalidError(_INVALID_INVITATION_MESSAGE)
            lifecycle = derive_invitation_status(
                invitation_generation=invitation.installation_state_version,
                current_generation=installation.state_version,
                expires_at=_aware_utc(invitation.expires_at),
                redeemed_at=(
                    _aware_utc(invitation.redeemed_at)
                    if invitation.redeemed_at is not None
                    else None
                ),
                revoked_at=(
                    _aware_utc(invitation.revoked_at)
                    if invitation.revoked_at is not None
                    else None
                ),
                now=provisioning.created_at,
            )
            if lifecycle is not InvitationStatus.ACTIVE:
                raise RegistrationInviteInvalidError(_INVALID_INVITATION_MESSAGE)

            username_exists = session.scalar(
                select(func.count())
                .select_from(UserModel)
                .where(func.lower(UserModel.username) == provisioning.username)
            )
            if username_exists:
                raise RegistrationUsernameConflictError(
                    "The requested username is unavailable."
                )

            user = UserModel(
                id=provisioning.user_id,
                username=provisioning.username,
                display_name=provisioning.display_name,
                password_hash=provisioning.password_hash,
                status="ACTIVE",
                created_at=provisioning.created_at,
                updated_at=provisioning.created_at,
            )
            membership = WorkspaceMembershipModel(
                workspace_id=invitation.workspace_id,
                user_id=user.id,
                role="MEMBER",
                created_at=provisioning.created_at,
            )
            onboarding = UserOnboardingStateModel(
                user_id=user.id,
                status="PENDING",
                completed_at=None,
                created_at=provisioning.created_at,
                updated_at=provisioning.created_at,
            )
            binding = WebSessionBinding(
                session_id=provisioning.session_id,
                user_id=user.id,
                active_workspace_id=invitation.workspace_id,
                installation_state_version=installation.state_version,
                expires_at=issued_session.expires_at,
            )
            web_session = WebSessionModel(
                id=binding.session_id,
                token_digest=issued_session.token_digest,
                user_id=binding.user_id,
                active_workspace_id=binding.active_workspace_id,
                installation_state_version=binding.installation_state_version,
                expires_at=binding.expires_at,
                created_at=provisioning.created_at,
            )
            # Materialize the user/membership boundary before inserting the session row. The
            # session table has a composite FK to that membership, and SQLite checks it at each
            # statement even though every statement remains inside this one transaction.
            session.add_all([user, membership, onboarding])
            session.flush()
            from app.application.libraries import grant_new_workspace_member_defaults

            grant_new_workspace_member_defaults(
                session, user_id=user.id, workspace_id=membership.workspace_id,
                granted_by_user_id=invitation.created_by_user_id, source="INVITATION_DEFAULT",
            )
            invitation.redeemed_at = provisioning.created_at
            invitation.redeemed_by_user_id = user.id
            invitation.updated_at = provisioning.created_at
            session.add(web_session)
            session.flush()
            session.commit()
            return binding
        except IntegrityError as error:
            session.rollback()
            if self._username_exists(provisioning.username):
                raise RegistrationUsernameConflictError(
                    "The requested username is unavailable."
                ) from error
            raise RegistrationStateError(
                "Registration persistence rejected the transaction."
            ) from error
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _ready_multi_user_installation(session: Session) -> InstallationStateModel:
        installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
        if (
            installation is None
            or installation.state != "READY"
            or installation.edition != "SERVER"
            or installation.owner_user_id is None
            or installation.installation_mode != "MULTI_USER"
        ):
            raise RegistrationUnavailableError(
                "Invitation registration is unavailable for this installation."
            )
        return installation

    def _username_exists(self, username: str) -> bool:
        with self.session_factory() as session:
            return bool(
                session.scalar(
                    select(func.count())
                    .select_from(UserModel)
                    .where(func.lower(UserModel.username) == username)
                )
            )
