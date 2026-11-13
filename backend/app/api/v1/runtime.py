from dataclasses import asdict

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.application.runtime import (
    API_VERSION,
    APPLICATION_VERSION,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
    RuntimeSetupState,
    capability_flags_for,
)
from app.domain.runtime import IdentityMode, RuntimeEdition
from app.domain.setup import InstallationMode

router = APIRouter(prefix="/runtime", tags=["runtime"])


class RuntimeCapabilityFlagsResponse(BaseModel):
    auth_ui: bool
    automatic_owner_session: bool
    registration: bool
    invitations: bool
    user_management: bool
    group_management: bool
    workspace_switching: bool
    system_status: bool
    desktop_supervisor: bool


class RuntimeVersionResponse(BaseModel):
    app: str
    api: str


class RuntimeCapabilitiesResponse(BaseModel):
    schema_version: int
    edition: RuntimeEdition
    identity_mode: IdentityMode
    installation_mode: InstallationMode | None
    setup_state: RuntimeSetupState
    capabilities: RuntimeCapabilityFlagsResponse
    version: RuntimeVersionResponse


@router.get("/capabilities", response_model=RuntimeCapabilitiesResponse)
def runtime_capabilities(request: Request, response: Response) -> RuntimeCapabilitiesResponse:
    profile = request.app.state.runtime_profile
    setup_state = request.app.state.runtime_setup_state_provider.get_setup_state()
    installation_mode = (
        request.app.state.runtime_installation_mode_provider.get_installation_mode()
    )
    response.headers["Cache-Control"] = "no-store"
    return RuntimeCapabilitiesResponse(
        schema_version=RUNTIME_CAPABILITY_SCHEMA_VERSION,
        edition=profile.edition,
        identity_mode=profile.identity_mode,
        installation_mode=installation_mode,
        setup_state=setup_state,
        capabilities=RuntimeCapabilityFlagsResponse(
            **asdict(
                capability_flags_for(
                    profile,
                    installation_mode,
                    setup_state=setup_state,
                    automatic_owner_session_enabled=(
                        request.app.state.settings.single_user_auto_login_enabled
                    ),
                )
            )
        ),
        version=RuntimeVersionResponse(app=APPLICATION_VERSION, api=API_VERSION),
    )
