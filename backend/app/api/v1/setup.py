from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.application.credentials import (
    CredentialApplicationError,
    CredentialMetadata,
    CredentialService,
    CredentialWrite,
)
from app.application.owner_setup import (
    BootstrapTokenExpiredError,
    BootstrapTokenInvalidError,
    BootstrapTokenReplayError,
    LocalActiveUserLimitError,
    LocalOwnerSetupConflictError,
    LocalOwnerSetupResult,
)
from app.application.runtime import RuntimeSetupState
from app.application.setup import (
    SetupStateSnapshot,
    SetupTransitionError,
)
from app.application.setup import (
    SetupStateVersionConflict as InstallationStateVersionConflict,
)
from app.domain.credentials import SensitiveText
from app.domain.os_adapters import (
    ExternalToolchainComponentState,
    ExternalToolchainKind,
    ExternalToolchainState,
)
from app.domain.owner_setup import LocalOwnerCredentials
from app.domain.setup import InstallationMode
from app.domain.users import PERSONAL_ACCOUNT

router = APIRouter(prefix="/setup", tags=["setup"])


class ExternalToolchainComponentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mactex: ExternalToolchainComponentState
    ghostscript: ExternalToolchainComponentState
    ghostscript_extras: ExternalToolchainComponentState


class ExternalToolchainStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ExternalToolchainKind
    state: ExternalToolchainState
    official_url: str
    restart_required: bool
    components: ExternalToolchainComponentsResponse


class SetupStatusResponse(BaseModel):
    setup_state: RuntimeSetupState
    state_version: int
    installation_mode: InstallationMode | None
    allowed_installation_modes: list[InstallationMode]
    owner_required: bool
    mutations_open: bool
    external_toolchain: ExternalToolchainStatusResponse | None


class LocalLlmCredentialSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "glm"
    api_key: SecretStr
    api_base: str
    model: str


class LocalOwnerSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_token: SecretStr
    username: str = PERSONAL_ACCOUNT
    display_name: str = PERSONAL_ACCOUNT
    password: SecretStr
    installation_mode: InstallationMode
    workspace_name: str = Field(min_length=1, max_length=128)
    expected_state_version: int = Field(ge=0)
    llm_credential: LocalLlmCredentialSetupRequest | None = None


class CredentialMetadataSetupResponse(BaseModel):
    id: str
    provider: str
    api_base: str | None
    model: str | None
    configured: bool
    masked_suffix: str | None
    requires_reentry: bool


class LocalOwnerSetupResponse(BaseModel):
    setup_state: RuntimeSetupState
    state_version: int
    installation_mode: InstallationMode
    replayed: bool
    llm_credential: CredentialMetadataSetupResponse | None


_CREDENTIAL_REQUEST_DOMAIN = b"academatepaper.local-owner-llm-credential.v1\0"


def _credential_write(
    payload: LocalLlmCredentialSetupRequest,
) -> tuple[CredentialWrite, str]:
    raw_api_key = payload.api_key.get_secret_value()
    value = CredentialWrite(
        provider=payload.provider,
        api_key=SensitiveText(raw_api_key),
        api_base=payload.api_base,
        model=payload.model,
    )
    canonical = json.dumps(
        {
            "api_base": value.api_base,
            "api_key": raw_api_key,
            "model": value.model,
            "provider": value.provider.strip().lower(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    binding = hashlib.sha256(_CREDENTIAL_REQUEST_DOMAIN + canonical).hexdigest()
    return value, f"sha256-v1:{binding}"


def _setup_credential_response(
    metadata: CredentialMetadata,
) -> CredentialMetadataSetupResponse:
    return CredentialMetadataSetupResponse(
        id=metadata.id,
        provider=metadata.provider,
        api_base=metadata.api_base,
        model=metadata.model,
        configured=metadata.configured,
        masked_suffix=metadata.masked_suffix,
        requires_reentry=metadata.requires_reentry,
    )


@router.get("/status", response_model=SetupStatusResponse)
def setup_status(request: Request, response: Response) -> SetupStatusResponse:
    setup_status_snapshot = request.app.state.setup_state_store.read_status()
    setup_state = setup_status_snapshot.state
    external_toolchain = request.app.state.settings.external_toolchain_setup_status
    response.headers["Cache-Control"] = "no-store"
    return SetupStatusResponse(
        setup_state=setup_state,
        state_version=setup_status_snapshot.state_version,
        installation_mode=setup_status_snapshot.installation_mode,
        allowed_installation_modes=list(
            setup_status_snapshot.allowed_installation_modes
        ),
        owner_required=setup_state in {
            RuntimeSetupState.NEW,
            RuntimeSetupState.MIGRATING,
            RuntimeSetupState.AWAITING_OWNER,
        },
        mutations_open=setup_state
        not in {RuntimeSetupState.READY, RuntimeSetupState.BLOCKED},
        external_toolchain=(
            ExternalToolchainStatusResponse(
                kind=external_toolchain.kind,
                state=external_toolchain.state,
                official_url=external_toolchain.official_url,
                restart_required=external_toolchain.restart_required,
                components=ExternalToolchainComponentsResponse(
                    mactex=external_toolchain.components.mactex,
                    ghostscript=external_toolchain.components.ghostscript,
                    ghostscript_extras=(
                        external_toolchain.components.ghostscript_extras
                    ),
                ),
            )
            if external_toolchain is not None
            else None
        ),
    )


@router.post("/owner", response_model=LocalOwnerSetupResponse)
def create_or_claim_local_owner(
    payload: LocalOwnerSetupRequest,
    request: Request,
) -> LocalOwnerSetupResponse:
    credential_service: CredentialService | None = None
    credential_write: CredentialWrite | None = None
    credential_binding: str | None = None
    try:
        if payload.llm_credential is not None:
            credential_service = getattr(request.app.state, "credential_service", None)
            if credential_service is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Protected credential storage is not available in this runtime.",
                )
            credential_write, credential_binding = _credential_write(payload.llm_credential)
        result: LocalOwnerSetupResult = request.app.state.local_owner_setup_service.submit(
            LocalOwnerCredentials(
                bootstrap_token=payload.bootstrap_token.get_secret_value(),
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password.get_secret_value(),
                installation_mode=payload.installation_mode,
                workspace_name=payload.workspace_name,
                expected_state_version=payload.expected_state_version,
                credential_request_binding=credential_binding,
            )
        )
    except BootstrapTokenInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The setup proof is invalid.",
        ) from error
    except BootstrapTokenExpiredError as error:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="The setup proof has expired. Request a new proof from the launcher.",
        ) from error
    except BootstrapTokenReplayError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The setup proof was already used by a different request.",
        ) from error
    except (LocalActiveUserLimitError, LocalOwnerSetupConflictError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The local owner could not be provisioned in the current state.",
        ) from error
    except SetupTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Owner setup mutations are closed in the current state.",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Owner setup input is invalid.",
        ) from error

    credential_metadata: CredentialMetadata | None = None
    if credential_service is not None and credential_write is not None:
        try:
            credential_metadata = credential_service.upsert_default_for_user(
                result.owner_user_id,
                credential_write,
            )
        except CredentialApplicationError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The owner was secured, but protected credential storage needs recovery.",
            ) from error

    # Owner/mode/workspace are committed first and credential persistence is an idempotent setup
    # saga.  Only this final compare-and-swap opens runtime verification, so the independent
    # container verifier can never mark the installation READY while an optional credential write
    # is incomplete.  A transient credential/store failure leaves CONFIGURING and the identical
    # one-time request can be replayed safely.
    final_state = result.setup_state
    final_version = result.state_version
    if final_state in {
        RuntimeSetupState.CONFIGURING,
        RuntimeSetupState.RECOVERABLE_FAILURE,
    }:
        try:
            transitioned = request.app.state.setup_state_store.transition(
                expected=SetupStateSnapshot(
                    state=final_state,
                    state_version=final_version,
                ),
                target=RuntimeSetupState.VERIFYING_RUNTIME,
                now=datetime.now(UTC),
            )
        except InstallationStateVersionConflict as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The installation state changed while finalizing setup.",
            ) from error
        except SetupTransitionError as error:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail="Runtime verification cannot start from the current setup state.",
            ) from error
        final_state = transitioned.state
        final_version = transitioned.state_version

    return LocalOwnerSetupResponse(
        setup_state=final_state,
        state_version=final_version,
        installation_mode=result.installation_mode,
        replayed=result.replayed,
        llm_credential=(
            _setup_credential_response(credential_metadata)
            if credential_metadata is not None
            else None
        ),
    )
