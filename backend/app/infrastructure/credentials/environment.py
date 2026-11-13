from __future__ import annotations

import hmac

from app.domain.credentials import (
    SecretKeyMaterial,
    SecretKeyNotFoundError,
    SecretStoreAvailability,
    SecretStoreState,
)


class EnvironmentSecretStore:
    """Read-only deployment master key supplied through explicit Settings.

    The adapter intentionally does not read ``os.environ`` itself.  API and worker receive the
    same validated Settings values at their composition root, while domain/application code sees
    only the ``SecretStore`` port.  A single immutable version avoids silently replacing the root
    key for ciphertext already stored in the database.
    """

    def __init__(
        self,
        key_name: str,
        key_material: SecretKeyMaterial,
        *,
        key_version: int = 1,
    ) -> None:
        if not key_name.strip():
            raise ValueError("Environment secret-store key name is required.")
        if key_version < 1:
            raise ValueError("Environment secret-store key version must be positive.")
        if len(key_material.reveal()) != 32:
            raise ValueError("Environment credential master key must contain exactly 256 bits.")
        self._key_name = key_name
        self._key_material = key_material
        self._key_version = key_version

    def __repr__(self) -> str:
        return (
            "EnvironmentSecretStore("
            f"key_name={self._key_name!r}, key_version={self._key_version!r}, "
            "key_material=<redacted>)"
        )

    def availability(self) -> SecretStoreAvailability:
        return SecretStoreAvailability(state=SecretStoreState.AVAILABLE)

    def active_key_version(self, key_name: str) -> int | None:
        return self._key_version if key_name == self._key_name else None

    def get(self, key_name: str, key_version: int) -> SecretKeyMaterial:
        if key_name != self._key_name or key_version != self._key_version:
            raise SecretKeyNotFoundError
        return self._key_material

    def put(
        self,
        key_name: str,
        key_version: int,
        key_material: SecretKeyMaterial,
        *,
        make_active: bool = False,
    ) -> None:
        """Accept an identical idempotent assertion; reject rotation through process memory."""

        if (
            key_name == self._key_name
            and key_version == self._key_version
            and hmac.compare_digest(
                key_material.reveal(),
                self._key_material.reveal(),
            )
        ):
            return
        raise ValueError(
            "Environment-backed credential keys are immutable; change deployment configuration."
        )

    def delete(self, key_name: str, key_version: int) -> None:
        del key_name, key_version
        raise ValueError(
            "Environment-backed credential keys are immutable; change deployment configuration."
        )
