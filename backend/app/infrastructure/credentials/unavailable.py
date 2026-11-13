from app.domain.credentials import (
    CipherEnvelope,
    CipherUnavailableError,
    SecretKeyMaterial,
    SecretStoreAvailability,
    SecretStoreState,
    SecretStoreUnavailableError,
    SensitiveText,
)


class UnavailableAuthenticatedCipher:
    """Honest placeholder until a declared, packaged AEAD dependency is selected."""

    algorithm = "UNAVAILABLE"

    def encrypt(
        self,
        plaintext: SensitiveText,
        *,
        key: SecretKeyMaterial,
        key_version: int,
        authenticated_data: bytes,
    ) -> CipherEnvelope:
        del plaintext, key, key_version, authenticated_data
        raise CipherUnavailableError

    def decrypt(
        self,
        envelope: CipherEnvelope,
        *,
        key: SecretKeyMaterial,
        authenticated_data: bytes,
    ) -> SensitiveText:
        del envelope, key, authenticated_data
        raise CipherUnavailableError


class UnavailableSecretStore:
    """Runtime placeholder that never falls back to an unprotected file or environment value."""

    def availability(self) -> SecretStoreAvailability:
        return SecretStoreAvailability(
            state=SecretStoreState.UNAVAILABLE,
            reason_code="PLATFORM_ADAPTER_NOT_INSTALLED",
        )

    def active_key_version(self, key_name: str) -> int | None:
        del key_name
        raise SecretStoreUnavailableError

    def get(self, key_name: str, key_version: int) -> SecretKeyMaterial:
        del key_name, key_version
        raise SecretStoreUnavailableError

    def put(
        self,
        key_name: str,
        key_version: int,
        key_material: SecretKeyMaterial,
        *,
        make_active: bool = False,
    ) -> None:
        del key_name, key_version, key_material, make_active
        raise SecretStoreUnavailableError

    def delete(self, key_name: str, key_version: int) -> None:
        del key_name, key_version
        raise SecretStoreUnavailableError
