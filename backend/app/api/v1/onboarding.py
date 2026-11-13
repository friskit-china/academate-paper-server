from __future__ import annotations

from datetime import datetime
from typing import Annotated, Never

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.dependencies import resolve_session_request_principal
from app.application.credentials import (
    CredentialApplicationError,
    CredentialMetadata,
    CredentialWrite,
)
from app.application.onboarding import (
    OnboardingCompletion,
    OnboardingCredentialUnavailableError,
    OnboardingPrincipalDeniedError,
    OnboardingProfile,
    OnboardingService,
    OnboardingState,
    OnboardingStateMissingError,
)
from app.domain.auth import RequestPrincipal
from app.domain.credentials import SensitiveText

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validate the key after SecretStr construction so a rejected plaintext value cannot be
    # echoed from Pydantic's validation input.
    provider: str
    api_key: SecretStr
    api_base: str | None = None
    model: str | None = None


class OnboardingCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    automatic_translation_enabled: bool = False
    automatic_ai_repair_enabled: bool = False
    credential: OnboardingCredentialRequest | None = None


class OnboardingCredentialResponse(BaseModel):
    id: str
    provider: str
    api_base: str | None
    model: str | None
    configured: bool
    masked_suffix: str | None
    requires_reentry: bool
    updated_at: datetime


class OnboardingStatusResponse(BaseModel):
    status: OnboardingState
    onboarding_required: bool
    username: str
    display_name: str
    automatic_translation_enabled: bool
    automatic_ai_repair_enabled: bool
    default_credential: OnboardingCredentialResponse | None
    completed_at: datetime | None


def _service(request: Request) -> OnboardingService:
    service = getattr(request.app.state, "onboarding_service", None)
    if not isinstance(service, OnboardingService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personal onboarding is not configured for this runtime.",
        )
    return service


def _credential_write(value: OnboardingCredentialRequest | None) -> CredentialWrite | None:
    if value is None:
        return None
    return CredentialWrite(
        provider=value.provider,
        api_key=SensitiveText(value.api_key.get_secret_value()),
        api_base=value.api_base,
        model=value.model,
    )


def _credential_response(
    value: CredentialMetadata | None,
) -> OnboardingCredentialResponse | None:
    if value is None:
        return None
    return OnboardingCredentialResponse(
        id=value.id,
        provider=value.provider,
        api_base=value.api_base,
        model=value.model,
        configured=value.configured,
        masked_suffix=value.masked_suffix,
        requires_reentry=value.requires_reentry,
        updated_at=value.updated_at,
    )


def _response(profile: OnboardingProfile) -> OnboardingStatusResponse:
    return OnboardingStatusResponse(
        status=profile.state,
        onboarding_required=profile.onboarding_required,
        username=profile.username,
        display_name=profile.display_name,
        automatic_translation_enabled=profile.automatic_translation_enabled,
        automatic_ai_repair_enabled=profile.automatic_ai_repair_enabled,
        default_credential=_credential_response(profile.default_credential),
        completed_at=profile.completed_at,
    )


def _raise_onboarding_error(error: Exception) -> Never:
    if isinstance(error, OnboardingPrincipalDeniedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The active account cannot access this onboarding profile.",
        ) from error
    if isinstance(error, OnboardingStateMissingError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personal onboarding state is unavailable.",
        ) from error
    if isinstance(error, (OnboardingCredentialUnavailableError, CredentialApplicationError)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Protected personal credential storage is temporarily unavailable.",
        ) from error
    raise error


@router.get("/status", response_model=OnboardingStatusResponse)
def onboarding_status(
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_session_request_principal)],
) -> OnboardingStatusResponse:
    try:
        profile = _service(request).get_status(principal=principal)
    except (OnboardingPrincipalDeniedError, OnboardingStateMissingError) as error:
        _raise_onboarding_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _response(profile)


@router.post("/complete", response_model=OnboardingStatusResponse)
def complete_onboarding(
    payload: OnboardingCompleteRequest,
    request: Request,
    response: Response,
    principal: Annotated[RequestPrincipal, Depends(resolve_session_request_principal)],
) -> OnboardingStatusResponse:
    try:
        profile = _service(request).complete(
            principal=principal,
            completion=OnboardingCompletion(
                display_name=payload.display_name,
                automatic_translation_enabled=payload.automatic_translation_enabled,
                automatic_ai_repair_enabled=payload.automatic_ai_repair_enabled,
                credential=_credential_write(payload.credential),
            ),
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Personal onboarding input is invalid.",
        ) from error
    except (
        CredentialApplicationError,
        OnboardingCredentialUnavailableError,
        OnboardingPrincipalDeniedError,
        OnboardingStateMissingError,
    ) as error:
        _raise_onboarding_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _response(profile)
