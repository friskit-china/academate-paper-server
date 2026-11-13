from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from app.api.dependencies import resolve_session_request_principal
from app.application.registration import (
    RegistrationInviteInvalidError,
    RegistrationService,
    RegistrationStateError,
    RegistrationUnavailableError,
    RegistrationUsernameConflictError,
)
from app.application.web_sessions import (
    AutomaticOwnerSessionUnavailableError,
    WebAuthenticationFailedError,
    WebLoginResult,
    WebSessionInvalidError,
    WebSessionSetupNotReadyError,
    WebSessionStateError,
)
from app.domain.auth import Permission, RequestPrincipal
from app.domain.runtime import IdentityMode
from app.infrastructure.db.models import (
    GroupMembershipModel,
    GroupModel,
    GroupPermissionModel,
    InstallationStateModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from app.infrastructure.internal_transport import (
    INTERNAL_SERVICE_IDENTITY_SCOPE_KEY,
    LOCAL_SESSION_ID_SCOPE_KEY,
    VERIFIED_BFF_IDENTITY,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class WebLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=128)
    # Service authentication owns validation.  Constraints on SecretStr can make Pydantic include
    # a rejected plaintext input in its 422 error structure.
    password: SecretStr
    workspace_id: UUID | None = None


class WebLoginResponse(BaseModel):
    authenticated: bool
    user_id: str
    active_workspace_id: str
    expires_at: datetime


class WebRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Length and format validation deliberately runs after SecretStr construction so a rejected
    # invitation or password cannot appear in Pydantic's validation ``input`` payload.
    invite_token: SecretStr
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    password: SecretStr


class WebSessionUserResponse(BaseModel):
    id: str
    username: str
    display_name: str


class WebSessionWorkspaceResponse(BaseModel):
    id: str
    name: str


class WebSessionStatusResponse(BaseModel):
    authenticated: bool
    user_id: str
    active_workspace_id: str
    user: WebSessionUserResponse
    workspace: WebSessionWorkspaceResponse
    onboarding_required: bool
    can_manage_users: bool
    can_manage_invitations: bool


def _set_session_cookie(
    *,
    request: Request,
    response: Response,
    result: WebLoginResult,
) -> None:
    now = datetime.now(UTC)
    max_age = max(1, int((result.expires_at - now).total_seconds()))
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=request.app.state.settings.web_session_cookie_name,
        value=result.raw_session_token,
        max_age=max_age,
        expires=result.expires_at,
        path="/",
        secure=request.app.state.settings.web_session_cookie_secure,
        httponly=True,
        samesite="strict",
    )


@router.post("/login", response_model=WebLoginResponse)
def login(payload: WebLoginRequest, request: Request, response: Response) -> WebLoginResponse:
    try:
        result = request.app.state.web_login_service.login(
            username=payload.username,
            password=payload.password.get_secret_value(),
            requested_workspace_id=(
                str(payload.workspace_id) if payload.workspace_id is not None else None
            ),
        )
    except WebAuthenticationFailedError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The username or password is invalid.",
        ) from error
    except WebSessionStateError as error:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Authenticated sessions are unavailable until setup is ready.",
        ) from error

    _set_session_cookie(request=request, response=response, result=result)
    return WebLoginResponse(
        authenticated=True,
        user_id=result.user_id,
        active_workspace_id=result.active_workspace_id,
        expires_at=result.expires_at,
    )


@router.post(
    "/register",
    response_model=WebLoginResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: WebRegistrationRequest,
    request: Request,
    response: Response,
) -> WebLoginResponse:
    if request.scope.get(INTERNAL_SERVICE_IDENTITY_SCOPE_KEY) != VERIFIED_BFF_IDENTITY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A trusted application session is required.",
        )
    service = getattr(request.app.state, "registration_service", None)
    if not isinstance(service, RegistrationService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invitation registration is not configured for this runtime.",
        )
    try:
        result = service.register(
            invite_token=payload.invite_token.get_secret_value(),
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password.get_secret_value(),
        )
    except RegistrationInviteInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This invitation cannot be used. Ask an administrator for a new invitation.",
        ) from error
    except RegistrationUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invitation registration is unavailable for this installation.",
        ) from error
    except RegistrationUsernameConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The requested username is unavailable.",
        ) from error
    except RegistrationStateError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invitation registration could not be completed safely.",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Registration input is invalid.",
        ) from error

    _set_session_cookie(request=request, response=response, result=result)
    return WebLoginResponse(
        authenticated=True,
        user_id=result.user_id,
        active_workspace_id=result.active_workspace_id,
        expires_at=result.expires_at,
    )


@router.post(
    "/automatic-owner-session",
    status_code=status.HTTP_204_NO_CONTENT,
)
def create_automatic_owner_session(request: Request) -> Response:
    # The middleware owns service-token verification. Keep this exact marker check at the route as
    # defense in depth so direct service/unit invocation cannot accidentally turn deployment policy
    # into an HTTP user identity claim.
    if request.scope.get(INTERNAL_SERVICE_IDENTITY_SCOPE_KEY) != VERIFIED_BFF_IDENTITY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A trusted application session is required.",
        )
    try:
        result = request.app.state.automatic_owner_session_service.issue()
    except AutomaticOwnerSessionUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Automatic owner session is unavailable for this installation.",
        ) from error
    except WebSessionSetupNotReadyError as error:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Automatic owner sessions are unavailable until setup is ready.",
        ) from error
    except WebSessionStateError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The single-user owner identity state is unavailable.",
        ) from error

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookie(request=request, response=response, result=result)
    return response


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(resolve_session_request_principal)],
)
def logout(request: Request) -> Response:
    session_id = request.scope.get(LOCAL_SESSION_ID_SCOPE_KEY)
    if not isinstance(session_id, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid browser session is required.",
        )
    try:
        request.app.state.web_logout_service.logout(session_id=session_id)
    except WebSessionInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid browser session is required.",
        ) from error
    response = Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "no-store"},
    )
    response.delete_cookie(
        key=request.app.state.settings.web_session_cookie_name,
        path="/",
        secure=request.app.state.settings.web_session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/status", response_model=WebSessionStatusResponse)
def session_status(
    request: Request,
    response: Response,
    principal: Annotated[
        RequestPrincipal,
        Depends(resolve_session_request_principal),
    ],
) -> WebSessionStatusResponse:
    with request.app.state.session_factory() as session:
        installation = session.get(InstallationStateModel, "installation")
        user = session.get(UserModel, principal.user_id)
        workspace = session.get(WorkspaceModel, principal.active_workspace_id)
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
            installation is None
            or user is None
            or user.status != "ACTIVE"
            or workspace is None
            or membership is None
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The authenticated account status is unavailable.",
            )
        can_manage_accounts = bool(
            installation.state == "READY"
            and installation.edition == "SERVER"
            and installation.installation_mode == "MULTI_USER"
            and principal.identity_mode is IdentityMode.AUTHENTICATED
            and not principal.onboarding_required
            and "administrators" in principal.group_keys
            and principal.has(Permission.USER_MANAGE)
            and persisted_administrator is not None
        )
        user_response = WebSessionUserResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
        )
        workspace_response = WebSessionWorkspaceResponse(
            id=workspace.id,
            name=workspace.name,
        )
    response.headers["Cache-Control"] = "no-store"
    return WebSessionStatusResponse(
        authenticated=True,
        user_id=principal.user_id,
        active_workspace_id=principal.active_workspace_id,
        user=user_response,
        workspace=workspace_response,
        onboarding_required=principal.onboarding_required,
        can_manage_users=can_manage_accounts,
        can_manage_invitations=can_manage_accounts,
    )
