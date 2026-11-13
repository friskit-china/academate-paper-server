"""Invitation registration shares the existing account transaction, then issues v2 credentials."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.v1.auth import WebRegistrationRequest
from app.api.v2_auth import NativeTokenResponse, WebTokenResponse, _payload, _token_response
from app.application.registration import (
    RegistrationInviteInvalidError,
    RegistrationStateError,
    RegistrationUnavailableError,
    RegistrationUsernameConflictError,
)
from app.infrastructure.client_sessions import (
    CLIENT_TRANSPORT_SCOPE_KEY,
    ClientAuthenticationError,
    ClientRateLimitError,
)
from app.infrastructure.db.models import WebSessionModel
from app.infrastructure.web_sessions import SecureWebSessionTokenCodec

router = APIRouter(tags=["registration"])


@router.post(
    "/auth/registrations",
    status_code=201,
    response_model=NativeTokenResponse | WebTokenResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": WebRegistrationRequest.model_json_schema()}},
        }
    },
)
async def registration(request: Request):
    payload = await _payload(request, WebRegistrationRequest)
    try:
        result = await run_in_threadpool(
            request.app.state.registration_service.register,
            invite_token=payload.invite_token.get_secret_value(),
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password.get_secret_value(),
        )
    except RegistrationInviteInvalidError:
        raise HTTPException(401, detail={"code": "INVALID_INVITATION"}) from None
    except RegistrationUnavailableError:
        raise HTTPException(403, detail={"code": "REGISTRATION_UNAVAILABLE"}) from None
    except RegistrationUsernameConflictError:
        raise HTTPException(409, detail={"code": "USERNAME_UNAVAILABLE"}) from None
    except (ValueError, RegistrationStateError):
        raise HTTPException(422, detail={"code": "INVALID_REGISTRATION"}) from None
    # The legacy domain ceremony creates a legacy session as part of its atomic transaction.
    # It is never delivered here, and is revoked before issuing the requested v2 transport.
    digest = SecureWebSessionTokenCodec().digest(result.raw_session_token)
    with request.app.state.session_factory() as session:
        legacy = session.scalar(
            select(WebSessionModel).where(WebSessionModel.token_digest == digest)
        )
        if legacy is not None:
            legacy.revoked_at = datetime.now(UTC)
        session.commit()
    try:
        pair = await run_in_threadpool(
            request.app.state.client_sessions.login,
            username=payload.username,
            password=payload.password.get_secret_value(),
            client_kind=request.scope[CLIENT_TRANSPORT_SCOPE_KEY],
            requested_workspace_id=result.active_workspace_id,
            client_name="registration",
            device_name="new client",
            peer=request.client.host if request.client else "unknown",
        )
    except (ClientAuthenticationError, ClientRateLimitError):
        # Account creation is durable; an interrupted session issue can be recovered by login.
        raise HTTPException(503, detail={"code": "ACCOUNT_CREATED_LOGIN_REQUIRED"}) from None
    response = _token_response(request, pair)
    response.status_code = 201
    return response
