from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.application.installation_identity import (
    InstallationIdentityInvariantError,
    InstallationIdentitySnapshot,
)
from app.domain.auth import WORKSPACE_PERMISSIONS, Permission
from app.domain.runtime import RuntimeEdition
from app.domain.setup import InstallationMode
from app.infrastructure.db.models import (
    GroupMembershipModel,
    GroupModel,
    GroupPermissionModel,
    InstallationStateModel,
    PermissionModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
    WorkspaceRolePermissionModel,
)


@dataclass(frozen=True, slots=True)
class DatabaseInstallationOwnerIdentityInvariant:
    """Validate the durable installation-owner graph for one runtime policy.

    Server installation mode is persisted state, not a browser or environment hint.  A
    ``SINGLE_USER`` installation therefore proves that the owner is the only active user, while
    ``MULTI_USER`` proves the same owner/admin/private-workspace graph without treating additional
    active users or shared workspace ownership as corruption.

    ``LOCAL`` support is retained only for the historical native adapter.  Its pre-ADR-033 rows
    may not have an installation mode and are conservatively interpreted as single-user.
    """

    session_factory: Any
    expected_edition: RuntimeEdition = RuntimeEdition.SERVER
    expected_installation_mode: InstallationMode | None = None

    def verify(self) -> InstallationIdentitySnapshot:
        with self.session_factory() as session:
            return self.verify_in_session(session)

    def verify_in_session(self, session: Session) -> InstallationIdentitySnapshot:
        """Verify against an existing transaction without opening a second database view."""

        expected_permissions = {permission.value for permission in Permission}
        expected_workspace_permissions = {
            permission.value for permission in WORKSPACE_PERMISSIONS
        }
        installation = session.get(InstallationStateModel, "installation")
        if installation is None or installation.owner_user_id is None:
            raise InstallationIdentityInvariantError(
                "The installation owner metadata is incomplete."
            )
        try:
            installed_edition = RuntimeEdition(installation.edition)
        except ValueError as error:
            raise InstallationIdentityInvariantError(
                "The installation contains an unsupported edition."
            ) from error
        if installed_edition is not self.expected_edition:
            raise InstallationIdentityInvariantError(
                "The installed edition does not match the runtime identity policy."
            )

        if installation.installation_mode is None:
            if installed_edition is RuntimeEdition.SERVER:
                raise InstallationIdentityInvariantError(
                    "The Server installation mode has not been selected."
                )
            installed_mode = InstallationMode.SINGLE_USER
        else:
            try:
                installed_mode = InstallationMode(installation.installation_mode)
            except ValueError as error:
                raise InstallationIdentityInvariantError(
                    "The installation contains an unsupported mode."
                ) from error
        if (
            self.expected_installation_mode is not None
            and installed_mode is not self.expected_installation_mode
        ):
            raise InstallationIdentityInvariantError(
                "The installed mode does not match the runtime identity policy."
            )
        if (
            installed_edition is RuntimeEdition.LOCAL
            and installed_mode is not InstallationMode.SINGLE_USER
        ):
            raise InstallationIdentityInvariantError(
                "The historical Local identity policy only supports single-user mode."
            )

        active_user_ids = list(
            session.scalars(
                select(UserModel.id)
                .where(UserModel.status == "ACTIVE")
                .order_by(UserModel.id)
            ).all()
        )
        if (
            installed_mode is InstallationMode.SINGLE_USER
            and active_user_ids != [installation.owner_user_id]
        ):
            raise InstallationIdentityInvariantError(
                "Single-user mode requires exactly one active installation owner."
            )
        if (
            installed_mode is InstallationMode.MULTI_USER
            and installation.owner_user_id not in active_user_ids
        ):
            raise InstallationIdentityInvariantError(
                "Multi-user mode requires an active installation owner."
            )

        workspaces = list(
            session.scalars(
                select(WorkspaceModel)
                .join(
                    WorkspaceMembershipModel,
                    WorkspaceMembershipModel.workspace_id == WorkspaceModel.id,
                )
                .where(
                    WorkspaceMembershipModel.user_id == installation.owner_user_id,
                    WorkspaceMembershipModel.role == "OWNER",
                    WorkspaceModel.kind == "PRIVATE",
                )
                .order_by(WorkspaceModel.id)
            ).all()
        )
        if len(workspaces) != 1:
            raise InstallationIdentityInvariantError(
                "The installation owner must have exactly one private OWNER workspace."
            )

        administrators = list(
            session.scalars(
                select(GroupModel)
                .join(
                    GroupMembershipModel,
                    GroupMembershipModel.group_id == GroupModel.id,
                )
                .where(
                    GroupMembershipModel.user_id == installation.owner_user_id,
                    GroupModel.key == "administrators",
                )
            ).all()
        )
        if len(administrators) != 1:
            raise InstallationIdentityInvariantError(
                "The installation owner administrators membership is incomplete."
            )

        catalog = set(session.scalars(select(PermissionModel.key)).all())
        role_permissions = set(
            session.scalars(
                select(WorkspaceRolePermissionModel.permission_key).where(
                    WorkspaceRolePermissionModel.role == "OWNER"
                )
            ).all()
        )
        administrator_permissions = set(
            session.scalars(
                select(GroupPermissionModel.permission_key).where(
                    GroupPermissionModel.group_id == administrators[0].id
                )
            ).all()
        )
        if (
            catalog != expected_permissions
            or role_permissions != expected_workspace_permissions
            or administrator_permissions != expected_permissions
        ):
            raise InstallationIdentityInvariantError(
                "The installation owner permission mappings are incomplete."
            )

        if installed_mode is InstallationMode.SINGLE_USER:
            # Catch ambiguous ownership even if a future schema relaxation permits it.  A
            # multi-user owner may legitimately own additional shared workspaces.
            owner_membership_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(WorkspaceMembershipModel)
                    .where(
                        WorkspaceMembershipModel.user_id == installation.owner_user_id,
                        WorkspaceMembershipModel.role == "OWNER",
                    )
                )
                or 0
            )
            if owner_membership_count != 1:
                raise InstallationIdentityInvariantError(
                    "The single-user owner workspace membership is ambiguous."
                )

        return InstallationIdentitySnapshot(
            owner_user_id=installation.owner_user_id,
            workspace_id=workspaces[0].id,
        )


@dataclass(frozen=True, slots=True)
class DatabaseLocalInstallationIdentityInvariant:
    """Compatibility adapter for the superseded native Local runtime."""

    session_factory: Any

    def verify(self) -> InstallationIdentitySnapshot:
        return DatabaseInstallationOwnerIdentityInvariant(
            self.session_factory,
            expected_edition=RuntimeEdition.LOCAL,
            expected_installation_mode=InstallationMode.SINGLE_USER,
        ).verify()
