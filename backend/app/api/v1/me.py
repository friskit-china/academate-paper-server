from __future__ import annotations

from typing import Annotated, Never

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import resolve_session_request_principal
from app.application.onboarding import (
    OnboardingPrincipalDeniedError,
    OnboardingProfile,
    OnboardingService,
    OnboardingStateMissingError,
    SelfProfileUpdate,
)
from app.domain.auth import RequestPrincipal

router = APIRouter(prefix="/me", tags=["me"])


class MeResponse(BaseModel):
    id: str
    username: str
    display_name: str
    active_workspace_id: str


class MeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)


def _service(request: Request) -> OnboardingService:
    service = getattr(request.app.state, "onboarding_service", None)
    if not isinstance(service, OnboardingService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personal onboarding is not configured for this runtime.",
        )
    return service


def _raise_profile_error(error: Exception) -> Never:
    if isinstance(error, OnboardingPrincipalDeniedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The active account cannot access this profile.",
        ) from error
    if isinstance(error, OnboardingStateMissingError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personal onboarding state is unavailable.",
        ) from error
    raise error


def _response(principal: RequestPrincipal, profile: OnboardingProfile) -> MeResponse:
    return MeResponse(
        id=principal.user_id,
        username=profile.username,
        display_name=profile.display_name,
        active_workspace_id=principal.active_workspace_id,
    )


@router.get("", response_model=MeResponse)
def get_me(
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_session_request_principal)],
) -> MeResponse:
    try:
        profile = _service(request).get_status(principal=principal)
    except (OnboardingPrincipalDeniedError, OnboardingStateMissingError) as error:
        _raise_profile_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _response(principal, profile)


@router.patch("", response_model=MeResponse)
def update_me(
    payload: MeUpdateRequest,
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_session_request_principal)],
) -> MeResponse:
    try:
        profile = _service(request).update_profile(
            principal=principal,
            update=SelfProfileUpdate(display_name=payload.display_name),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Personal profile input is invalid.",
        ) from error
    except (OnboardingPrincipalDeniedError, OnboardingStateMissingError) as error:
        _raise_profile_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _response(principal, profile)
