from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import build_api_v1_router
from app.application.credentials import CredentialService
from app.application.invitations import InvitationService
from app.application.local_sessions import (
    LocalSessionExchangeService,
    LocalSessionVerifier,
    TrustedLocalLaunchTokenService,
)
from app.application.onboarding import OnboardingService
from app.application.owner_setup import LocalOwnerSetupService
from app.application.registration import RegistrationService
from app.application.runtime import (
    APPLICATION_VERSION,
    RuntimeInstallationModeProvider,
    RuntimeSetupStateProvider,
)
from app.application.user_admin import UserAdministrationService
from app.application.web_sessions import (
    AutomaticOwnerSessionService,
    WebLoginService,
    WebLogoutService,
    WebSessionVerifier,
)
from app.config import RuntimeConfigurationError, Settings, get_settings
from app.domain.runtime import IdentityMode, StorageProfile, TaskQueueProfile
from app.infrastructure.credentials import (
    CredentialAdapterError,
    CredentialAdapterFactory,
    DatabaseCredentialRepository,
    build_credential_adapters,
)
from app.infrastructure.db import build_engine, build_session_factory
from app.infrastructure.identity import (
    AuthenticatedDatabaseIdentityProvider,
    LocalDatabaseIdentityProvider,
)
from app.infrastructure.internal_transport import (
    LOCAL_SESSION_COOKIE_NAME,
    InternalServiceTokenMiddleware,
)
from app.infrastructure.invitations import (
    DatabaseInvitationRepository,
    SecureInvitationTokenCodec,
)
from app.infrastructure.local_sessions import (
    DatabaseLocalSessionRepository,
    SecureLocalSessionTokenCodec,
)
from app.infrastructure.onboarding import DatabaseOnboardingRepository
from app.infrastructure.owner_setup import (
    DatabaseLocalOwnerSetupRepository,
    DatabaseRuntimeSetupStateProvider,
    DatabaseSetupStateStore,
)
from app.infrastructure.platform import build_storage_key_mapper
from app.infrastructure.registration import DatabaseRegistrationRepository
from app.infrastructure.runtime_installation import DatabaseRuntimeInstallationModeProvider
from app.infrastructure.security import ScryptPasswordHasher, SecureBootstrapTokenIssuer
from app.infrastructure.storage import LocalFileStorage
from app.infrastructure.tasks import DatabaseTaskQueue
from app.infrastructure.user_admin import DatabaseUserAdministrationRepository
from app.infrastructure.web_sessions import (
    DatabaseWebSessionRepository,
    SecureWebSessionTokenCodec,
)

if TYPE_CHECKING:
    from app.domain.auth import IdentityProvider


_DUMMY_PASSWORD_HASH = (
    "scrypt-v1$n=16384,r=8,p=1$YWNhZGVtYXRlLWR1bW15ISE=$"
    "Qqis2_RMwl4_jDQ1joueUbSwjdZiW4zZKTFc4HVMRyc="
)


def create_app(
    settings: Settings | None = None,
    *,
    setup_state_provider: RuntimeSetupStateProvider | None = None,
    installation_mode_provider: RuntimeInstallationModeProvider | None = None,
    identity_provider: IdentityProvider | None = None,
    local_owner_setup_service: LocalOwnerSetupService | None = None,
    local_session_verifier: LocalSessionVerifier | None = None,
    web_session_verifier: WebSessionVerifier | None = None,
    credential_adapter_factory: CredentialAdapterFactory = build_credential_adapters,
) -> FastAPI:
    application_settings = settings or get_settings()
    runtime_profile = application_settings.validate_runtime_configuration()
    if application_settings.app_env.strip().lower() in {"prod", "production"} and (
        application_settings.internal_service_token is None
    ):
        raise RuntimeConfigurationError(
            "Production API requires an externally supplied INTERNAL_SERVICE_TOKEN"
        )
    try:
        credential_adapters = credential_adapter_factory(
            runtime_profile=runtime_profile,
            os_secret_store_mode=application_settings.os_secret_store_mode,
            local_installation_id=application_settings.local_installation_id,
            credential_installation_id=application_settings.credential_installation_id,
            credential_master_key=application_settings.credential_master_key_material,
        )
    except CredentialAdapterError as error:
        raise RuntimeConfigurationError(str(error)) from error
    engine = build_engine(application_settings.database_url)
    session_factory = build_session_factory(engine)
    if runtime_profile.storage is not StorageProfile.LOCAL_FILES:
        raise AssertionError("validated runtime profile selected an unavailable storage adapter")
    storage = LocalFileStorage(
        application_settings.storage_root,
        path_mapper=build_storage_key_mapper(application_settings.storage_root),
    )
    if runtime_profile.task_queue is not TaskQueueProfile.SQLITE_DB:
        raise AssertionError("validated runtime profile selected an unavailable task queue adapter")
    task_queue = DatabaseTaskQueue(session_factory)
    password_hasher = ScryptPasswordHasher()

    session_cookie_name: str
    selected_session_verifier: LocalSessionVerifier | WebSessionVerifier
    if runtime_profile.identity_mode is IdentityMode.TRUSTED_LOCAL:
        local_session_repository = DatabaseLocalSessionRepository(session_factory)
        local_session_token_codec = SecureLocalSessionTokenCodec()
        selected_session_verifier = local_session_verifier or LocalSessionVerifier(
            codec=local_session_token_codec,
            repository=local_session_repository,
        )
        session_cookie_name = LOCAL_SESSION_COOKIE_NAME
    else:
        web_session_repository = DatabaseWebSessionRepository(session_factory)
        web_session_token_codec = SecureWebSessionTokenCodec(
            session_ttl=timedelta(seconds=application_settings.web_session_ttl_seconds)
        )
        invitation_token_codec = SecureInvitationTokenCodec()
        selected_session_verifier = web_session_verifier or WebSessionVerifier(
            codec=web_session_token_codec,
            repository=web_session_repository,
        )
        session_cookie_name = application_settings.web_session_cookie_name

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage.initialize()
        yield
        engine.dispose()

    app = FastAPI(
        title="AcadeMate Paper API",
        version=APPLICATION_VERSION,
        description="Private-first research paper reading workspace API.",
        lifespan=lifespan,
    )
    app.state.settings = application_settings
    app.state.runtime_profile = runtime_profile
    app.state.runtime_setup_state_provider = (
        setup_state_provider or DatabaseRuntimeSetupStateProvider(session_factory)
    )
    app.state.runtime_installation_mode_provider = (
        installation_mode_provider or DatabaseRuntimeInstallationModeProvider(session_factory)
    )
    app.state.setup_state_store = DatabaseSetupStateStore(session_factory)
    if identity_provider is not None:
        app.state.identity_provider = identity_provider
    elif runtime_profile.identity_mode is IdentityMode.TRUSTED_LOCAL:
        app.state.identity_provider = LocalDatabaseIdentityProvider(session_factory)
    else:
        app.state.identity_provider = AuthenticatedDatabaseIdentityProvider(session_factory)
    app.state.local_owner_setup_service = local_owner_setup_service or LocalOwnerSetupService(
        repository=DatabaseLocalOwnerSetupRepository(session_factory),
        token_issuer=SecureBootstrapTokenIssuer(),
        password_hasher=password_hasher,
    )
    if runtime_profile.identity_mode is IdentityMode.TRUSTED_LOCAL:
        app.state.local_launch_token_service = TrustedLocalLaunchTokenService(
            codec=local_session_token_codec,
            registry=local_session_repository,
        )
        app.state.local_session_exchange_service = LocalSessionExchangeService(
            codec=local_session_token_codec,
            repository=local_session_repository,
        )
    else:
        app.state.web_login_service = WebLoginService(
            repository=web_session_repository,
            codec=web_session_token_codec,
            password_hasher=password_hasher,
            dummy_password_hash=_DUMMY_PASSWORD_HASH,
        )
        app.state.web_logout_service = WebLogoutService(web_session_repository)
        app.state.automatic_owner_session_service = AutomaticOwnerSessionService(
            repository=web_session_repository,
            codec=web_session_token_codec,
            enabled=application_settings.single_user_auto_login_enabled,
        )
        app.state.user_administration_service = UserAdministrationService(
            repository=DatabaseUserAdministrationRepository(session_factory),
            password_hasher=password_hasher,
        )
        app.state.registration_service = RegistrationService(
            repository=DatabaseRegistrationRepository(session_factory),
            invitation_codec=invitation_token_codec,
            web_session_codec=web_session_token_codec,
            password_hasher=password_hasher,
        )
        if application_settings.web_public_origin is not None:
            app.state.invitation_service = InvitationService(
                repository=DatabaseInvitationRepository(session_factory),
                token_codec=invitation_token_codec,
                web_public_origin=application_settings.web_public_origin,
            )
    credential_service: CredentialService | None = None
    if credential_adapters is not None:
        credential_service = CredentialService(
            repository=DatabaseCredentialRepository(session_factory),
            secret_store=credential_adapters.secret_store,
            cipher=credential_adapters.cipher,
        )
        app.state.credential_service = credential_service
    if runtime_profile.identity_mode is IdentityMode.AUTHENTICATED:
        app.state.onboarding_service = OnboardingService(
            repository=DatabaseOnboardingRepository(session_factory),
            credential_service=credential_service,
        )
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage = storage
    app.state.task_queue = task_queue
    if runtime_profile.identity_mode is IdentityMode.AUTHENTICATED:
        from app.infrastructure.client_sessions import ClientSessionService

        app.state.client_sessions = ClientSessionService(session_factory)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=application_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added after CORS so this boundary is the outermost middleware and cannot be bypassed by a
    # direct browser preflight. Public bootstrap/readiness routes use an exact method/path
    # allowlist.
    middleware = InternalServiceTokenMiddleware
    transport_options = {}
    if runtime_profile.identity_mode is IdentityMode.AUTHENTICATED:
        from app.infrastructure.versioned_transport import VersionedTransportMiddleware

        middleware = VersionedTransportMiddleware
        transport_options = {
            "client_sessions": app.state.client_sessions,
            "session_factory": session_factory,
            "web_public_origin": application_settings.web_public_origin,
            "allowed_web_origins": application_settings.web_client_origins,
            "any_host_web_ports": (application_settings.server_https_web_ports
                                   if application_settings.server_https_any_host else []),
            "cookie_secure": application_settings.web_session_cookie_secure,
            "allow_insecure_http": application_settings.web_allow_insecure_http,
        }
    app.add_middleware(
        middleware,
        **transport_options,
        expected_token=application_settings.internal_service_token,
        session_verifier=selected_session_verifier,
        session_cookie_name=session_cookie_name,
    )
    app.include_router(
        build_api_v1_router(runtime_profile.identity_mode),
        prefix="/api/v1",
    )
    if runtime_profile.identity_mode is IdentityMode.AUTHENTICATED:
        from app.api.v2 import build_api_v2_router

        v2_router = build_api_v2_router()
        app.state.v2_contract_router = v2_router
        app.include_router(v2_router, prefix="/api/v2")
    _install_error_contract(app)
    return app


def _install_error_contract(app: FastAPI) -> None:
    from fastapi.exception_handlers import (
        http_exception_handler,
        request_validation_exception_handler,
    )
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        if not request.url.path.startswith("/api/v2/"):
            return await http_exception_handler(request, error)
        details = error.detail if isinstance(error.detail, dict) else {}
        code = details.get(
            "code",
            {
                400: "INVALID_REQUEST",
                401: "AUTHENTICATION_REQUIRED",
                403: "FORBIDDEN",
                404: "NOT_FOUND",
                409: "CONFLICT",
                422: "INVALID_REQUEST",
                429: "RATE_LIMITED",
            }.get(error.status_code, "REQUEST_FAILED"),
        )
        response = JSONResponse(
            {
                "code": code,
                "message": details.get("message", str(code)),
                "request_id": request.scope.get("papermanager.request_id", ""),
                "details": None,
            },
            status_code=error.status_code,
            headers=error.headers,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Missing fields can echo the whole request in input. An unknown field's name
        # can itself contain a pasted secret, so sensitive forms also omit locations.
        path = request.url.path
        endpoint_groups = path.strip("/").split("/")
        sensitive_form = len(endpoint_groups) >= 3 and endpoint_groups[2] in {
            "auth", "credentials", "setup", "onboarding", "local-session", "admin", "me",
        }
        if not path.startswith("/api/v2/"):
            if sensitive_form:
                return JSONResponse(
                    {"detail": "Request validation failed."}, status_code=422,
                    headers={"Cache-Control": "no-store"},
                )
            return await request_validation_exception_handler(request, error)
        # For other v2 resources only the location and type are part of the public contract.
        return JSONResponse(
            {
                "code": "INVALID_REQUEST",
                "message": "Request validation failed.",
                "request_id": request.scope.get("papermanager.request_id", ""),
                "details": None if sensitive_form else [
                    {"location": list(e["loc"]), "type": e["type"]} for e in error.errors()
                ],
            },
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )


app = create_app()
