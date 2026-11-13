from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.domain.auth import (
    AuthorizationError,
    AuthorizationPolicy,
    IdentityRequestContext,
    Permission,
    RequestPrincipal,
)
from app.infrastructure.identity import (
    IdentityStateError,
    UntrustedServiceIdentityError,
)
from app.infrastructure.internal_transport import (
    INTERNAL_SERVICE_IDENTITY_SCOPE_KEY,
    LOCAL_SESSION_ID_SCOPE_KEY,
)

_PRINCIPAL_STATE_KEY = "request_principal"


def resolve_session_request_principal(request: Request) -> RequestPrincipal:
    """Resolve a principal from server-owned transport state.

    Browser headers and query parameters are intentionally not inspected here.  The future BFF
    adapter must first authenticate its service token and then place only the verified service
    identity marker in the ASGI scope under ``INTERNAL_SERVICE_IDENTITY_SCOPE_KEY``.
    """

    client = request.client
    context = IdentityRequestContext(
        peer_host=client.host if client is not None else None,
        internal_service_identity=request.scope.get(INTERNAL_SERVICE_IDENTITY_SCOPE_KEY),
        session_id=request.scope.get(LOCAL_SESSION_ID_SCOPE_KEY),
    )
    from app.infrastructure.client_sessions import (
        CLIENT_BINDING_SCOPE_KEY,
        ClientAuthenticationError,
        VerifiedClientSessionBinding,
    )

    try:
        from app.api.v2_sync import (
            INTEGRATION_BINDING_SCOPE_KEY,
            VerifiedIntegrationBinding,
            resolve_integration_binding,
        )

        integration = request.scope.get(INTEGRATION_BINDING_SCOPE_KEY)
        binding = request.scope.get(CLIENT_BINDING_SCOPE_KEY)
        if isinstance(integration, VerifiedIntegrationBinding):
            principal = resolve_integration_binding(integration, request.app.state.session_factory)
        elif isinstance(binding, VerifiedClientSessionBinding):
            principal = request.app.state.identity_provider.resolve_client_binding(binding)
        else:
            principal = request.app.state.identity_provider.resolve(context)
    except (UntrustedServiceIdentityError, ClientAuthenticationError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A trusted application session is required.",
        ) from error
    except IdentityStateError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The application identity state is not ready.",
        ) from error
    setattr(request.state, _PRINCIPAL_STATE_KEY, principal)
    return principal


def resolve_request_principal(request: Request) -> RequestPrincipal:
    """Resolve the session principal and enforce the global onboarding boundary.

    Protected application routers depend on this function, so a PENDING account cannot reach
    library, Paper, task, credential, settings, or administration services.  The tiny allowlist
    of self-scoped onboarding/session routes uses ``resolve_session_request_principal`` directly;
    transport authentication and database principal reconstruction still happen there.
    """

    principal = resolve_session_request_principal(request)
    if principal.onboarding_required:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ONBOARDING_REQUIRED",
                "message": "Complete personal onboarding before using the workspace.",
            },
        )
    return principal


def require_request_permission(request: Request, permission: Permission) -> RequestPrincipal:
    principal = getattr(request.state, _PRINCIPAL_STATE_KEY, None)
    if not isinstance(principal, RequestPrincipal):
        # This is an application wiring error, not a browser-controlled fallback path.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The request identity was not resolved.",
        )
    try:
        AuthorizationPolicy().require_permission(principal, permission)
    except AuthorizationError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The active principal cannot perform this operation.",
        ) from error
    return principal
