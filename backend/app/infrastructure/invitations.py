from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.invitations import (
    InvitationActor,
    InvitationDeniedError,
    InvitationModeError,
    InvitationNotFoundError,
    InvitationNotRevocableError,
    InvitationRecord,
    InvitationRepository,
    InvitationStateError,
)
from app.domain.auth import Permission
from app.domain.invitations import (
    InvitationIssuerChannel,
    InvitationStatus,
    IssuedInvitationToken,
    derive_invitation_status,
)
from app.infrastructure.db.models import (
    GroupMembershipModel,
    GroupModel,
    GroupPermissionModel,
    InstallationStateModel,
    UserModel,
    WorkspaceInvitationModel,
    WorkspaceMembershipModel,
)

_INSTALLATION_KEY = "installation"
_INVITATION_DIGEST_DOMAIN = b"academatepaper.workspace-invitation-token.v1\0"


def _begin_serialized(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SecureInvitationTokenCodec:
    entropy_bytes: int = 32
    hint_characters: int = 6

    def __post_init__(self) -> None:
        if self.entropy_bytes < 32:
            raise ValueError("Invitation tokens require at least 256 bits of entropy.")
        if not 4 <= self.hint_characters <= 12:
            raise ValueError("Invitation hints must reveal between four and 12 characters.")

    def issue(self) -> IssuedInvitationToken:
        raw_token = secrets.token_urlsafe(self.entropy_bytes)
        return IssuedInvitationToken(
            raw_token=raw_token,
            token_digest=self.digest(raw_token),
            masked_hint=f"••••{raw_token[-self.hint_characters:]}",
        )

    @staticmethod
    def digest(raw_token: str) -> str:
        digest = hashlib.sha256(_INVITATION_DIGEST_DOMAIN + raw_token.encode("utf-8"))
        return f"sha256-v1:{digest.hexdigest()}"

    def matches(self, raw_token: str, token_digest: str) -> bool:
        if not raw_token or not token_digest:
            return False
        return hmac.compare_digest(self.digest(raw_token), token_digest)


@dataclass(slots=True)
class DatabaseInvitationRepository(InvitationRepository):
    session_factory: sessionmaker[Session]

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
    ) -> InvitationRecord:
        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = self._ready_multi_user_installation(session)
            workspace_id, created_by_user_id = self._authorized_workspace(
                session,
                actor=actor,
                installation=installation,
            )
            invitation = WorkspaceInvitationModel(
                id=invitation_id,
                workspace_id=workspace_id,
                token_digest=token_digest,
                masked_hint=masked_hint,
                created_by_user_id=created_by_user_id,
                issuer_channel=actor.channel.value,
                installation_state_version=installation.state_version,
                label=label,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(invitation)
            session.flush()
            record = self._record(session, invitation, installation.state_version)
            session.commit()
            return record
        except IntegrityError as error:
            session.rollback()
            raise InvitationStateError("Invitation persistence rejected the request.") from error
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def list(
        self,
        *,
        actor: InvitationActor,
    ) -> tuple[InvitationRecord, ...]:
        with self.session_factory() as session:
            installation = self._ready_multi_user_installation(session)
            workspace_id, _ = self._authorized_workspace(
                session,
                actor=actor,
                installation=installation,
            )
            invitations = tuple(
                session.scalars(
                    select(WorkspaceInvitationModel)
                    .where(WorkspaceInvitationModel.workspace_id == workspace_id)
                    .order_by(
                        WorkspaceInvitationModel.created_at.desc(),
                        WorkspaceInvitationModel.id,
                    )
                ).all()
            )
            return tuple(
                self._record(session, invitation, installation.state_version)
                for invitation in invitations
            )

    def get(
        self,
        *,
        actor: InvitationActor,
        invitation_id: str,
    ) -> InvitationRecord:
        with self.session_factory() as session:
            installation = self._ready_multi_user_installation(session)
            workspace_id, _ = self._authorized_workspace(
                session,
                actor=actor,
                installation=installation,
            )
            invitation = session.scalar(
                select(WorkspaceInvitationModel).where(
                    WorkspaceInvitationModel.id == invitation_id,
                    WorkspaceInvitationModel.workspace_id == workspace_id,
                )
            )
            if invitation is None:
                raise InvitationNotFoundError("Invitation was not found.")
            return self._record(session, invitation, installation.state_version)

    def revoke(
        self,
        *,
        actor: InvitationActor,
        invitation_id: str,
        now: datetime,
    ) -> InvitationRecord:
        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = self._ready_multi_user_installation(session)
            workspace_id, _ = self._authorized_workspace(
                session,
                actor=actor,
                installation=installation,
            )
            invitation = session.scalar(
                select(WorkspaceInvitationModel).where(
                    WorkspaceInvitationModel.id == invitation_id,
                    WorkspaceInvitationModel.workspace_id == workspace_id,
                )
            )
            if invitation is None:
                raise InvitationNotFoundError("Invitation was not found.")
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
                now=now,
            )
            if lifecycle is InvitationStatus.REVOKED:
                record = self._record(session, invitation, installation.state_version)
                session.rollback()
                return record
            if lifecycle is not InvitationStatus.ACTIVE:
                raise InvitationNotRevocableError(
                    "Only an active, unredeemed invitation can be revoked."
                )
            invitation.revoked_at = now
            invitation.updated_at = now
            session.flush()
            record = self._record(session, invitation, installation.state_version)
            session.commit()
            return record
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
        ):
            raise InvitationStateError(
                "Invitations require a READY Server installation with an owner."
            )
        if installation.installation_mode != "MULTI_USER":
            raise InvitationModeError(
                "Invitations are unavailable outside multi-user mode."
            )
        return installation

    @staticmethod
    def _authorized_workspace(
        session: Session,
        *,
        actor: InvitationActor,
        installation: InstallationStateModel,
    ) -> tuple[str, str | None]:
        if actor.channel is InvitationIssuerChannel.OPERATOR_CLI:
            owner = session.get(UserModel, installation.owner_user_id)
            owner_workspace_ids = tuple(
                session.scalars(
                    select(WorkspaceMembershipModel.workspace_id)
                    .where(
                        WorkspaceMembershipModel.user_id == installation.owner_user_id,
                        WorkspaceMembershipModel.role == "OWNER",
                    )
                    .order_by(WorkspaceMembershipModel.workspace_id)
                ).all()
            )
            if owner is None or owner.status != "ACTIVE" or len(owner_workspace_ids) != 1:
                raise InvitationStateError(
                    "The operator could not resolve the installation owner's unique workspace."
                )
            return owner_workspace_ids[0], None

        principal = actor.principal
        if principal is None:
            raise InvitationDeniedError("Administrator access is required.")
        user = session.get(UserModel, principal.user_id)
        membership = session.get(
            WorkspaceMembershipModel,
            (principal.active_workspace_id, principal.user_id),
        )
        persisted_administrator = session.scalar(
            select(GroupModel.id)
            .join(
                GroupMembershipModel,
                GroupMembershipModel.group_id == GroupModel.id,
            )
            .join(
                GroupPermissionModel,
                GroupPermissionModel.group_id == GroupModel.id,
            )
            .where(
                GroupMembershipModel.user_id == principal.user_id,
                GroupModel.key == "administrators",
                GroupPermissionModel.permission_key == Permission.USER_MANAGE.value,
            )
        )
        if (
            user is None
            or user.status != "ACTIVE"
            or membership is None
            or persisted_administrator is None
        ):
            raise InvitationDeniedError("Persisted administrator access is required.")
        return principal.active_workspace_id, principal.user_id

    @staticmethod
    def _record(
        session: Session,
        invitation: WorkspaceInvitationModel,
        current_installation_state_version: int,
    ) -> InvitationRecord:
        try:
            issuer_channel = InvitationIssuerChannel(invitation.issuer_channel)
        except ValueError as error:
            raise InvitationStateError(
                "Invitation contains an unsupported issuer channel."
            ) from error
        redeemed_by = (
            session.get(UserModel, invitation.redeemed_by_user_id)
            if invitation.redeemed_by_user_id is not None
            else None
        )
        return InvitationRecord(
            id=invitation.id,
            workspace_id=invitation.workspace_id,
            masked_hint=invitation.masked_hint,
            created_by_user_id=invitation.created_by_user_id,
            issuer_channel=issuer_channel,
            installation_state_version=invitation.installation_state_version,
            label=invitation.label,
            expires_at=_aware_utc(invitation.expires_at),
            redeemed_at=(
                _aware_utc(invitation.redeemed_at)
                if invitation.redeemed_at is not None
                else None
            ),
            redeemed_by_user_id=invitation.redeemed_by_user_id,
            redeemed_by_username=(redeemed_by.username if redeemed_by is not None else None),
            redeemed_by_display_name=(
                redeemed_by.display_name if redeemed_by is not None else None
            ),
            revoked_at=(
                _aware_utc(invitation.revoked_at)
                if invitation.revoked_at is not None
                else None
            ),
            created_at=_aware_utc(invitation.created_at),
            updated_at=_aware_utc(invitation.updated_at),
            current_installation_state_version=current_installation_state_version,
        )
