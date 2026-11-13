from __future__ import annotations

import secrets
from collections.abc import Callable
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.domain.credentials import (
    CipherAuthenticationError,
    CipherEnvelope,
    CipherUnavailableError,
    SecretKeyMaterial,
    SensitiveText,
)

_AAD_DOMAIN = b"AcadematePaper/CredentialEnvelope/AES-256-GCM/v1\0"
_AES_256_KEY_BYTES = 32
_GCM_NONCE_BYTES = 12


class InstallationScopedAesGcmCipher:
    """AES-256-GCM with an installation identity added to every credential AAD."""

    algorithm = "AES-256-GCM+INSTALLATION-AAD-v1"

    def __init__(
        self,
        installation_id: UUID,
        *,
        nonce_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._installation_id = installation_id
        self._nonce_factory = nonce_factory

    def encrypt(
        self,
        plaintext: SensitiveText,
        *,
        key: SecretKeyMaterial,
        key_version: int,
        authenticated_data: bytes,
    ) -> CipherEnvelope:
        raw_key = key.reveal()
        if len(raw_key) != _AES_256_KEY_BYTES:
            raise CipherUnavailableError
        nonce = self._nonce_factory(_GCM_NONCE_BYTES)
        if not isinstance(nonce, bytes) or len(nonce) != _GCM_NONCE_BYTES:
            raise CipherUnavailableError
        try:
            ciphertext = AESGCM(raw_key).encrypt(
                nonce,
                plaintext.reveal().encode("utf-8"),
                self._scoped_aad(authenticated_data),
            )
        except (OverflowError, ValueError) as error:
            raise CipherUnavailableError from error
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
        raw_key = key.reveal()
        if len(raw_key) != _AES_256_KEY_BYTES:
            raise CipherUnavailableError
        if envelope.algorithm != self.algorithm or len(envelope.nonce) != _GCM_NONCE_BYTES:
            raise CipherAuthenticationError
        try:
            plaintext = AESGCM(raw_key).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                self._scoped_aad(authenticated_data),
            )
            return SensitiveText(plaintext.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise CipherAuthenticationError from error

    def _scoped_aad(self, authenticated_data: bytes) -> bytes:
        # Length framing prevents a caller-controlled AAD suffix from colliding with the fixed
        # domain/installation prefix. The existing CredentialBinding AAD still binds user,
        # credential UUID and provider metadata inside this installation scope.
        return b"".join(
            (
                _AAD_DOMAIN,
                self._installation_id.bytes,
                len(authenticated_data).to_bytes(8, "big"),
                authenticated_data,
            )
        )
