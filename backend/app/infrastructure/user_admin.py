from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.user_admin import (
    InstallationOwnerMutationError,
    ManagedUser,
    ManagedUserConflictError,
    ManagedUserNotFoundError,
    ManagedUserProvisioning,
    UserAdministrationDeniedError,
    UserAdministrationModeError,
    UserAdministrationRepository,
    UserAdministrationStateError,
)
from app.domain.auth import Permission, RequestPrincipal
from app.infrastructure.db.models import (
    GroupMembershipModel,
    GroupModel,
    GroupPermissionModel,
    InstallationStateModel,
    UserModel,
    UserOnboardingStateModel,
    WebSessionModel,
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
class DatabaseUserAdministrationRepository(UserAdministrationRepository):
    session_factory: sessionmaker[Session]

    def list_users(self, *, principal: RequestPrincipal) -> tuple[ManagedUser, ...]:
        with self.session_factory() as session:
            installation = self._require_administrator(session, principal)
            rows = session.execute(
                select(UserModel, WorkspaceMembershipModel.role)
                .join(
                    WorkspaceMembershipModel,
                    WorkspaceMembershipModel.user_id == UserModel.id,
                )
                .where(WorkspaceMembershipModel.workspace_id == principal.active_workspace_id)
                .order_by(UserModel.created_at, UserModel.id)
            ).all()
            return tuple(
                self._record(
                    user,
                    workspace_id=principal.active_workspace_id,
                    workspace_role=role,
                    owner_user_id=installation.owner_user_id,
                )
                for user, role in rows
            )

    def create_user(
        self,
        *,
        principal: RequestPrincipal,
        provisioning: ManagedUserProvisioning,
    ) -> ManagedUser:
        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = self._require_administrator(session, principal)
            username_exists = session.scalar(
                select(func.count())
                .select_from(UserModel)
                .where(func.lower(UserModel.username) == provisioning.username)
            )
            if username_exists:
                raise ManagedUserConflictError("The requested username is unavailable.")
            user = UserModel(
                id=provisioning.id,
                username=provisioning.username,
                display_name=provisioning.display_name,
                password_hash=provisioning.password_hash,
                status="ACTIVE",
                created_at=provisioning.created_at,
                updated_at=provisioning.created_at,
            )
            membership = WorkspaceMembershipModel(
                workspace_id=principal.active_workspace_id,
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
            session.add_all([user, membership, onboarding])
            session.flush()
            from app.application.libraries import grant_new_workspace_member_defaults

            grant_new_workspace_member_defaults(
                session, user_id=user.id, workspace_id=membership.workspace_id,
                granted_by_user_id=principal.user_id, source="ADMIN_MEMBERSHIP",
            )
            result = self._record(
                user,
                workspace_id=membership.workspace_id,
                workspace_role=membership.role,
                owner_user_id=installation.owner_user_id,
            )
            session.commit()
            return result
        except IntegrityError as error:
            session.rollback()
            raise ManagedUserConflictError("The requested username is unavailable.") from error
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def disable_user(
        self,
        *,
        principal: RequestPrincipal,
        target_user_id: str,
        now: datetime,
    ) -> ManagedUser:
        if now.tzinfo is None:
            raise ValueError("User disable time must be timezone-aware.")
        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = self._require_administrator(session, principal)
            target = session.get(UserModel, target_user_id)
            target_membership = session.get(
                WorkspaceMembershipModel,
                (principal.active_workspace_id, target_user_id),
            )
            if target is None or target_membership is None:
                raise ManagedUserNotFoundError("Managed user was not found.")
            if target.id == installation.owner_user_id:
                raise InstallationOwnerMutationError("The installation owner cannot be disabled.")
            if target.id == principal.user_id:
                raise InstallationOwnerMutationError(
                    "An administrator cannot disable the active account."
                )

            if target.status != "DISABLED":
                target.auth_generation += 1
                target.status = "DISABLED"
                target.updated_at = now
            session.execute(
                update(WebSessionModel)
                .where(
                    WebSessionModel.user_id == target.id,
                    WebSessionModel.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            from app.infrastructure.db.client_models import ClientSessionModel

            session.execute(
                update(ClientSessionModel)
                .where(ClientSessionModel.user_id == target.id)
                .values(
                    revoked_at=now,
                    revocation_reason="USER_DISABLED",
                )
            )
            result = self._record(
                target,
                workspace_id=target_membership.workspace_id,
                workspace_role=target_membership.role,
                owner_user_id=installation.owner_user_id,
            )
            session.commit()
            return result
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _require_administrator(
        session: Session,
        principal: RequestPrincipal,
    ) -> InstallationStateModel:
        installation = session.get(InstallationStateModel, "installation")
        if (
            installation is None
            or installation.state != "READY"
            or installation.edition != "SERVER"
            or installation.owner_user_id is None
        ):
            raise UserAdministrationStateError(
                "User administration requires a READY Server installation."
            )
        if installation.installation_mode != "MULTI_USER":
            raise UserAdministrationModeError(
                "User administration is unavailable outside multi-user mode."
            )
        actor = session.get(UserModel, principal.user_id)
        actor_membership = session.get(
            WorkspaceMembershipModel,
            (principal.active_workspace_id, principal.user_id),
        )
        administrator_group = session.scalar(
            select(GroupModel)
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
            actor is None
            or actor.status != "ACTIVE"
            or actor_membership is None
            or administrator_group is None
        ):
            raise UserAdministrationDeniedError(
                "The persisted administrator membership is not active."
            )
        return installation

    @staticmethod
    def _record(
        user: UserModel,
        *,
        workspace_id: str,
        workspace_role: str,
        owner_user_id: str | None,
    ) -> ManagedUser:
        return ManagedUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            status=user.status,
            workspace_id=workspace_id,
            workspace_role=workspace_role,
            is_installation_owner=user.id == owner_user_id,
            created_at=_aware_utc(user.created_at),
            updated_at=_aware_utc(user.updated_at),
        )
