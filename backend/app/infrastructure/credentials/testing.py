from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.credentials import (
    CipherAuthenticationError,
    CipherEnvelope,
    SecretKeyMaterial,
    SecretKeyNotFoundError,
    SecretStoreAvailability,
    SecretStoreState,
    SecretStoreUnavailableError,
    SensitiveText,
)


class InMemorySecretStore:
    """Deterministic contract fake; it never reads a host OS credential store."""

    def __init__(self) -> None:
        self._available = True
        self._keys: dict[tuple[str, int], SecretKeyMaterial] = {}
        self._active_versions: dict[str, int] = {}

    def availability(self) -> SecretStoreAvailability:
        return SecretStoreAvailability(
            state=(
                SecretStoreState.AVAILABLE
                if self._available
                else SecretStoreState.UNAVAILABLE
            ),
            reason_code=None if self._available else "TEST_STORE_DISABLED",
        )

    def active_key_version(self, key_name: str) -> int | None:
        self._require_available()
        return self._active_versions.get(key_name)

    def get(self, key_name: str, key_version: int) -> SecretKeyMaterial:
        self._require_available()
        try:
            return self._keys[(key_name, key_version)]
        except KeyError as error:
            raise SecretKeyNotFoundError from error

    def put(
        self,
        key_name: str,
        key_version: int,
        key_material: SecretKeyMaterial,
        *,
        make_active: bool = False,
    ) -> None:
        self._require_available()
        if key_version < 1:
            raise ValueError("Secret key version must be positive.")
        self._keys[(key_name, key_version)] = key_material
        if make_active or key_name not in self._active_versions:
            self._active_versions[key_name] = key_version

    def delete(self, key_name: str, key_version: int) -> None:
        self._require_available()
        self._keys.pop((key_name, key_version), None)
        if self._active_versions.get(key_name) == key_version:
            remaining = [version for name, version in self._keys if name == key_name]
            if remaining:
                self._active_versions[key_name] = max(remaining)
            else:
                self._active_versions.pop(key_name, None)

    def set_available(self, available: bool) -> None:
        self._available = available

    def lose_all(self) -> None:
        self._keys.clear()
        self._active_versions.clear()

    def _require_available(self) -> None:
        if not self._available:
            raise SecretStoreUnavailableError


@dataclass(frozen=True, slots=True)
class _FakeCipherRecord:
    plaintext: SensitiveText
    key: bytes = field(repr=False)
    key_version: int
    authenticated_data: bytes
    nonce: bytes


class InMemoryAuthenticatedCipher:
    """Opaque-reference test fake, intentionally not a cryptographic implementation.

    Plaintext stays only in this process' memory.  Persisted ciphertext is an opaque lookup token,
    so this fake can validate the AEAD contract without inventing a production cipher.
    """

    algorithm = "TEST-OPAQUE-REFERENCE-NOT-FOR-PRODUCTION"

    def __init__(self) -> None:
        self._counter = 0
        self._records: dict[bytes, _FakeCipherRecord] = {}

    def encrypt(
        self,
        plaintext: SensitiveText,
        *,
        key: SecretKeyMaterial,
        key_version: int,
        authenticated_data: bytes,
    ) -> CipherEnvelope:
        self._counter += 1
        ciphertext = f"test-envelope-{self._counter:016d}".encode()
        nonce = self._counter.to_bytes(12, "big")
        self._records[ciphertext] = _FakeCipherRecord(
            plaintext=plaintext,
            key=key.reveal(),
            key_version=key_version,
            authenticated_data=authenticated_data,
            nonce=nonce,
        )
        return CipherEnvelope(
            algorithm=self.algorithm,
            key_version=key_version,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    def decrypt(
        self,
        envelope: CipherEnvelope,
        *,
        key: SecretKeyMaterial,
        authenticated_data: bytes,
    ) -> SensitiveText:
        record = self._records.get(envelope.ciphertext)
        if (
            envelope.algorithm != self.algorithm
            or record is None
            or envelope.nonce != record.nonce
            or envelope.key_version != record.key_version
            or key.reveal() != record.key
            or authenticated_data != record.authenticated_data
        ):
            raise CipherAuthenticationError
        return record.plaintext

    def lose_all(self) -> None:
        self._records.clear()
