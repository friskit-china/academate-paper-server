from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.domain.auth import Permission, RequestPrincipal
from app.domain.runtime import IdentityMode
from app.domain.setup import PasswordHasher
from app.domain.users import (
    normalized_display_name,
    normalized_username,
    validated_password,
)


class UserAdministrationDeniedError(PermissionError):
    pass


class UserAdministrationModeError(PermissionError):
    """Account administration is closed by persisted installation policy."""


class ManagedUserConflictError(RuntimeError):
    pass


class ManagedUserNotFoundError(LookupError):
    pass


class InstallationOwnerMutationError(PermissionError):
    pass


class UserAdministrationStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ManagedUser:
    id: str
    username: str
    display_name: str
    status: str
    workspace_id: str
    workspace_role: str
    is_installation_owner: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ManagedUserProvisioning:
    id: str
    username: str
    display_name: str
    password_hash: str = field(repr=False)
    created_at: datetime


class UserAdministrationRepository(Protocol):
    def list_users(self, *, principal: RequestPrincipal) -> tuple[ManagedUser, ...]: ...

    def create_user(
        self,
        *,
        principal: RequestPrincipal,
        provisioning: ManagedUserProvisioning,
    ) -> ManagedUser: ...

    def disable_user(
        self,
        *,
        principal: RequestPrincipal,
        target_user_id: str,
        now: datetime,
    ) -> ManagedUser: ...


class IdentifierFactory(Protocol):
    def __call__(self) -> str: ...


def _new_identifier() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class UserAdministrationService:
    repository: UserAdministrationRepository
    password_hasher: PasswordHasher
    identifier_factory: IdentifierFactory = _new_identifier

    def list_users(self, *, principal: RequestPrincipal) -> tuple[ManagedUser, ...]:
        self._require_administrator(principal)
        return self.repository.list_users(principal=principal)

    def create_user(
        self,
        *,
        principal: RequestPrincipal,
        username: str,
        display_name: str,
        initial_password: str,
        now: datetime | None = None,
    ) -> ManagedUser:
        self._require_administrator(principal)
        created_at = self._aware_time(now)
        password = validated_password(initial_password)
        provisioning = ManagedUserProvisioning(
            id=self.identifier_factory(),
            username=normalized_username(username),
            display_name=normalized_display_name(display_name),
            password_hash=self.password_hasher.hash(password),
            created_at=created_at,
        )
        return self.repository.create_user(
            principal=principal,
            provisioning=provisioning,
        )

    def disable_user(
        self,
        *,
        principal: RequestPrincipal,
        target_user_id: str,
        now: datetime | None = None,
    ) -> ManagedUser:
        self._require_administrator(principal)
        try:
            normalized_user_id = str(uuid.UUID(target_user_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ManagedUserNotFoundError("Managed user was not found.") from error
        return self.repository.disable_user(
            principal=principal,
            target_user_id=normalized_user_id,
            now=self._aware_time(now),
        )

    @staticmethod
    def _require_administrator(principal: RequestPrincipal) -> None:
        if (
            principal.identity_mode is not IdentityMode.AUTHENTICATED
            or "administrators" not in principal.group_keys
            or not principal.has(Permission.USER_MANAGE)
        ):
            raise UserAdministrationDeniedError(
                "An authenticated administrators-group principal is required."
            )

    @staticmethod
    def _aware_time(value: datetime | None) -> datetime:
        resolved = value or datetime.now(UTC)
        if resolved.tzinfo is None:
            raise ValueError("User administration timestamps must be timezone-aware.")
        return resolved
