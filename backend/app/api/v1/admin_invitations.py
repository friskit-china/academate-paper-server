from __future__ import annotations

from datetime import datetime, timedelta
from typing import Never

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import resolve_request_principal
from app.application.invitations import (
    CreatedInvitation,
    InvitationActor,
    InvitationDeniedError,
    InvitationModeError,
    InvitationNotFoundError,
    InvitationNotRevocableError,
    InvitationService,
    InvitationStateError,
    InvitationSummary,
)
from app.domain.auth import RequestPrincipal
from app.domain.invitations import InvitationIssuerChannel, InvitationStatus

router = APIRouter(
    prefix="/admin/invitations",
    tags=["admin-invitations"],
    dependencies=[Depends(resolve_request_principal)],
)


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_seconds: int = Field(default=604_800, ge=60, le=7_776_000)
    label: str | None = Field(default=None, max_length=128)


class InvitationResponse(BaseModel):
    id: str
    workspace_id: str
    masked_hint: str
    issuer_channel: InvitationIssuerChannel
    label: str | None
    status: InvitationStatus
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    redeemed_by_user_id: str | None
    redeemed_by_username: str | None
    redeemed_by_display_name: str | None
    revoked_at: datetime | None


class InvitationCreateResponse(InvitationResponse):
    # This one-time secret must remain serializable in the create response while staying out of
    # model/log representations.
    registration_url: str = Field(repr=False)


class InvitationListResponse(BaseModel):
    invitations: list[InvitationResponse]


def _service(request: Request) -> InvitationService:
    service = getattr(request.app.state, "invitation_service", None)
    if not isinstance(service, InvitationService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invitation management is not configured for this runtime.",
        )
    return service


def _actor(request: Request) -> InvitationActor:
    principal: RequestPrincipal = request.state.request_principal
    return InvitationActor.web_admin(principal)


def _response(invitation: InvitationSummary) -> InvitationResponse:
    return InvitationResponse(
        id=invitation.id,
        workspace_id=invitation.workspace_id,
        masked_hint=invitation.masked_hint,
        issuer_channel=invitation.issuer_channel,
        label=invitation.label,
        status=invitation.status,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
        redeemed_at=invitation.redeemed_at,
        redeemed_by_user_id=invitation.redeemed_by_user_id,
        redeemed_by_username=invitation.redeemed_by_username,
        redeemed_by_display_name=invitation.redeemed_by_display_name,
        revoked_at=invitation.revoked_at,
    )


def _created_response(created: CreatedInvitation) -> InvitationCreateResponse:
    invitation = _response(created.invitation)
    return InvitationCreateResponse(
        **invitation.model_dump(),
        registration_url=created.registration_url,
    )


def _raise_invitation_error(error: Exception) -> Never:
    if isinstance(error, InvitationDeniedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required.",
        ) from error
    if isinstance(error, InvitationModeError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invitations are disabled by the installation mode.",
        ) from error
    if isinstance(error, InvitationNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation was not found.",
        ) from error
    if isinstance(error, InvitationNotRevocableError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an active invitation can be revoked.",
        ) from error
    if isinstance(error, InvitationStateError):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Invitation management requires a ready Server installation.",
        ) from error
    raise error


@router.post(
    "",
    response_model=InvitationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation(
    payload: InvitationCreateRequest,
    request: Request,
    response: Response,
) -> InvitationCreateResponse:
    try:
        created = _service(request).create(
            actor=_actor(request),
            expires_in=timedelta(seconds=payload.expires_in_seconds),
            label=payload.label,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invitation input is invalid.",
        ) from error
    except (
        InvitationDeniedError,
        InvitationModeError,
        InvitationStateError,
    ) as error:
        _raise_invitation_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _created_response(created)


@router.get("", response_model=InvitationListResponse)
def list_invitations(
    request: Request,
    response: Response,
    include_inactive: bool = Query(default=True),
) -> InvitationListResponse:
    try:
        invitations = _service(request).list(
            actor=_actor(request),
            include_inactive=include_inactive,
        )
    except (
        InvitationDeniedError,
        InvitationModeError,
        InvitationStateError,
    ) as error:
        _raise_invitation_error(error)
    response.headers["Cache-Control"] = "no-store"
    return InvitationListResponse(
        invitations=[_response(invitation) for invitation in invitations]
    )


@router.get("/{invitation_id}", response_model=InvitationResponse)
def invitation_status(
    invitation_id: str,
    request: Request,
    response: Response,
) -> InvitationResponse:
    try:
        invitation = _service(request).status(
            actor=_actor(request),
            invitation_id=invitation_id,
        )
    except (
        InvitationDeniedError,
        InvitationModeError,
        InvitationNotFoundError,
        InvitationStateError,
    ) as error:
        _raise_invitation_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _response(invitation)


@router.post("/{invitation_id}/revoke", response_model=InvitationResponse)
def revoke_invitation(
    invitation_id: str,
    request: Request,
    response: Response,
) -> InvitationResponse:
    try:
        invitation = _service(request).revoke(
            actor=_actor(request),
            invitation_id=invitation_id,
        )
    except (
        InvitationDeniedError,
        InvitationModeError,
        InvitationNotFoundError,
        InvitationNotRevocableError,
        InvitationStateError,
    ) as error:
        _raise_invitation_error(error)
    response.headers["Cache-Control"] = "no-store"
    return _response(invitation)
