from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.domain.os_adapters import (
    ResolvedToolchain,
    ToolchainCapability,
    ToolchainCommands,
)

TOOLCHAIN_MANIFEST_SCHEMA_VERSION = 1
REQUIRED_TOOLCHAIN_COMMANDS = tuple(ToolchainCommands.__dataclass_fields__)
REQUIRED_TOOLCHAIN_CAPABILITIES = tuple(ToolchainCapability)


class ToolchainResolutionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


def _command_set(commands: dict[str, str]) -> ToolchainCommands:
    missing = sorted(set(REQUIRED_TOOLCHAIN_COMMANDS) - commands.keys())
    if missing:
        raise ToolchainResolutionError(
            "Toolchain is missing required commands: " + ", ".join(missing)
        )
    return ToolchainCommands(**{name: commands[name] for name in REQUIRED_TOOLCHAIN_COMMANDS})


def _read_key_value_manifest(path: Path) -> dict[str, str]:
    try:
        return dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )
    except OSError as error:
        raise ToolchainResolutionError(f"Cannot read toolchain profile manifest: {path}") from error


class ComposePathToolchainResolver:
    """Resolve the immutable command PATH baked into the Compose backend image.

    This is deliberately a composition-root adapter, not a fallback used by worker handlers.  A
    self-contained Local Edition must instead select ``ManagedManifestToolchainResolver``.
    """

    def __init__(
        self,
        *,
        profile_path: Path = Path("/opt/texlive/runtime-profile"),
        lookup: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._profile_path = profile_path
        self._lookup = lookup

    def resolve(self) -> ResolvedToolchain:
        profile_values = _read_key_value_manifest(self._profile_path)
        profile = profile_values.get("runtime_id", "").strip()
        version = profile_values.get("release", "").strip()
        if not profile or not version:
            raise ToolchainResolutionError(
                "Compose toolchain profile manifest requires runtime_id and release"
            )
        resolved: dict[str, str] = {}
        for name in REQUIRED_TOOLCHAIN_COMMANDS:
            candidate = self._lookup(name)
            if not candidate:
                raise ToolchainResolutionError(f"Compose image PATH cannot resolve {name}")
            path = Path(candidate)
            if not path.is_absolute():
                raise ToolchainResolutionError(
                    f"Compose image PATH resolved {name} to a non-absolute path"
                )
            # TeX selects a preloaded format from argv[0]. Preserve command symlink names such as
            # ``pdflatex -> pdftex`` while still returning an absolute path.
            resolved[name] = str(path.absolute())
        return ResolvedToolchain(
            resolver="compose-path",
            profile=profile,
            version=version,
            commands=_command_set(resolved),
            capabilities=REQUIRED_TOOLCHAIN_CAPABILITIES,
            managed_root=None,
            manifest_path=str(self._profile_path.resolve()),
        )


class ManagedManifestToolchainResolver:
    """Resolve commands only from one explicitly managed, versioned runtime root."""

    def __init__(
        self,
        *,
        managed_root: Path,
        manifest_path: Path,
        require_executable_bit: bool = True,
    ) -> None:
        self._managed_root = managed_root.resolve()
        self._manifest_path = manifest_path.resolve()
        self._require_executable_bit = require_executable_bit

    def resolve(self) -> ResolvedToolchain:
        if not self._manifest_path.is_relative_to(self._managed_root):
            raise ToolchainResolutionError("Toolchain manifest must be inside the managed root")
        try:
            contents = self._manifest_path.read_bytes()
            raw: Any = json.loads(contents)
        except (OSError, json.JSONDecodeError) as error:
            raise ToolchainResolutionError("Cannot read managed toolchain manifest") from error
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != TOOLCHAIN_MANIFEST_SCHEMA_VERSION
        ):
            raise ToolchainResolutionError(
                f"Managed toolchain manifest must use schema {TOOLCHAIN_MANIFEST_SCHEMA_VERSION}"
            )
        profile = raw.get("profile")
        version = raw.get("version")
        command_entries = raw.get("commands")
        capability_entries = raw.get("capabilities")
        if not isinstance(profile, str) or not profile.strip():
            raise ToolchainResolutionError("Managed toolchain manifest requires profile")
        if not isinstance(version, str) or not version.strip():
            raise ToolchainResolutionError("Managed toolchain manifest requires version")
        if not isinstance(command_entries, dict):
            raise ToolchainResolutionError("Managed toolchain manifest requires commands")
        if not isinstance(capability_entries, list) or not all(
            isinstance(value, str) for value in capability_entries
        ):
            raise ToolchainResolutionError("Managed toolchain manifest requires capabilities")

        resolved: dict[str, str] = {}
        for name in REQUIRED_TOOLCHAIN_COMMANDS:
            command_entry = command_entries.get(name)
            if not isinstance(command_entry, dict):
                raise ToolchainResolutionError(f"Managed toolchain manifest omits {name}")
            relative = command_entry.get("path")
            expected_sha256 = command_entry.get("sha256")
            if not isinstance(relative, str) or not relative.strip():
                raise ToolchainResolutionError(f"Managed toolchain manifest omits {name} path")
            if (
                not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
                or any(character not in "0123456789abcdef" for character in expected_sha256)
            ):
                raise ToolchainResolutionError(
                    f"Managed toolchain manifest requires a lowercase SHA-256 for {name}"
                )
            candidate = (self._managed_root / relative).absolute()
            resolved_candidate = candidate.resolve()
            if not resolved_candidate.is_relative_to(self._managed_root):
                raise ToolchainResolutionError(f"Managed toolchain command escapes root: {name}")
            if not resolved_candidate.is_file():
                raise ToolchainResolutionError(f"Managed toolchain command is missing: {name}")
            if self._require_executable_bit and resolved_candidate.stat().st_mode & 0o111 == 0:
                raise ToolchainResolutionError(
                    f"Managed toolchain command is not executable: {name}"
                )
            try:
                with resolved_candidate.open("rb") as executable:
                    actual_sha256 = hashlib.file_digest(executable, "sha256").hexdigest()
            except OSError as error:
                raise ToolchainResolutionError(
                    f"Cannot hash managed toolchain command: {name}"
                ) from error
            if not hmac.compare_digest(actual_sha256, expected_sha256):
                raise ToolchainResolutionError(
                    f"Managed toolchain command hash does not match manifest: {name}"
                )
            resolved[name] = str(candidate)

        try:
            capabilities = tuple(
                ToolchainCapability(value) for value in dict.fromkeys(capability_entries)
            )
        except ValueError as error:
            raise ToolchainResolutionError(
                f"Managed toolchain manifest declares an unknown capability: {error}"
            ) from error
        missing_capabilities = set(REQUIRED_TOOLCHAIN_CAPABILITIES) - set(capabilities)
        if missing_capabilities:
            raise ToolchainResolutionError(
                "Managed toolchain manifest is missing capabilities: "
                + ", ".join(sorted(capability.value for capability in missing_capabilities))
            )
        return ResolvedToolchain(
            resolver="managed-manifest",
            profile=profile.strip(),
            version=version.strip(),
            commands=_command_set(resolved),
            capabilities=capabilities,
            managed_root=str(self._managed_root),
            manifest_path=str(self._manifest_path),
            fingerprint="sha256:" + hashlib.sha256(contents).hexdigest(),
        )
