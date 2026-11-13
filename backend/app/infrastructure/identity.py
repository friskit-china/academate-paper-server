from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.application.installation_identity import InstallationIdentityInvariantError
from app.domain.auth import IdentityRequestContext, Permission, RequestPrincipal
from app.domain.runtime import IdentityMode, RuntimeEdition
from app.domain.setup import InstallationMode
from app.infrastructure.db.models import (
    GroupMembershipModel,
    GroupModel,
    GroupPermissionModel,
    InstallationStateModel,
    LocalSessionModel,
    UserModel,
    UserOnboardingStateModel,
    WebSessionModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
    WorkspaceRolePermissionModel,
)
from app.infrastructure.installation_identity import (
    DatabaseInstallationOwnerIdentityInvariant,
    DatabaseLocalInstallationIdentityInvariant,
)


class UntrustedServiceIdentityError(PermissionError):
    """The request did not cross an authenticated internal service boundary."""


class IdentityStateError(RuntimeError):
    """The persistent identity state does not satisfy the Local Edition invariant."""


@dataclass(frozen=True, slots=True)
class LocalDatabaseIdentityProvider:
    """Resolve the one ready Local owner from persistent memberships and permission mappings.

    ``internal_service_identity`` is a verified ASGI-scope marker, not an HTTP identity claim.  A
    BFF/service-token adapter is responsible for authenticating its secret before setting it.  Raw
    browser headers never reach this resolver as user, workspace, group, or permission values.
    """

    session_factory: Any
    trusted_service_identities: frozenset[str] = field(
        default_factory=lambda: frozenset({"supervisor-bff"})
    )

    def resolve(self, context: IdentityRequestContext) -> RequestPrincipal:
        if context.internal_service_identity not in self.trusted_service_identities:
            raise UntrustedServiceIdentityError("unverified internal service identity")
        if context.session_id is None:
            raise UntrustedServiceIdentityError("unverified local browser session")

        try:
            identity_graph = DatabaseLocalInstallationIdentityInvariant(
                self.session_factory
            ).verify()
        except InstallationIdentityInvariantError as error:
            raise IdentityStateError(
                "the Local owner identity and permission graph is incomplete"
            ) from error

        with self.session_factory() as session:
            installation = session.get(InstallationStateModel, "installation")
            if (
                installation is None
                or installation.state != "READY"
                or installation.edition != "LOCAL"
                or installation.owner_user_id is None
            ):
                raise IdentityStateError("the Local installation is not ready")

            local_session = session.get(LocalSessionModel, context.session_id)
            now = datetime.now(UTC)
            if (
                local_session is None
                or local_session.revoked_at is not None
                or now >= self._aware_utc(local_session.expires_at)
                or local_session.installation_state_version != installation.state_version
            ):
                raise UntrustedServiceIdentityError("invalid local browser session evidence")

            active_users = list(
                session.scalars(
                    select(UserModel).where(UserModel.status == "ACTIVE").order_by(UserModel.id)
                ).all()
            )
            if len(active_users) != 1 or active_users[0].id != identity_graph.owner_user_id:
                raise IdentityStateError(
                    "the Local installation must have exactly one active owner"
                )
            owner = active_users[0]

            memberships = list(
                session.execute(
                    select(WorkspaceMembershipModel, WorkspaceModel)
                    .join(
                        WorkspaceModel,
                        WorkspaceModel.id == WorkspaceMembershipModel.workspace_id,
                    )
                    .where(
                        WorkspaceMembershipModel.user_id == owner.id,
                        WorkspaceMembershipModel.role == "OWNER",
                        WorkspaceModel.kind == "PRIVATE",
                    )
                    .order_by(WorkspaceModel.id)
                ).all()
            )
            if len(memberships) != 1:
                raise IdentityStateError(
                    "the Local owner must have exactly one private owner workspace"
                )
            membership, workspace = memberships[0]
            if workspace.id != identity_graph.workspace_id:
                raise IdentityStateError(
                    "the Local owner workspace does not match the installation identity"
                )

            role_permission_keys = set(
                session.scalars(
                    select(WorkspaceRolePermissionModel.permission_key).where(
                        WorkspaceRolePermissionModel.role == membership.role
                    )
                ).all()
            )
            group_rows = list(
                session.execute(
                    select(GroupModel.id, GroupModel.key)
                    .join(
                        GroupMembershipModel,
                        GroupMembershipModel.group_id == GroupModel.id,
                    )
                    .where(GroupMembershipModel.user_id == owner.id)
                    .order_by(GroupModel.key)
                ).all()
            )
            group_ids = [row.id for row in group_rows]
            group_permission_keys: set[str] = set()
            if group_ids:
                group_permission_keys.update(
                    session.scalars(
                        select(GroupPermissionModel.permission_key).where(
                            GroupPermissionModel.group_id.in_(group_ids)
                        )
                    ).all()
                )

        known_permissions = {permission.value: permission for permission in Permission}
        permission_set = frozenset(
            known_permissions[key]
            for key in role_permission_keys | group_permission_keys
            if key in known_permissions
        )
        group_keys = frozenset(row.key for row in group_rows)
        if permission_set != frozenset(Permission) or "administrators" not in group_keys:
            raise IdentityStateError("the Local owner permission mappings are incomplete")
        return RequestPrincipal(
            user_id=owner.id,
            active_workspace_id=workspace.id,
            group_keys=group_keys,
            permission_set=permission_set,
            identity_mode=IdentityMode.TRUSTED_LOCAL,
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AuthenticatedDatabaseIdentityProvider:
    """Resolve a Server principal only from a verified browser-session identifier.

    The internal service credential proves which BFF forwarded the request, but it never selects
    a user. User, active workspace, groups, and permissions are loaded from the database on every
    request so membership revocation takes effect without trusting browser claims or an owner
    shortcut.
    """

    session_factory: Any
    trusted_service_identities: frozenset[str] = field(
        default_factory=lambda: frozenset({"supervisor-bff"})
    )

    def resolve(self, context: IdentityRequestContext) -> RequestPrincipal:
        if context.internal_service_identity not in self.trusted_service_identities:
            raise UntrustedServiceIdentityError("unverified internal service identity")
        if context.session_id is None:
            raise UntrustedServiceIdentityError("unverified authenticated browser session")

        with self.session_factory() as session:
            installation = session.get(InstallationStateModel, "installation")
            if (
                installation is None
                or installation.state != "READY"
                or installation.edition != "SERVER"
                or installation.installation_mode not in {"SINGLE_USER", "MULTI_USER"}
                or installation.owner_user_id is None
            ):
                raise IdentityStateError("the Server installation is not ready")

            web_session = session.get(WebSessionModel, context.session_id)
            now = datetime.now(UTC)
            if (
                web_session is None
                or web_session.revoked_at is not None
                or now >= self._aware_utc(web_session.expires_at)
                or web_session.installation_state_version != installation.state_version
            ):
                raise UntrustedServiceIdentityError(
                    "invalid authenticated browser session evidence"
                )

            return self.resolve_principal_in_session(
                session,
                user_id=web_session.user_id,
                workspace_id=web_session.active_workspace_id,
                auth_generation=web_session.auth_generation,
            )

    def resolve_client_binding(self, binding) -> RequestPrincipal:
        from app.infrastructure.client_sessions import ClientSessionService

        with self.session_factory() as session:
            verifier = ClientSessionService(self.session_factory)
            verifier.validate_binding_in_session(session, binding, datetime.now(UTC))
            return self.resolve_principal_in_session(
                session,
                user_id=binding.user_id,
                workspace_id=binding.active_workspace_id,
                auth_generation=binding.auth_generation,
            )

    def resolve_principal_in_session(
        self,
        session,
        *,
        user_id: str,
        workspace_id: str,
        auth_generation: int | None = None,
    ) -> RequestPrincipal:
        installation = session.get(InstallationStateModel, "installation")
        if (
            installation is None
            or installation.state != "READY"
            or installation.edition != "SERVER"
            or installation.owner_user_id is None
            or installation.installation_mode not in {"SINGLE_USER", "MULTI_USER"}
        ):
            raise IdentityStateError("the Server installation is not ready")
        user = session.get(UserModel, user_id)
        membership = session.get(
            WorkspaceMembershipModel,
            (workspace_id, user_id),
        )
        if user is None or user.status != "ACTIVE" or membership is None:
            raise UntrustedServiceIdentityError(
                "authenticated session membership is no longer active"
            )
        if auth_generation is not None and user.auth_generation != auth_generation:
            raise UntrustedServiceIdentityError("revoked authenticated session generation")
        onboarding = session.get(UserOnboardingStateModel, user.id)
        if onboarding is None or onboarding.status not in {"PENDING", "COMPLETED"}:
            raise IdentityStateError("the authenticated account has no valid onboarding state")
        if (onboarding.status == "PENDING" and onboarding.completed_at is not None) or (
            onboarding.status == "COMPLETED" and onboarding.completed_at is None
        ):
            raise IdentityStateError("the authenticated account onboarding state is inconsistent")
        if installation.installation_mode == "SINGLE_USER":
            try:
                identity = DatabaseInstallationOwnerIdentityInvariant(
                    self.session_factory,
                    expected_edition=RuntimeEdition.SERVER,
                    expected_installation_mode=InstallationMode.SINGLE_USER,
                ).verify_in_session(session)
            except InstallationIdentityInvariantError as error:
                raise IdentityStateError(
                    "the single-user owner identity and permission graph is incomplete"
                ) from error
            if user.id != identity.owner_user_id or workspace_id != identity.workspace_id:
                raise IdentityStateError(
                    "the single-user session is not bound to the unique installation owner"
                )

        role_permission_keys = set(
            session.scalars(
                select(WorkspaceRolePermissionModel.permission_key).where(
                    WorkspaceRolePermissionModel.role == membership.role
                )
            ).all()
        )
        group_rows = list(
            session.execute(
                select(GroupModel.id, GroupModel.key)
                .join(
                    GroupMembershipModel,
                    GroupMembershipModel.group_id == GroupModel.id,
                )
                .where(GroupMembershipModel.user_id == user.id)
                .order_by(GroupModel.key)
            ).all()
        )
        group_ids = [row.id for row in group_rows]
        group_permission_keys: set[str] = set()
        if group_ids:
            group_permission_keys.update(
                session.scalars(
                    select(GroupPermissionModel.permission_key).where(
                        GroupPermissionModel.group_id.in_(group_ids)
                    )
                ).all()
            )

        known_permissions = {permission.value: permission for permission in Permission}
        permission_set = frozenset(
            known_permissions[key]
            for key in role_permission_keys | group_permission_keys
            if key in known_permissions
        )
        return RequestPrincipal(
            user_id=user.id,
            active_workspace_id=workspace_id,
            group_keys=frozenset(row.key for row in group_rows),
            permission_set=permission_set,
            identity_mode=IdentityMode.AUTHENTICATED,
            onboarding_required=onboarding.status == "PENDING",
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
