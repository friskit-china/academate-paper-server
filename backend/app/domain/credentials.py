from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class SecretStoreState(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class SecretStoreAvailability:
    state: SecretStoreState
    reason_code: str | None = None

    @property
    def available(self) -> bool:
        return self.state is SecretStoreState.AVAILABLE


@dataclass(frozen=True, slots=True)
class SecretKeyMaterial:
    """Opaque key bytes whose string representations are always redacted."""

    _value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self._value) < 32:
            raise ValueError("Secret key material must contain at least 256 bits.")

    def reveal(self) -> bytes:
        return self._value

    def __str__(self) -> str:
        return "<redacted-secret-key>"


@dataclass(frozen=True, slots=True)
class SensitiveText:
    """A short-lived plaintext value that cannot leak through repr/str."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if len(self._value) < 8:
            raise ValueError("Credential secrets must contain at least 8 characters.")
        if len(self._value) > 8192:
            raise ValueError("Credential secrets must not exceed 8192 characters.")

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return "<redacted-sensitive-text>"


@dataclass(frozen=True, slots=True)
class CipherEnvelope:
    algorithm: str
    key_version: int
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.algorithm.strip():
            raise ValueError("Cipher envelope algorithm is required.")
        if self.key_version < 1:
            raise ValueError("Cipher envelope key version must be positive.")
        if not self.nonce:
            raise ValueError("Cipher envelope nonce is required.")
        if not self.ciphertext:
            raise ValueError("Cipher envelope ciphertext is required.")

    def __repr__(self) -> str:
        return (
            "CipherEnvelope("
            f"algorithm={self.algorithm!r}, key_version={self.key_version!r}, "
            "nonce=<redacted>, ciphertext=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    credential_id: str
    user_id: str
    provider: str
    api_base: str | None
    model: str | None

    def authenticated_data(self) -> bytes:
        """Canonical AAD binds ciphertext to ownership and non-secret provider metadata."""

        return json.dumps(
            {
                "api_base": self.api_base,
                "credential_id": self.credential_id,
                "model": self.model,
                "provider": self.provider,
                "user_id": self.user_id,
                "version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    id: str
    user_id: str
    provider: str
    api_base: str | None
    model: str | None
    envelope: CipherEnvelope
    created_at: datetime
    updated_at: datetime

    @property
    def binding(self) -> CredentialBinding:
        return CredentialBinding(
            credential_id=self.id,
            user_id=self.user_id,
            provider=self.provider,
            api_base=self.api_base,
            model=self.model,
        )


class SecretStoreError(RuntimeError):
    error_code = "SECRET_STORE_ERROR"


class SecretStoreUnavailableError(SecretStoreError):
    error_code = "SECRET_STORE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("The protected secret store is unavailable.")


class SecretKeyNotFoundError(SecretStoreError):
    error_code = "SECRET_KEY_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("The requested protected key version is unavailable.")


class CipherError(RuntimeError):
    error_code = "CIPHER_ERROR"


class CipherUnavailableError(CipherError):
    error_code = "CIPHER_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("The authenticated credential cipher is unavailable.")


class CipherAuthenticationError(CipherError):
    error_code = "CIPHER_AUTHENTICATION_FAILED"

    def __init__(self) -> None:
        super().__init__("The encrypted credential could not be authenticated.")


class SecretStore(Protocol):
    """Versioned master-key storage implemented by platform or deployment adapters."""

    def availability(self) -> SecretStoreAvailability: ...

    def active_key_version(self, key_name: str) -> int | None: ...

    def get(self, key_name: str, key_version: int) -> SecretKeyMaterial: ...

    def put(
        self,
        key_name: str,
        key_version: int,
        key_material: SecretKeyMaterial,
        *,
        make_active: bool = False,
    ) -> None: ...

    def delete(self, key_name: str, key_version: int) -> None: ...


class AuthenticatedCipher(Protocol):
    """AEAD seam; production adapters must authenticate ciphertext and AAD."""

    @property
    def algorithm(self) -> str: ...

    def encrypt(
        self,
        plaintext: SensitiveText,
        *,
        key: SecretKeyMaterial,
        key_version: int,
        authenticated_data: bytes,
    ) -> CipherEnvelope: ...

    def decrypt(
        self,
        envelope: CipherEnvelope,
        *,
        key: SecretKeyMaterial,
        authenticated_data: bytes,
    ) -> SensitiveText: ...


class DefaultCredentialFactory(Protocol):
    def __call__(
        self,
        existing: EncryptedCredential | None,
    ) -> EncryptedCredential: ...


class CredentialRepository(Protocol):
    def list_for_user(self, user_id: str) -> list[EncryptedCredential]: ...

    def get_for_user(self, credential_id: str, user_id: str) -> EncryptedCredential | None: ...

    def get_for_active_workspace_user(
        self,
        credential_id: str,
        user_id: str,
        workspace_id: str,
    ) -> EncryptedCredential | None: ...

    def save(self, credential: EncryptedCredential) -> EncryptedCredential: ...

    def upsert_default_for_user(
        self,
        user_id: str,
        factory: DefaultCredentialFactory,
    ) -> EncryptedCredential: ...

    def delete_for_user(self, credential_id: str, user_id: str) -> bool: ...
