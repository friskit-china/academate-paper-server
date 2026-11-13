from __future__ import annotations

from datetime import datetime
from typing import Literal, Never
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.dependencies import resolve_request_principal
from app.application.user_admin import (
    InstallationOwnerMutationError,
    ManagedUser,
    ManagedUserConflictError,
    ManagedUserNotFoundError,
    UserAdministrationDeniedError,
    UserAdministrationModeError,
    UserAdministrationService,
    UserAdministrationStateError,
)
from app.domain.auth import RequestPrincipal

router = APIRouter(
    prefix="/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(resolve_request_principal)],
)


class ManagedUserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    # The shared account validator owns length/content checks and returns a generic API error.
    # SecretStr schema constraints may echo the rejected plaintext in Pydantic's 422 payload.
    initial_password: SecretStr


class ManagedUserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    status: str
    workspace_id: str
    workspace_role: str
    is_installation_owner: bool
    created_at: datetime
    updated_at: datetime


class ManagedUserListResponse(BaseModel):
    users: list[ManagedUserResponse]


class CompatibilityManagedUserCreateResponse(ManagedUserResponse):
    """Deprecated direct-provisioning result; invitation registration is the normal flow."""

    onboarding_status: Literal["PENDING"] = "PENDING"
    onboarding_required: Literal[True] = True


def _response(user: ManagedUser) -> ManagedUserResponse:
    return ManagedUserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        status=user.status,
        workspace_id=user.workspace_id,
        workspace_role=user.workspace_role,
        is_installation_owner=user.is_installation_owner,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _service(request: Request) -> UserAdministrationService:
    service = getattr(request.app.state, "user_administration_service", None)
    if not isinstance(service, UserAdministrationService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User administration is not available in this runtime.",
        )
    return service


def _raise_admin_error(error: Exception) -> Never:
    if isinstance(error, UserAdministrationDeniedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required.",
        ) from error
    if isinstance(error, UserAdministrationModeError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User administration is disabled by the installation mode.",
        ) from error
    if isinstance(error, ManagedUserNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Managed user was not found.",
        ) from error
    if isinstance(error, ManagedUserConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The requested username is unavailable.",
        ) from error
    if isinstance(error, InstallationOwnerMutationError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The protected installation account cannot be disabled.",
        ) from error
    if isinstance(error, UserAdministrationStateError):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="User administration requires a ready Server installation.",
        ) from error
    raise error


@router.get("", response_model=ManagedUserListResponse)
def list_managed_users(request: Request) -> ManagedUserListResponse:
    principal: RequestPrincipal = request.state.request_principal
    try:
        users = _service(request).list_users(principal=principal)
    except (
        UserAdministrationDeniedError,
        UserAdministrationModeError,
        UserAdministrationStateError,
    ) as error:
        _raise_admin_error(error)
    return ManagedUserListResponse(users=[_response(user) for user in users])


@router.post(
    "",
    response_model=CompatibilityManagedUserCreateResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    description=(
        "Compatibility-only direct provisioning. The new member is always PENDING and must "
        "complete personal onboarding after first login. Normal administrators should use a "
        "single-use invitation instead."
    ),
)
def create_managed_user(
    payload: ManagedUserCreateRequest,
    request: Request,
) -> CompatibilityManagedUserCreateResponse:
    principal: RequestPrincipal = request.state.request_principal
    try:
        user = _service(request).create_user(
            principal=principal,
            username=payload.username,
            display_name=payload.display_name,
            initial_password=payload.initial_password.get_secret_value(),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="User account input is invalid.",
        ) from error
    except (
        ManagedUserConflictError,
        UserAdministrationDeniedError,
        UserAdministrationModeError,
        UserAdministrationStateError,
    ) as error:
        _raise_admin_error(error)
    return CompatibilityManagedUserCreateResponse(**_response(user).model_dump())


@router.post("/{user_id}/disable", response_model=ManagedUserResponse)
def disable_managed_user(user_id: UUID, request: Request) -> ManagedUserResponse:
    principal: RequestPrincipal = request.state.request_principal
    try:
        user = _service(request).disable_user(
            principal=principal,
            target_user_id=str(user_id),
        )
    except (
        InstallationOwnerMutationError,
        ManagedUserNotFoundError,
        UserAdministrationDeniedError,
        UserAdministrationModeError,
        UserAdministrationStateError,
    ) as error:
        _raise_admin_error(error)
    return _response(user)
