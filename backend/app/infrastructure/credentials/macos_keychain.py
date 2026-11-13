from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.domain.credentials import (
    SecretKeyMaterial,
    SecretKeyNotFoundError,
    SecretStoreAvailability,
    SecretStoreState,
    SecretStoreUnavailableError,
)

KEYCHAIN_SERVICE = "com.academatepaper.local.credentials.v1"
KEYCHAIN_SMOKE_SERVICE = "com.academatepaper.local.credentials.smoke.v1"
SECURITY_EXECUTABLE = "/usr/bin/security"
_ITEM_NOT_FOUND_EXIT_CODE = 44
_DUPLICATE_ITEM_EXIT_CODE = 45
_MAX_ACCOUNT_VALUE_BYTES = 16_384
_KEY_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class KeychainCommandResult:
    returncode: int
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)


class KeychainCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_data: bytes | None = None,
    ) -> KeychainCommandResult: ...


class SubprocessKeychainCommandRunner:
    """Run Apple's fixed-path security tool without putting protected values in argv/env."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._environment = dict(
            _controlled_keychain_environment() if environment is None else environment
        )

    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_data: bytes | None = None,
    ) -> KeychainCommandResult:
        if not argv or argv[0] != SECURITY_EXECUTABLE:
            raise ValueError("Keychain runner accepts only the fixed macOS security executable.")
        completed = subprocess.run(  # noqa: S603 - fixed executable and validated adapter args
            argv,
            input=input_data,
            capture_output=True,
            check=False,
            close_fds=True,
            env=self._environment,
            # Packaged API/worker processes already run without a controlling terminal. Keeping
            # this fixed-path helper in their process group lets the Supervisor contain it during
            # crash or Quit cleanup instead of creating an unowned descendant session.
            start_new_session=False,
            timeout=self._timeout_seconds,
        )
        return KeychainCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class _KeyMetadata:
    active: int | None
    versions: tuple[int, ...]


class MacOSKeychainSecretStore:
    """Versioned master keys stored as generic-password items in the login Keychain.

    The service is constant per application. Accounts are namespaced by the stable installation
    UUID, logical key name and key version. Protected values are base64/JSON encoded and supplied
    only through the child's stdin; neither keys nor metadata values appear in argv or exceptions.
    """

    def __init__(
        self,
        installation_id: UUID,
        *,
        runner: KeychainCommandRunner | None = None,
        service: str = KEYCHAIN_SERVICE,
    ) -> None:
        if not _KEY_NAME_PATTERN.fullmatch(service):
            raise ValueError("Keychain service must use a stable application identifier.")
        self._installation_id = installation_id
        self._runner = runner or SubprocessKeychainCommandRunner()
        self._service = service

    def __repr__(self) -> str:
        return (
            "MacOSKeychainSecretStore("
            f"service={self._service!r}, installation_id={str(self._installation_id)!r})"
        )

    def availability(self) -> SecretStoreAvailability:
        try:
            result = self._runner.run(
                (SECURITY_EXECUTABLE, "default-keychain", "-d", "user")
            )
        except (OSError, subprocess.SubprocessError):
            return SecretStoreAvailability(
                state=SecretStoreState.UNAVAILABLE,
                reason_code="MACOS_KEYCHAIN_COMMAND_UNAVAILABLE",
            )
        if result.returncode != 0:
            return SecretStoreAvailability(
                state=SecretStoreState.UNAVAILABLE,
                reason_code="MACOS_KEYCHAIN_UNAVAILABLE",
            )
        return SecretStoreAvailability(state=SecretStoreState.AVAILABLE)

    def active_key_version(self, key_name: str) -> int | None:
        return self._load_metadata(key_name).active

    def get(self, key_name: str, key_version: int) -> SecretKeyMaterial:
        self._validate_key_reference(key_name, key_version)
        encoded = self._read_account(self._version_account(key_name, key_version))
        if encoded is None:
            raise SecretKeyNotFoundError
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise SecretKeyNotFoundError from error
        if len(decoded) != 32:
            raise SecretKeyNotFoundError
        return SecretKeyMaterial(decoded)

    def put(
        self,
        key_name: str,
        key_version: int,
        key_material: SecretKeyMaterial,
        *,
        make_active: bool = False,
    ) -> None:
        self._validate_key_reference(key_name, key_version)
        raw_key = key_material.reveal()
        if len(raw_key) != 32:
            raise ValueError("The macOS credential master key must contain exactly 256 bits.")
        installed = self.ensure(
            key_name,
            key_version,
            key_material,
            make_active=make_active,
        )
        if not hmac.compare_digest(installed.reveal(), raw_key):
            raise ValueError("A different protected key already exists at this key version.")

    def ensure(
        self,
        key_name: str,
        key_version: int,
        candidate: SecretKeyMaterial,
        *,
        make_active: bool = False,
    ) -> SecretKeyMaterial:
        """Atomically create a version or adopt the winner of a concurrent first start."""

        self._validate_key_reference(key_name, key_version)
        raw_key = candidate.reveal()
        if len(raw_key) != 32:
            raise ValueError("The macOS credential master key must contain exactly 256 bits.")
        account = self._version_account(key_name, key_version)
        encoded = base64.b64encode(raw_key)
        existing = self._read_account(account)
        if existing is None:
            created = self._create_account(account, encoded)
            if not created:
                # API and worker can start together. A duplicate means the other trusted process
                # won the immutable version-account insert; adopt that exact stored key.
                existing = self._read_account(account)
                if existing is None:
                    raise SecretStoreUnavailableError
            else:
                existing = encoded
        try:
            installed = base64.b64decode(existing, validate=True)
        except (binascii.Error, ValueError) as error:
            raise SecretStoreUnavailableError from error
        if len(installed) != 32:
            raise SecretStoreUnavailableError

        # Reload after the immutable insert so a concurrent creator's metadata is not ignored.
        metadata = self._load_metadata(key_name)
        versions = tuple(sorted({*metadata.versions, key_version}))
        active = key_version if make_active or metadata.active is None else metadata.active
        self._write_metadata(key_name, _KeyMetadata(active=active, versions=versions))
        return SecretKeyMaterial(installed)

    def delete(self, key_name: str, key_version: int) -> None:
        self._validate_key_reference(key_name, key_version)
        metadata = self._load_metadata(key_name)
        self._delete_account(self._version_account(key_name, key_version))
        versions = tuple(version for version in metadata.versions if version != key_version)
        if not versions:
            self._delete_account(self._metadata_account(key_name))
            return
        active = metadata.active
        if active == key_version or active not in versions:
            active = max(versions)
        self._write_metadata(key_name, _KeyMetadata(active=active, versions=versions))

    def _load_metadata(self, key_name: str) -> _KeyMetadata:
        self._validate_key_name(key_name)
        raw = self._read_account(self._metadata_account(key_name))
        if raw is None:
            return _KeyMetadata(active=None, versions=())
        if len(raw) > _MAX_ACCOUNT_VALUE_BYTES:
            raise SecretStoreUnavailableError
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SecretStoreUnavailableError from error
        if not isinstance(payload, dict) or set(payload) != {"active", "schema", "versions"}:
            raise SecretStoreUnavailableError
        versions_value = payload["versions"]
        active = payload["active"]
        if payload["schema"] != 1 or not isinstance(versions_value, list):
            raise SecretStoreUnavailableError
        if any(type(version) is not int or version < 1 for version in versions_value):
            raise SecretStoreUnavailableError
        versions = tuple(sorted(set(versions_value)))
        if len(versions) != len(versions_value):
            raise SecretStoreUnavailableError
        if active is not None and (type(active) is not int or active not in versions):
            raise SecretStoreUnavailableError
        return _KeyMetadata(active=active, versions=versions)

    def _write_metadata(self, key_name: str, metadata: _KeyMetadata) -> None:
        payload = json.dumps(
            {"active": metadata.active, "schema": 1, "versions": list(metadata.versions)},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        self._write_account(self._metadata_account(key_name), payload)

    def _read_account(self, account: str) -> bytes | None:
        result = self._run(
            (
                SECURITY_EXECUTABLE,
                "find-generic-password",
                "-s",
                self._service,
                "-a",
                account,
                "-w",
            )
        )
        if result.returncode == _ITEM_NOT_FOUND_EXIT_CODE:
            return None
        if result.returncode != 0:
            raise SecretStoreUnavailableError
        value = result.stdout.rstrip(b"\r\n")
        if not value or len(value) > _MAX_ACCOUNT_VALUE_BYTES or b"\n" in value or b"\r" in value:
            raise SecretStoreUnavailableError
        return value

    def _write_account(self, account: str, value: bytes) -> None:
        result = self._add_account(account, value, update=True)
        if result.returncode != 0:
            raise SecretStoreUnavailableError

    def _create_account(self, account: str, value: bytes) -> bool:
        result = self._add_account(account, value, update=False)
        if result.returncode == _DUPLICATE_ITEM_EXIT_CODE:
            return False
        if result.returncode != 0:
            raise SecretStoreUnavailableError
        return True

    def _add_account(
        self,
        account: str,
        value: bytes,
        *,
        update: bool,
    ) -> KeychainCommandResult:
        if not value or len(value) > _MAX_ACCOUNT_VALUE_BYTES or b"\n" in value or b"\r" in value:
            raise ValueError("Keychain values must be bounded single-line encoded values.")
        # Apple's security help explicitly marks a password passed as a -w argument insecure. A
        # trailing flag prompts on the runner-supplied stdin without relying on a controlling TTY.
        argv = [
            SECURITY_EXECUTABLE,
            "add-generic-password",
            "-s",
            self._service,
            "-a",
            account,
            "-D",
            "AcadematePaper protected credential key",
            "-w",
        ]
        if update:
            argv.insert(2, "-U")
        return self._run(
            tuple(argv),
            # With ``-w`` and no argv value, Apple's security CLI reads a new generic-password
            # value twice (value + confirmation). Supplying only one line makes the real command
            # fail with ``passwords don't match`` even though fake runners may accept it. Keep the
            # protected value out of argv/environment while satisfying that native stdin protocol.
            input_data=value + b"\n" + value + b"\n",
        )

    def _delete_account(self, account: str) -> None:
        result = self._run(
            (
                SECURITY_EXECUTABLE,
                "delete-generic-password",
                "-s",
                self._service,
                "-a",
                account,
            )
        )
        if result.returncode not in {0, _ITEM_NOT_FOUND_EXIT_CODE}:
            raise SecretStoreUnavailableError

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        input_data: bytes | None = None,
    ) -> KeychainCommandResult:
        try:
            return self._runner.run(argv, input_data=input_data)
        except (OSError, subprocess.SubprocessError) as error:
            raise SecretStoreUnavailableError from error

    def _metadata_account(self, key_name: str) -> str:
        return f"installation:{self._installation_id}:key:{key_name}:metadata"

    def _version_account(self, key_name: str, key_version: int) -> str:
        return f"installation:{self._installation_id}:key:{key_name}:version:{key_version}"

    @staticmethod
    def _validate_key_name(key_name: str) -> None:
        if not _KEY_NAME_PATTERN.fullmatch(key_name):
            raise ValueError("Secret key name must use the versioned application-key namespace.")

    @classmethod
    def _validate_key_reference(cls, key_name: str, key_version: int) -> None:
        cls._validate_key_name(key_name)
        if type(key_version) is not int or key_version < 1:
            raise ValueError("Secret key version must be positive.")


def _controlled_keychain_environment() -> dict[str, str]:
    required = ("HOME", "USER", "LOGNAME")
    missing = [
        name for name in required if not os.environ.get(name) or "\x00" in os.environ[name]
    ]
    if missing:
        raise ValueError(
            "The macOS Keychain adapter requires trusted HOME, USER, and LOGNAME values."
        )
    allowed = (*required, "TMPDIR", "LANG", "LC_ALL")
    environment = {
        name: value
        for name in allowed
        if (value := os.environ.get(name)) is not None and "\x00" not in value
    }
    environment["PATH"] = "/usr/bin:/bin"
    return environment


def run_macos_keychain_smoke(
    installation_id: UUID,
    *,
    platform_name: str,
    runner: KeychainCommandRunner | None = None,
) -> None:
    """Explicit destructive probe for a native runner; callers must opt in and own cleanup."""

    if platform_name != "darwin":
        raise RuntimeError("The real Keychain smoke is supported only on native macOS.")
    smoke_key_name = "papermanager.native-keychain-smoke"
    material = SecretKeyMaterial(os.urandom(32))
    store = MacOSKeychainSecretStore(
        installation_id,
        runner=runner,
        service=KEYCHAIN_SMOKE_SERVICE,
    )
    if not store.availability().available:
        raise SecretStoreUnavailableError
    try:
        store.put(smoke_key_name, 1, material, make_active=True)
        if not hmac.compare_digest(store.get(smoke_key_name, 1).reveal(), material.reveal()):
            raise SecretStoreUnavailableError
    finally:
        store.delete(smoke_key_name, 1)
