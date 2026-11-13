from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from app.domain.runtime import IdentityMode, RuntimeEdition, RuntimeNetworkScope, RuntimeProfile
from app.domain.setup import InstallationMode

RUNTIME_CAPABILITY_SCHEMA_VERSION = 4
APPLICATION_VERSION = "0.1.6"
API_VERSION = "v1"
DATABASE_SCHEMA_REVISION = "20261111_0033"


class RuntimeSetupState(StrEnum):
    NEW = "NEW"
    MIGRATING = "MIGRATING"
    AWAITING_OWNER = "AWAITING_OWNER"
    CONFIGURING = "CONFIGURING"
    VERIFYING_RUNTIME = "VERIFYING_RUNTIME"
    READY = "READY"
    RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"
    BLOCKED = "BLOCKED"


class RuntimeSetupStateProvider(Protocol):
    def get_setup_state(self) -> RuntimeSetupState: ...


class RuntimeInstallationModeProvider(Protocol):
    """Server-owned source for the write-once first-run product mode."""

    def get_installation_mode(self) -> InstallationMode | None: ...


@dataclass(frozen=True, slots=True)
class PreInstallationTableCompatibilitySetupStateProvider:
    """Temporary server-owned state until installation_state becomes authoritative.

    The provider deliberately accepts no request, query, header, or browser state.  Replacing it
    with the installation repository only requires implementing ``RuntimeSetupStateProvider``.
    """

    def get_setup_state(self) -> RuntimeSetupState:
        return RuntimeSetupState.READY


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityFlags:
    auth_ui: bool
    automatic_owner_session: bool
    registration: bool
    invitations: bool
    user_management: bool
    group_management: bool
    workspace_switching: bool
    system_status: bool
    desktop_supervisor: bool


_LOCAL_CAPABILITIES = RuntimeCapabilityFlags(
    auth_ui=False,
    automatic_owner_session=False,
    registration=False,
    invitations=False,
    user_management=False,
    group_management=False,
    workspace_switching=False,
    # Capability flags describe callable product behavior, not the target architecture.
    # These remain off until M5-20..23/26 have a runnable composition and status surface.
    system_status=False,
    desktop_supervisor=False,
)

_SERVER_CAPABILITIES = RuntimeCapabilityFlags(
    # Authenticated Server login/session routes are present in both installation modes. Account
    # administration is enabled below only for persisted MULTI_USER installations.
    auth_ui=True,
    automatic_owner_session=False,
    registration=False,
    invitations=False,
    user_management=False,
    group_management=False,
    workspace_switching=False,
    system_status=False,
    desktop_supervisor=False,
)


def capability_flags_for(
    profile: RuntimeProfile,
    installation_mode: InstallationMode | None = None,
    *,
    setup_state: RuntimeSetupState = RuntimeSetupState.READY,
    automatic_owner_session_enabled: bool = False,
) -> RuntimeCapabilityFlags:
    """Return the centralized, server-issued UI contract for persisted runtime state.

    Server login/session is available once setup has persisted a mode. Keeping
    ``installation_mode`` in this application-layer decision point ensures that enabling account
    administration depends on the database provider, never a browser field or
    architecture/environment shortcut.
    """

    if profile.edition is RuntimeEdition.LOCAL:
        return replace(
            _LOCAL_CAPABILITIES,
            desktop_supervisor=profile.network_scope is RuntimeNetworkScope.LOOPBACK,
        )
    if installation_mode is None:
        # Before the setup transaction commits a mode, the bootstrap wizard is the only intended
        # public surface.  Do not advertise the ordinary authenticated application prematurely.
        return replace(_SERVER_CAPABILITIES, auth_ui=False)
    if installation_mode is InstallationMode.SINGLE_USER:
        return replace(
            _SERVER_CAPABILITIES,
            automatic_owner_session=(
                automatic_owner_session_enabled
                and setup_state is RuntimeSetupState.READY
                and profile.identity_mode is IdentityMode.AUTHENTICATED
                and profile.network_scope is RuntimeNetworkScope.PRIVATE_COMPOSE
            ),
        )
    if installation_mode is InstallationMode.MULTI_USER:
        multi_user_ready = setup_state is RuntimeSetupState.READY
        return replace(
            _SERVER_CAPABILITIES,
            registration=multi_user_ready,
            invitations=multi_user_ready,
            user_management=multi_user_ready,
        )
    raise AssertionError("installation mode must use the fixed domain enum")
