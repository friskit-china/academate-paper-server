from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.domain.runtime import IdentityMode


class Permission(StrEnum):
    LIBRARY_READ = "library.read"
    LIBRARY_WRITE = "library.write"
    ANNOTATION_READ = "annotation.read"
    ANNOTATION_WRITE = "annotation.write"
    PAPER_MAP_READ = "paper_map.read"
    PAPER_MAP_WRITE = "paper_map.write"
    SETTINGS_READ = "settings.read"
    SETTINGS_WRITE = "settings.write"
    TASK_READ = "task.read"
    TASK_WRITE = "task.write"
    CREDENTIAL_READ = "credential.read"
    CREDENTIAL_WRITE = "credential.write"
    SYSTEM_STATUS_READ = "system_status.read"
    USER_MANAGE = "user.manage"


OWNER_PERMISSIONS = frozenset(Permission)
WORKSPACE_PERMISSIONS = frozenset(
    permission for permission in Permission if permission is not Permission.USER_MANAGE
)


@dataclass(frozen=True, slots=True)
class RequestPrincipal:
    user_id: str
    active_workspace_id: str
    group_keys: frozenset[str] = field(default_factory=frozenset)
    permission_set: frozenset[Permission] = field(default_factory=frozenset)
    identity_mode: IdentityMode = IdentityMode.TRUSTED_LOCAL
    # This is reconstructed from durable server state with the rest of the principal.  A default
    # keeps legacy/trusted-local adapters and focused test doubles source-compatible; the
    # authenticated database adapter must set it explicitly and fails closed when the row is
    # missing.
    onboarding_required: bool = False

    def has(self, permission: Permission) -> bool:
        return permission in self.permission_set


@dataclass(frozen=True, slots=True)
class IdentityRequestContext:
    """Transport facts accepted by an identity adapter.

    User, workspace, group, Edition and permission values are deliberately absent. They must come
    from trusted server-side session state and database memberships, never browser headers.
    """

    peer_host: str | None
    internal_service_identity: str | None
    session_id: str | None


class IdentityProvider(Protocol):
    def resolve(self, context: IdentityRequestContext) -> RequestPrincipal:
        """Resolve trusted transport/session evidence to an application principal."""


class AuthorizationError(PermissionError):
    pass


class AuthorizationPolicy:
    def require_permission(
        self,
        principal: RequestPrincipal,
        permission: Permission,
    ) -> None:
        if not principal.has(permission):
            raise AuthorizationError("The principal does not have the required permission.")

    def require_workspace(
        self,
        principal: RequestPrincipal,
        workspace_id: str,
    ) -> None:
        if workspace_id != principal.active_workspace_id:
            raise AuthorizationError("The resource is not visible in the active workspace.")

    def authorize(
        self,
        principal: RequestPrincipal,
        *,
        permission: Permission,
        workspace_id: str,
    ) -> None:
        self.require_permission(principal, permission)
        self.require_workspace(principal, workspace_id)
