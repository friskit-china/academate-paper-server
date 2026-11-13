from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, SecretStr

from app.api.dependencies import resolve_request_principal
from app.application.local_sessions import (
    LocalLaunchTokenExpiredError,
    LocalLaunchTokenInvalidError,
    LocalLaunchTokenReplayError,
    LocalSessionStateError,
)
from app.application.runtime import RuntimeSetupState
from app.infrastructure.internal_transport import (
    INTERNAL_SERVICE_IDENTITY_SCOPE_KEY,
    LOCAL_SESSION_COOKIE_NAME,
    VERIFIED_BFF_IDENTITY,
)

router = APIRouter(prefix="/local-session", tags=["local-session"])


class LocalSessionExchangeRequest(BaseModel):
    launch_token: SecretStr


class LocalSessionStatusResponse(BaseModel):
    authenticated: bool
    setup_state: RuntimeSetupState


@router.get(
    "/status",
    response_model=LocalSessionStatusResponse,
    dependencies=[Depends(resolve_request_principal)],
)
def local_session_status(request: Request, response: Response) -> LocalSessionStatusResponse:
    """Confirm that transport session evidence resolves to a database-backed principal."""

    response.headers["Cache-Control"] = "no-store"
    return LocalSessionStatusResponse(
        authenticated=True,
        setup_state=request.app.state.runtime_setup_state_provider.get_setup_state(),
    )


@router.post("/exchange", status_code=status.HTTP_204_NO_CONTENT)
def exchange_local_launch_token(
    payload: LocalSessionExchangeRequest,
    request: Request,
) -> Response:
    # This endpoint is session-exempt only after the outer transport middleware authenticates the
    # private BFF service token.  A browser cannot manufacture this ASGI-scope marker.
    if request.scope.get(INTERNAL_SERVICE_IDENTITY_SCOPE_KEY) != VERIFIED_BFF_IDENTITY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A trusted application exchange is required.",
        )

    exchanged_at = datetime.now(UTC)
    try:
        result = request.app.state.local_session_exchange_service.exchange(
            payload.launch_token.get_secret_value(),
            now=exchanged_at,
        )
    except LocalLaunchTokenInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The browser launch proof is invalid.",
        ) from error
    except LocalLaunchTokenExpiredError as error:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="The browser launch proof has expired.",
        ) from error
    except LocalLaunchTokenReplayError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The browser launch proof was already used.",
        ) from error
    except LocalSessionStateError as error:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Local browser sessions are closed until setup is READY.",
        ) from error

    max_age = max(1, int((result.expires_at - exchanged_at).total_seconds()))
    response = Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        key=LOCAL_SESSION_COOKIE_NAME,
        value=result.raw_session_token,
        max_age=max_age,
        expires=result.expires_at,
        path="/",
        secure=False,
        httponly=True,
        samesite="strict",
    )
    return response
