from __future__ import annotations

import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.credentials import MASTER_KEY_NAME
from app.domain.credentials import (
    AuthenticatedCipher,
    SecretKeyMaterial,
    SecretKeyNotFoundError,
    SecretStore,
    SecretStoreUnavailableError,
)
from app.domain.runtime import (
    RuntimeEdition,
    RuntimeNetworkScope,
    RuntimeProfile,
    SecretStoreProfile,
)
from app.infrastructure.credentials.aead import InstallationScopedAesGcmCipher
from app.infrastructure.credentials.environment import EnvironmentSecretStore
from app.infrastructure.credentials.macos_keychain import (
    KeychainCommandRunner,
    MacOSKeychainSecretStore,
)


class CredentialAdapterError(RuntimeError):
    """Safe startup error for an explicit but unusable credential adapter selection."""


@dataclass(frozen=True, slots=True)
class CredentialAdapterBundle:
    secret_store: SecretStore
    cipher: AuthenticatedCipher


class CredentialAdapterFactory(Protocol):
    def __call__(
        self,
        *,
        runtime_profile: RuntimeProfile,
        os_secret_store_mode: str,
        local_installation_id: UUID | None,
        credential_installation_id: UUID | None,
        credential_master_key: SecretKeyMaterial | None,
    ) -> CredentialAdapterBundle | None: ...


def macos_credential_child_environment(installation_id: UUID) -> dict[str, str]:
    """Non-secret settings a trusted macOS Supervisor injects into API and worker."""

    if not isinstance(installation_id, UUID):
        raise ValueError("macOS credential composition requires an installation UUID.")
    return {
        "LOCAL_INSTALLATION_ID": str(installation_id),
        "OS_SECRET_STORE_MODE": "native",
        "RUNTIME_NETWORK_SCOPE": RuntimeNetworkScope.LOOPBACK.value,
        "SECRET_STORE_PROFILE": SecretStoreProfile.OS_KEYRING.value,
    }


def build_credential_adapters(
    *,
    runtime_profile: RuntimeProfile,
    os_secret_store_mode: str,
    local_installation_id: UUID | None,
    credential_installation_id: UUID | None = None,
    credential_master_key: SecretKeyMaterial | None = None,
    platform_name: str = sys.platform,
    command_runner: KeychainCommandRunner | None = None,
    key_material_factory: Callable[[int], bytes] = secrets.token_bytes,
) -> CredentialAdapterBundle | None:
    if runtime_profile.secret_store is SecretStoreProfile.ENV_OR_KMS:
        configured_values = (
            credential_installation_id is not None,
            credential_master_key is not None,
        )
        if not any(configured_values):
            return None
        if not all(configured_values):
            raise CredentialAdapterError(
                "ENV_OR_KMS credential storage requires both "
                "CREDENTIAL_INSTALLATION_ID and CREDENTIAL_MASTER_KEY_B64."
            )
        if runtime_profile.edition is not RuntimeEdition.SERVER:
            raise CredentialAdapterError(
                "Environment-backed credential storage requires Server Edition."
            )
        if os_secret_store_mode != "unavailable":
            raise CredentialAdapterError(
                "ENV_OR_KMS credential storage cannot compose a native OS adapter."
            )
        if (
            not isinstance(credential_installation_id, UUID)
            or credential_installation_id.int == 0
        ):
            raise CredentialAdapterError(
                "CREDENTIAL_INSTALLATION_ID must be a non-nil UUID."
            )
        if not isinstance(credential_master_key, SecretKeyMaterial):
            raise CredentialAdapterError(
                "CREDENTIAL_MASTER_KEY_B64 did not produce protected key material."
            )
        try:
            secret_store = EnvironmentSecretStore(
                MASTER_KEY_NAME,
                credential_master_key,
            )
        except ValueError as error:
            raise CredentialAdapterError(
                "The environment credential master key is invalid."
            ) from error
        return CredentialAdapterBundle(
            secret_store=secret_store,
            cipher=InstallationScopedAesGcmCipher(credential_installation_id),
        )

    if credential_installation_id is not None or credential_master_key is not None:
        raise CredentialAdapterError(
            "Container credential settings require SECRET_STORE_PROFILE=ENV_OR_KMS."
        )
    if os_secret_store_mode == "unavailable":
        return None
    if os_secret_store_mode != "native":
        raise CredentialAdapterError("Unknown OS secret-store adapter mode.")
    if (
        runtime_profile.edition is not RuntimeEdition.LOCAL
        or runtime_profile.secret_store is not SecretStoreProfile.OS_KEYRING
        or runtime_profile.network_scope is not RuntimeNetworkScope.LOOPBACK
    ):
        raise CredentialAdapterError(
            "The native OS secret store requires a loopback Local OS_KEYRING profile."
        )
    if local_installation_id is None:
        raise CredentialAdapterError(
            "The native OS secret store requires a stable installation identity."
        )
    if platform_name != "darwin":
        raise CredentialAdapterError(
            f"The native OS secret-store adapter is not implemented on {platform_name}."
        )

    try:
        secret_store = MacOSKeychainSecretStore(
            local_installation_id,
            runner=command_runner,
        )
    except (OSError, ValueError) as error:
        raise CredentialAdapterError(
            "The macOS Keychain process environment is unavailable."
        ) from error
    availability = secret_store.availability()
    if not availability.available:
        raise CredentialAdapterError(
            f"The macOS Keychain is unavailable ({availability.reason_code})."
        )
    try:
        active_version = secret_store.active_key_version(MASTER_KEY_NAME)
        if active_version is None:
            raw_key = key_material_factory(32)
            if not isinstance(raw_key, bytes) or len(raw_key) != 32:
                raise CredentialAdapterError(
                    "The credential master-key generator returned invalid key material."
                )
            # API and worker are sibling packaged processes and may initialize together. The
            # immutable version account elects one key; the loser adopts it rather than replacing
            # ciphertext's root key.
            secret_store.ensure(
                MASTER_KEY_NAME,
                1,
                SecretKeyMaterial(raw_key),
                make_active=True,
            )
        else:
            # Detect partially lost/corrupt Keychain state during startup rather than accepting
            # tasks and failing only after provider work begins.
            secret_store.get(MASTER_KEY_NAME, active_version)
    except SecretKeyNotFoundError as error:
        raise CredentialAdapterError(
            "The macOS Keychain metadata references a missing credential master key."
        ) from error
    except SecretStoreUnavailableError as error:
        raise CredentialAdapterError("The macOS Keychain became unavailable.") from error
    except ValueError as error:
        raise CredentialAdapterError(
            "The macOS Keychain credential namespace is inconsistent."
        ) from error

    return CredentialAdapterBundle(
        secret_store=secret_store,
        cipher=InstallationScopedAesGcmCipher(local_installation_id),
    )
