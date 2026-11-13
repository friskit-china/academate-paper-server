from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.domain.os_adapters import (
    ExternalToolchainComponents,
    ExternalToolchainComponentState,
    ExternalToolchainKind,
    ExternalToolchainSetupStatus,
    ExternalToolchainState,
    ResolvedToolchain,
    ToolchainCapability,
    ToolchainCommands,
)
from app.infrastructure.platform.toolchain import (
    REQUIRED_TOOLCHAIN_CAPABILITIES,
    ToolchainResolutionError,
)

MACOS_EXTERNAL_TOOLCHAIN_MANIFEST_SCHEMA_VERSION = 2
MACOS_EXTERNAL_TOOLCHAIN_PROFILE = "academatepaper-macos-external"
MACTEX_OFFICIAL_URL = "https://www.tug.org/mactex/"
DEFAULT_MACTEX_TEX_BIN_ROOT = Path("/Library/TeX/texbin")
DEFAULT_MACTEX_USR_LOCAL_ROOT = Path("/usr/local")

_ROOT_TEX_BIN = "tex-bin"
_ROOT_USR_LOCAL = "usr-local"
_EXTERNAL_COMMAND_PATHS = {
    "biber": (_ROOT_TEX_BIN, "biber"),
    "bibtex": (_ROOT_TEX_BIN, "bibtex"),
    "tex": (_ROOT_TEX_BIN, "tex"),
    "pdftex": (_ROOT_TEX_BIN, "pdftex"),
    "latex": (_ROOT_TEX_BIN, "latex"),
    "pdflatex": (_ROOT_TEX_BIN, "pdflatex"),
    "xelatex": (_ROOT_TEX_BIN, "xelatex"),
    "kpsewhich": (_ROOT_TEX_BIN, "kpsewhich"),
    "dvisvgm": (_ROOT_TEX_BIN, "dvisvgm"),
    "gs": (_ROOT_USR_LOCAL, "bin/gs"),
    "mutool": (_ROOT_USR_LOCAL, "bin/mutool"),
}
_MANAGED_COMMAND_PATHS = {
    "pdftocairo": "native-tools/bin/pdftocairo",
    "pdftotext": "native-tools/bin/pdftotext",
    "pdfinfo": "native-tools/bin/pdfinfo",
}
_GHOSTSCRIPT_LIBRARY_PATH = (_ROOT_USR_LOCAL, "lib/libgs.dylib")
_GHOSTSCRIPT_RESOURCES_PATH = (_ROOT_USR_LOCAL, "share/ghostscript")
_VERSION_OUTPUT_LIMIT = 64 * 1024
# A freshly installed, quarantined Biber executable can spend more than five seconds in the first
# macOS loader/Gatekeeper pass. Keep the probe bounded while allowing that one-time cold start.
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_TEX_LIVE_RELEASE = re.compile(r"\bTeX Live\s+(\d{4})\b")
_GHOSTSCRIPT_VERSION = re.compile(r"\b(\d+(?:\.\d+){1,3})\b")
_BIBER_VERSION = re.compile(r"\bbiber(?:\s+version)?[: ]+(\d+(?:\.\d+){1,3})\b", re.I)
_DVISVGM_VERSION = re.compile(r"\bdvisvgm\s+(\d+(?:\.\d+){1,3})\b", re.I)
_ACCEPTANCE_ROOT_PREFIX = "academate"
_FINGERPRINT_TEX_RESOURCES = (
    "IEEEtran.bst",
    "amsmath.sty",
    "amssymb.sty",
    "article.cls",
    "array.sty",
    "booktabs.sty",
    "changepage.sty",
    "colortbl.sty",
    "dcolumn.sty",
    "dsfont.sty",
    "extarrows.sty",
    "fontenc.sty",
    "fontspec.sty",
    "graphicx.sty",
    "listings.sty",
    "longtable.sty",
    "makecell.sty",
    "mathtools.sty",
    "multirow.sty",
    "pifont.sty",
    "preview.sty",
    "rotating.sty",
    "siunitx.sty",
    "standalone.cls",
    "stmaryrd.sty",
    "tabularx.sty",
    "tikz-cd.sty",
    "url.sty",
    "xcolor.sty",
)
_RESOURCE_TREE_MAX_FILES = 20_000
_RESOURCE_TREE_MAX_BYTES = 1024 * 1024 * 1024
_TEX_PROCESS_ENVIRONMENT_KEYS = ("TMPDIR", "TEXMFHOME", "TEXMFCONFIG", "TEXMFVAR")
_TEX_WORKSPACE_PREFIX = "academatepaper-tex-"


class MacOSExternalToolchainError(ToolchainResolutionError):
    def __init__(self, message: str, *, state: ExternalToolchainState) -> None:
        self.state = state
        super().__init__(message, retryable=True)


@dataclass(frozen=True, slots=True)
class MacOSExternalToolchainEnvironment:
    toolchain: ResolvedToolchain
    bin_directories: tuple[Path, ...]
    values: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class MacOSExternalToolchainPreflight:
    status: ExternalToolchainSetupStatus
    bin_directories: tuple[Path, ...]
    values: Mapping[str, str]
    fingerprint: str | None


class MacOSExternalToolchainResolver:
    """Resolve a system MacTeX installation through a schema-2 bundle contract.

    The manifest can name only fixed command aliases below two explicit roots.  It cannot add a
    host path or change command discovery, and this resolver never consults the process ``PATH``.
    The three PDFKit helpers remain hash-pinned files inside the signed managed runtime.
    """

    def __init__(
        self,
        *,
        managed_root: Path,
        manifest_path: Path,
        tex_bin_root: Path = DEFAULT_MACTEX_TEX_BIN_ROOT,
        usr_local_root: Path = DEFAULT_MACTEX_USR_LOCAL_ROOT,
        require_executable_bit: bool = True,
        allow_acceptance_roots: bool = False,
        acceptance_root_parent: Path | None = None,
        process_environment: Mapping[str, str] = os.environ,
    ) -> None:
        roots = (managed_root, manifest_path, tex_bin_root, usr_local_root)
        if any(not path.is_absolute() for path in roots):
            raise ValueError("macOS external toolchain paths must be absolute")
        self._managed_root = managed_root.absolute()
        self._manifest_path = manifest_path.absolute()
        self._root_aliases = {
            _ROOT_TEX_BIN: tex_bin_root.absolute(),
            _ROOT_USR_LOCAL: usr_local_root.absolute(),
        }
        self._require_executable_bit = require_executable_bit
        self._process_environment = process_environment
        self._allow_acceptance_roots = allow_acceptance_roots
        if allow_acceptance_roots:
            if acceptance_root_parent is None or not acceptance_root_parent.is_absolute():
                raise ValueError("acceptance toolchain roots require one explicit isolated root")
            self._acceptance_root_parent = acceptance_root_parent.absolute()
            if any(
                not alias.is_relative_to(self._acceptance_root_parent)
                for alias in self._root_aliases.values()
            ):
                raise ValueError("acceptance toolchain roots must be under the isolated root")
        else:
            self._acceptance_root_parent = None
            if acceptance_root_parent is not None:
                raise ValueError("production MacTeX resolution cannot name an acceptance root")
            if (
                self._root_aliases[_ROOT_TEX_BIN] != DEFAULT_MACTEX_TEX_BIN_ROOT
                or self._root_aliases[_ROOT_USR_LOCAL] != DEFAULT_MACTEX_USR_LOCAL_ROOT
            ):
                raise ValueError("production MacTeX roots cannot be overridden")

    def resolve(self) -> ResolvedToolchain:
        return self.resolve_environment().toolchain

    def diagnose_components(self) -> ExternalToolchainComponents:
        """Classify fixed prerequisite groups without exposing host details.

        These diagnostics intentionally stop at presence and trust checks.  ``resolve_environment``
        remains the authoritative functional gate for versions, manifest integrity, resource
        completeness, probes, and the provenance fingerprint.
        """

        tex_commands = tuple(
            (name, relative, True)
            for name, (root_id, relative) in _EXTERNAL_COMMAND_PATHS.items()
            if root_id == _ROOT_TEX_BIN
        )
        return ExternalToolchainComponents(
            mactex=self._diagnose_component_group(
                root_id=_ROOT_TEX_BIN,
                files=tex_commands,
            ),
            ghostscript=self._diagnose_component_group(
                root_id=_ROOT_USR_LOCAL,
                files=(("gs", "bin/gs", True),),
                directories=(("ghostscript resources", "share/ghostscript"),),
            ),
            ghostscript_extras=self._diagnose_component_group(
                root_id=_ROOT_USR_LOCAL,
                files=(
                    ("ghostscript library", "lib/libgs.dylib", False),
                    ("mutool", "bin/mutool", True),
                ),
            ),
        )

    def resolve_environment(self) -> MacOSExternalToolchainEnvironment:
        process_environment = _validated_tex_process_environment(
            self._process_environment
        )
        managed_root = self._resolve_managed_root()
        manifest = self._read_manifest(managed_root)
        external = _required_mapping(manifest, "external")
        self._validate_contract(manifest, external)

        root_targets = self._resolve_external_roots()
        commands: dict[str, str] = {}
        for name, (root_id, relative) in _EXTERNAL_COMMAND_PATHS.items():
            commands[name] = str(
                self._resolve_external_file(
                    name=name,
                    root_id=root_id,
                    relative=relative,
                    root_target=root_targets[root_id],
                    executable=True,
                )
            )

        native_entries = _required_mapping(manifest, "managed_commands")
        for name, relative in _MANAGED_COMMAND_PATHS.items():
            commands[name] = str(
                self._resolve_managed_command(
                    name=name,
                    relative=relative,
                    entry=_required_mapping(native_entries, name),
                    managed_root=managed_root,
                )
            )

        tex_release = self._probe_tex_live_release(
            Path(commands["tex"]),
            bin_directories=self._probe_bin_directories(managed_root),
            process_environment=process_environment,
        )
        minimum_texlive_release = _required_release(external, "minimum_texlive_release")
        if int(tex_release) < int(minimum_texlive_release):
            raise self._invalid("MacTeX TeX Live release is unsupported")

        ghostscript_version = self._probe_ghostscript_version(
            Path(commands["gs"]),
            bin_directories=self._probe_bin_directories(managed_root),
            process_environment=process_environment,
        )
        minimum_ghostscript_version = _required_version(
            external,
            "minimum_ghostscript_version",
        )
        if _version_tuple(ghostscript_version) < _version_tuple(
            minimum_ghostscript_version
        ):
            raise self._invalid("external Ghostscript version is unsupported")
        biber_version = self._probe_named_version(
            Path(commands["biber"]),
            pattern=_BIBER_VERSION,
            label="Biber",
            bin_directories=self._probe_bin_directories(managed_root),
            process_environment=process_environment,
        )
        dvisvgm_version = self._probe_named_version(
            Path(commands["dvisvgm"]),
            pattern=_DVISVGM_VERSION,
            label="dvisvgm",
            bin_directories=self._probe_bin_directories(managed_root),
            process_environment=process_environment,
        )

        ghostscript_library = self._resolve_declared_external_file(
            name="ghostscript library",
            entry=_required_mapping(external, "ghostscript_library"),
            expected=_GHOSTSCRIPT_LIBRARY_PATH,
            root_targets=root_targets,
            executable=False,
        )
        ghostscript_resources = self._resolve_declared_external_directory(
            name="ghostscript resources",
            entry=_required_mapping(external, "ghostscript_resources"),
            expected=_GHOSTSCRIPT_RESOURCES_PATH,
            root_targets=root_targets,
        )
        gs_lib_directories = self._ghostscript_search_directories(
            ghostscript_resources,
            ghostscript_version=ghostscript_version,
            root_target=root_targets[_ROOT_USR_LOCAL],
        )

        capabilities = tuple(ToolchainCapability(value) for value in manifest["capabilities"])
        fingerprint = self._toolchain_fingerprint(
            commands=commands,
            manifest=manifest,
            root_targets=root_targets,
            tex_release=tex_release,
            ghostscript_version=ghostscript_version,
            biber_version=biber_version,
            dvisvgm_version=dvisvgm_version,
            ghostscript_library=ghostscript_library,
            ghostscript_search_directories=gs_lib_directories,
            managed_root=managed_root,
            process_environment=process_environment,
        )
        toolchain = ResolvedToolchain(
            resolver="macos-external",
            profile=MACOS_EXTERNAL_TOOLCHAIN_PROFILE,
            version=tex_release,
            commands=ToolchainCommands(
                **{
                    name: commands[name]
                    for name in ToolchainCommands.__dataclass_fields__
                }
            ),
            capabilities=capabilities,
            managed_root=str(managed_root),
            manifest_path=str(self._manifest_path.resolve(strict=True)),
            fingerprint=fingerprint,
            retry_on_failure=True,
            acceptance_only=self._allow_acceptance_roots,
        )
        return MacOSExternalToolchainEnvironment(
            toolchain=toolchain,
            bin_directories=self._probe_bin_directories(managed_root),
            values={
                "ACADEMATE_TEXLIVE_PROFILE": toolchain.profile,
                "ACADEMATE_TEXLIVE_RELEASE": toolchain.version,
                "GS_LIB": os.pathsep.join(str(path) for path in gs_lib_directories),
                "LIBGS": str(ghostscript_library),
            },
        )

    def _diagnose_component_group(
        self,
        *,
        root_id: str,
        files: Sequence[tuple[str, str, bool]],
        directories: Sequence[tuple[str, str]] = (),
    ) -> ExternalToolchainComponentState:
        root_state, root_target = self._diagnose_external_root(root_id)
        if root_target is None:
            return root_state
        states = [
            self._diagnose_external_file(
                name=name,
                root_id=root_id,
                relative=relative,
                root_target=root_target,
                executable=executable,
            )
            for name, relative, executable in files
        ]
        states.extend(
            self._diagnose_external_directory(
                name=name,
                root_id=root_id,
                relative=relative,
                root_target=root_target,
            )
            for name, relative in directories
        )
        if ExternalToolchainComponentState.INVALID in states:
            return ExternalToolchainComponentState.INVALID
        if ExternalToolchainComponentState.MISSING in states:
            return ExternalToolchainComponentState.MISSING
        return ExternalToolchainComponentState.PRESENT

    def _diagnose_external_root(
        self,
        root_id: str,
    ) -> tuple[ExternalToolchainComponentState, Path | None]:
        try:
            if not self._allow_acceptance_roots:
                self._validate_production_root_alias(root_id)
            target = self._root_aliases[root_id].resolve(strict=True)
            if not target.is_dir():
                raise self._invalid(f"external root {root_id} is invalid")
            self._validate_external_target_trust(target, root_id=root_id)
        except FileNotFoundError:
            return ExternalToolchainComponentState.MISSING, None
        except MacOSExternalToolchainError as error:
            return self._component_state_from_error(error), None
        except Exception:
            return ExternalToolchainComponentState.INVALID, None
        return ExternalToolchainComponentState.PRESENT, target

    def _diagnose_external_file(
        self,
        *,
        name: str,
        root_id: str,
        relative: str,
        root_target: Path,
        executable: bool,
    ) -> ExternalToolchainComponentState:
        alias = (self._root_aliases[root_id] / PurePosixPath(relative)).absolute()
        try:
            target = alias.resolve(strict=True)
        except FileNotFoundError:
            return ExternalToolchainComponentState.MISSING
        except Exception:
            return ExternalToolchainComponentState.INVALID
        try:
            if not target.is_relative_to(root_target) or not target.is_file():
                raise self._invalid(f"external toolchain file escapes its root: {name}")
            self._validate_external_target_trust(target, root_id=root_id)
            if (
                executable
                and self._require_executable_bit
                and target.stat().st_mode & 0o111 == 0
            ):
                raise self._invalid(
                    f"external toolchain command is not executable: {name}"
                )
        except Exception:
            return ExternalToolchainComponentState.INVALID
        return ExternalToolchainComponentState.PRESENT

    def _diagnose_external_directory(
        self,
        *,
        name: str,
        root_id: str,
        relative: str,
        root_target: Path,
    ) -> ExternalToolchainComponentState:
        alias = (self._root_aliases[root_id] / PurePosixPath(relative)).absolute()
        try:
            target = alias.resolve(strict=True)
        except FileNotFoundError:
            return ExternalToolchainComponentState.MISSING
        except Exception:
            return ExternalToolchainComponentState.INVALID
        try:
            if not target.is_relative_to(root_target) or not target.is_dir():
                raise self._invalid(f"external directory escapes its root: {name}")
            self._validate_external_target_trust(target, root_id=root_id)
        except Exception:
            return ExternalToolchainComponentState.INVALID
        return ExternalToolchainComponentState.PRESENT

    @staticmethod
    def _component_state_from_error(
        error: MacOSExternalToolchainError,
    ) -> ExternalToolchainComponentState:
        if error.state is ExternalToolchainState.MISSING:
            return ExternalToolchainComponentState.MISSING
        return ExternalToolchainComponentState.INVALID

    def _resolve_managed_root(self) -> Path:
        try:
            managed_root = self._managed_root.resolve(strict=True)
        except OSError as error:
            raise self._invalid("managed runtime root is unavailable") from error
        if not managed_root.is_dir():
            raise self._invalid("managed runtime root is not a directory")
        return managed_root

    def _read_manifest(self, managed_root: Path) -> dict[str, Any]:
        try:
            resolved_manifest = self._manifest_path.resolve(strict=True)
            if not resolved_manifest.is_relative_to(managed_root):
                raise self._invalid("toolchain manifest escapes the managed runtime")
            raw = json.loads(
                resolved_manifest.read_text(encoding="utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except MacOSExternalToolchainError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise self._invalid("cannot read schema-2 toolchain manifest") from error
        if not isinstance(raw, dict):
            raise self._invalid("schema-2 toolchain manifest must be an object")
        return raw

    def _validate_contract(
        self,
        manifest: dict[str, Any],
        external: Mapping[str, Any],
    ) -> None:
        _require_exact_keys(
            manifest,
            {
                "schema_version",
                "profile",
                "external",
                "managed_commands",
                "capabilities",
            },
            "toolchain manifest",
            self._invalid,
        )
        if manifest["schema_version"] != MACOS_EXTERNAL_TOOLCHAIN_MANIFEST_SCHEMA_VERSION:
            raise self._invalid("toolchain manifest schema is unsupported")
        if manifest["profile"] != MACOS_EXTERNAL_TOOLCHAIN_PROFILE:
            raise self._invalid("toolchain manifest profile is unsupported")
        _require_exact_keys(
            external,
            {
                "kind",
                "minimum_texlive_release",
                "minimum_ghostscript_version",
                "official_url",
                "allowed_roots",
                "commands",
                "ghostscript_library",
                "ghostscript_resources",
            },
            "external toolchain contract",
            self._invalid,
        )
        if external["kind"] != "mactex":
            raise self._invalid("external toolchain kind is unsupported")
        if external["official_url"] != MACTEX_OFFICIAL_URL:
            raise self._invalid("external toolchain URL is not allowlisted")
        if external["allowed_roots"] != [_ROOT_TEX_BIN, _ROOT_USR_LOCAL]:
            raise self._invalid("external toolchain roots are not allowlisted")
        _required_release(external, "minimum_texlive_release")
        _required_version(external, "minimum_ghostscript_version")

        command_entries = _required_mapping(external, "commands")
        if set(command_entries) != set(_EXTERNAL_COMMAND_PATHS):
            raise self._invalid("external toolchain command set is invalid")
        for name, expected in _EXTERNAL_COMMAND_PATHS.items():
            _require_root_path_entry(
                _required_mapping(command_entries, name),
                expected=expected,
                label=f"external command {name}",
                invalid=self._invalid,
            )
        _require_root_path_entry(
            _required_mapping(external, "ghostscript_library"),
            expected=_GHOSTSCRIPT_LIBRARY_PATH,
            label="ghostscript library",
            invalid=self._invalid,
        )
        _require_root_path_entry(
            _required_mapping(external, "ghostscript_resources"),
            expected=_GHOSTSCRIPT_RESOURCES_PATH,
            label="ghostscript resources",
            invalid=self._invalid,
        )

        managed_entries = _required_mapping(manifest, "managed_commands")
        if set(managed_entries) != set(_MANAGED_COMMAND_PATHS):
            raise self._invalid("managed PDFKit command set is invalid")
        for name, expected_path in _MANAGED_COMMAND_PATHS.items():
            entry = _required_mapping(managed_entries, name)
            _require_exact_keys(
                entry,
                {"path", "sha256"},
                f"managed command {name}",
                self._invalid,
            )
            if entry["path"] != expected_path or not _is_lower_sha256(entry["sha256"]):
                raise self._invalid(f"managed command {name} contract is invalid")

        expected_capabilities = [
            capability.value for capability in REQUIRED_TOOLCHAIN_CAPABILITIES
        ]
        if manifest["capabilities"] != expected_capabilities:
            raise self._invalid("toolchain capability contract is invalid")

    def _resolve_external_roots(self) -> dict[str, Path]:
        if not self._allow_acceptance_roots:
            self._validate_production_aliases()
        resolved: dict[str, Path] = {}
        for root_id in (_ROOT_TEX_BIN, _ROOT_USR_LOCAL):
            alias = self._root_aliases[root_id]
            try:
                target = alias.resolve(strict=True)
            except OSError as error:
                if root_id == _ROOT_TEX_BIN:
                    raise self._missing("external MacTeX installation is missing") from error
                raise self._invalid("external MacTeX installation is incomplete") from error
            if not target.is_dir():
                raise self._invalid(f"external root {root_id} is invalid")
            self._validate_external_target_trust(target, root_id=root_id)
            resolved[root_id] = target
        return resolved

    def _validate_production_aliases(self) -> None:
        self._validate_production_root_alias(_ROOT_TEX_BIN)
        self._validate_production_root_alias(_ROOT_USR_LOCAL)

    def _validate_production_root_alias(self, root_id: str) -> None:
        if root_id == _ROOT_TEX_BIN:
            try:
                library = Path("/Library").resolve(strict=True)
                tex_parent = Path("/Library/TeX").resolve(strict=True)
                tex_alias_stat = DEFAULT_MACTEX_TEX_BIN_ROOT.lstat()
            except FileNotFoundError as error:
                raise self._missing("external MacTeX installation is missing") from error
            except OSError as error:
                raise self._invalid("external MacTeX root alias is unavailable") from error
            if not tex_parent.is_relative_to(library):
                raise self._invalid("external MacTeX root alias is not trusted")
            _require_root_owned_chain(tex_parent, anchor=library, invalid=self._invalid)
            if tex_alias_stat.st_uid != 0:
                raise self._invalid("external MacTeX root alias is not system-owned")
            return
        if root_id != _ROOT_USR_LOCAL:  # pragma: no cover - private call invariant
            raise self._invalid("external MacTeX root alias is unsupported")
        try:
            usr_local = DEFAULT_MACTEX_USR_LOCAL_ROOT.resolve(strict=True)
        except FileNotFoundError as error:
            raise self._missing("external MacTeX companion root is missing") from error
        except OSError as error:
            raise self._invalid("external MacTeX companion root is unavailable") from error
        if usr_local != Path("/usr/local"):
            raise self._invalid("external MacTeX companion root is not trusted")
        _require_root_owned_chain(usr_local, anchor=usr_local, invalid=self._invalid)

    def _resolve_external_file(
        self,
        *,
        name: str,
        root_id: str,
        relative: str,
        root_target: Path,
        executable: bool,
    ) -> Path:
        alias = (self._root_aliases[root_id] / PurePosixPath(relative)).absolute()
        try:
            target = alias.resolve(strict=True)
        except OSError as error:
            raise self._invalid(f"external toolchain file is unavailable: {name}") from error
        if not target.is_relative_to(root_target) or not target.is_file():
            raise self._invalid(f"external toolchain file escapes its root: {name}")
        self._validate_external_target_trust(target, root_id=root_id)
        if executable and self._require_executable_bit and target.stat().st_mode & 0o111 == 0:
            raise self._invalid(f"external toolchain command is not executable: {name}")
        return alias

    def _resolve_declared_external_file(
        self,
        *,
        name: str,
        entry: Mapping[str, Any],
        expected: tuple[str, str],
        root_targets: Mapping[str, Path],
        executable: bool,
    ) -> Path:
        _require_root_path_entry(
            entry,
            expected=expected,
            label=name,
            invalid=self._invalid,
        )
        root_id, relative = expected
        return self._resolve_external_file(
            name=name,
            root_id=root_id,
            relative=relative,
            root_target=root_targets[root_id],
            executable=executable,
        )

    def _resolve_declared_external_directory(
        self,
        *,
        name: str,
        entry: Mapping[str, Any],
        expected: tuple[str, str],
        root_targets: Mapping[str, Path],
    ) -> Path:
        _require_root_path_entry(
            entry,
            expected=expected,
            label=name,
            invalid=self._invalid,
        )
        root_id, relative = expected
        alias = (self._root_aliases[root_id] / PurePosixPath(relative)).absolute()
        try:
            target = alias.resolve(strict=True)
        except OSError as error:
            raise self._invalid(f"external directory is unavailable: {name}") from error
        if not target.is_relative_to(root_targets[root_id]) or not target.is_dir():
            raise self._invalid(f"external directory escapes its root: {name}")
        self._validate_external_target_trust(target, root_id=root_id)
        return alias

    def _resolve_managed_command(
        self,
        *,
        name: str,
        relative: str,
        entry: Mapping[str, Any],
        managed_root: Path,
    ) -> Path:
        candidate = (self._managed_root / PurePosixPath(relative)).absolute()
        try:
            target = candidate.resolve(strict=True)
        except OSError as error:
            raise self._invalid(f"managed PDFKit command is unavailable: {name}") from error
        if not target.is_relative_to(managed_root) or not target.is_file():
            raise self._invalid(f"managed PDFKit command escapes runtime: {name}")
        if self._require_executable_bit and target.stat().st_mode & 0o111 == 0:
            raise self._invalid(f"managed PDFKit command is not executable: {name}")
        expected_sha256 = entry["sha256"]
        try:
            with target.open("rb") as executable:
                actual_sha256 = hashlib.file_digest(executable, "sha256").hexdigest()
        except OSError as error:
            raise self._invalid(f"managed PDFKit command cannot be hashed: {name}") from error
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise self._invalid(f"managed PDFKit command hash mismatch: {name}")
        return candidate

    def _probe_tex_live_release(
        self,
        command: Path,
        *,
        bin_directories: Sequence[Path],
        process_environment: Mapping[str, str],
    ) -> str:
        output = _run_bounded_version_probe(
            command,
            bin_directories=bin_directories,
            process_environment=process_environment,
        )
        match = _TEX_LIVE_RELEASE.search(output)
        if match is None:
            raise self._invalid("external TeX Live release could not be identified")
        return match.group(1)

    def _probe_ghostscript_version(
        self,
        command: Path,
        *,
        bin_directories: Sequence[Path],
        process_environment: Mapping[str, str],
    ) -> str:
        output = _run_bounded_version_probe(
            command,
            bin_directories=bin_directories,
            process_environment=process_environment,
        )
        match = _GHOSTSCRIPT_VERSION.search(output)
        if match is None:
            raise self._invalid("external Ghostscript version could not be identified")
        return match.group(1)

    def _probe_named_version(
        self,
        command: Path,
        *,
        pattern: re.Pattern[str],
        label: str,
        bin_directories: Sequence[Path],
        process_environment: Mapping[str, str],
    ) -> str:
        output = _run_bounded_version_probe(
            command,
            bin_directories=bin_directories,
            process_environment=process_environment,
        )
        match = pattern.search(output)
        if match is None:
            raise self._invalid(f"external {label} version could not be identified")
        return match.group(1)

    def _toolchain_fingerprint(
        self,
        *,
        commands: Mapping[str, str],
        manifest: Mapping[str, Any],
        root_targets: Mapping[str, Path],
        tex_release: str,
        ghostscript_version: str,
        biber_version: str,
        dvisvgm_version: str,
        ghostscript_library: Path,
        ghostscript_search_directories: Sequence[Path],
        managed_root: Path,
        process_environment: Mapping[str, str],
    ) -> str:
        external_binary_hashes = {
            name: _sha256_file(Path(commands[name]).resolve(strict=True))
            for name in sorted(_EXTERNAL_COMMAND_PATHS)
        }
        managed_binary_hashes = {
            name: _sha256_file(Path(commands[name]).resolve(strict=True))
            for name in sorted(_MANAGED_COMMAND_PATHS)
        }
        tex_resource_hashes = self._tex_resource_hashes(
            Path(commands["kpsewhich"]),
            bin_directories=self._probe_bin_directories(managed_root),
            process_environment=process_environment,
        )
        ghostscript_resource_tree_sha256 = _sha256_resource_tree(
            ghostscript_search_directories,
            root_target=root_targets[_ROOT_USR_LOCAL],
            validate=lambda target: self._validate_external_target_trust(
                target,
                root_id=_ROOT_USR_LOCAL,
            ),
        )
        manifest_contract_sha256 = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
        identity = {
            "schema_version": 2,
            "profile": MACOS_EXTERNAL_TOOLCHAIN_PROFILE,
            "texlive_release": tex_release,
            "ghostscript_version": ghostscript_version,
            "biber_version": biber_version,
            "dvisvgm_version": dvisvgm_version,
            "canonical_roots": {
                root_id: str(root_targets[root_id])
                for root_id in (_ROOT_TEX_BIN, _ROOT_USR_LOCAL)
            },
            "external_executable_sha256": external_binary_hashes,
            "ghostscript_library_sha256": _sha256_file(
                ghostscript_library.resolve(strict=True)
            ),
            "ghostscript_resource_tree_sha256": ghostscript_resource_tree_sha256,
            "managed_executable_sha256": managed_binary_hashes,
            "manifest_contract_sha256": manifest_contract_sha256,
            "tex_resource_sha256": tex_resource_hashes,
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def _tex_resource_hashes(
        self,
        kpsewhich_command: Path,
        *,
        bin_directories: Sequence[Path],
        process_environment: Mapping[str, str],
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for resource_name in _FINGERPRINT_TEX_RESOURCES:
            output = _run_bounded_probe(
                kpsewhich_command,
                (resource_name,),
                bin_directories=bin_directories,
                process_environment=process_environment,
            )
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            if len(lines) != 1 or "\x00" in lines[0]:
                raise self._invalid(
                    "external TeX resource identity could not be resolved safely"
                )
            raw_path = Path(lines[0])
            if not raw_path.is_absolute():
                raise self._invalid("external TeX resource path is not absolute")
            try:
                target = raw_path.resolve(strict=True)
            except OSError as error:
                raise self._invalid("external TeX resource is unavailable") from error
            if not target.is_file():
                raise self._invalid("external TeX resource is not a regular file")
            self._validate_external_target_trust(target, root_id=_ROOT_TEX_BIN)
            hashes[resource_name] = _sha256_file(target)
        return hashes

    def _validate_external_target_trust(self, target: Path, *, root_id: str) -> None:
        if self._allow_acceptance_roots:
            if self._acceptance_root_parent is None:  # pragma: no cover - constructor invariant
                raise self._invalid("acceptance root is unavailable")
            try:
                acceptance_root = self._acceptance_root_parent.resolve(strict=True)
            except OSError as error:
                raise self._invalid("acceptance root is unavailable") from error
            if not target.is_relative_to(acceptance_root):
                raise self._invalid("acceptance toolchain target escapes its isolated root")
            _require_private_acceptance_chain(
                target,
                anchor=acceptance_root,
                invalid=self._invalid,
            )
            return

        if root_id == _ROOT_TEX_BIN:
            anchor = None
            for allowed_root, trust_anchor in (
                (Path("/usr/local/texlive"), Path("/usr/local")),
                (Path("/Library/TeX"), Path("/Library")),
            ):
                try:
                    resolved_allowed = allowed_root.resolve(strict=True)
                    resolved_anchor = trust_anchor.resolve(strict=True)
                except OSError:
                    continue
                if target.is_relative_to(resolved_allowed):
                    anchor = resolved_anchor
                    break
        else:
            anchor = Path("/usr/local").resolve(strict=True)
            if not target.is_relative_to(anchor):
                anchor = None
        if anchor is None:
            raise self._invalid("external toolchain target is outside a trusted system tree")
        _require_root_owned_chain(target, anchor=anchor, invalid=self._invalid)

    def _ghostscript_search_directories(
        self,
        resource_root: Path,
        *,
        ghostscript_version: str,
        root_target: Path,
    ) -> tuple[Path, ...]:
        version_root = resource_root / ghostscript_version
        required = (version_root / "Resource" / "Init", version_root / "lib")
        optional = (version_root / "Resource" / "Font", resource_root / "fonts")
        directories: list[Path] = []
        for directory in (*required, *optional):
            try:
                target = directory.resolve(strict=True)
            except OSError as error:
                if directory in optional:
                    continue
                raise self._invalid("external Ghostscript resources are incomplete") from error
            if not target.is_relative_to(root_target) or not target.is_dir():
                raise self._invalid("external Ghostscript resources escape their root")
            self._validate_external_target_trust(target, root_id=_ROOT_USR_LOCAL)
            directories.append(target)
        return tuple(directories)

    def _probe_bin_directories(self, managed_root: Path) -> tuple[Path, ...]:
        return (
            self._root_aliases[_ROOT_TEX_BIN],
            self._root_aliases[_ROOT_USR_LOCAL] / "bin",
            managed_root / "native-tools" / "bin",
        )

    @staticmethod
    def _missing(message: str) -> MacOSExternalToolchainError:
        return MacOSExternalToolchainError(
            message,
            state=ExternalToolchainState.MISSING,
        )

    @staticmethod
    def _invalid(message: str) -> MacOSExternalToolchainError:
        return MacOSExternalToolchainError(
            message,
            state=ExternalToolchainState.INVALID,
        )


def preflight_macos_external_toolchain(
    *,
    managed_root: Path,
    manifest_path: Path,
    tex_bin_root: Path = DEFAULT_MACTEX_TEX_BIN_ROOT,
    usr_local_root: Path = DEFAULT_MACTEX_USR_LOCAL_ROOT,
    allow_acceptance_roots: bool = False,
    acceptance_root_parent: Path | None = None,
    process_environment: Mapping[str, str] = os.environ,
) -> MacOSExternalToolchainPreflight:
    """Return a nonfatal, public-safe launch snapshot and a deterministic child environment."""

    resolver = MacOSExternalToolchainResolver(
        managed_root=managed_root,
        manifest_path=manifest_path,
        tex_bin_root=tex_bin_root,
        usr_local_root=usr_local_root,
        allow_acceptance_roots=allow_acceptance_roots,
        acceptance_root_parent=acceptance_root_parent,
        process_environment=process_environment,
    )
    components = resolver.diagnose_components()
    try:
        resolved = resolver.resolve_environment()
    except MacOSExternalToolchainError as error:
        state = error.state
        values = _empty_external_environment()
        bin_directories = ((managed_root / "native-tools" / "bin").absolute(),)
    except Exception:
        state = ExternalToolchainState.INVALID
        values = _empty_external_environment()
        bin_directories = ((managed_root / "native-tools" / "bin").absolute(),)
    else:
        state = ExternalToolchainState.AVAILABLE
        components = ExternalToolchainComponents(
            mactex=ExternalToolchainComponentState.PRESENT,
            ghostscript=ExternalToolchainComponentState.PRESENT,
            ghostscript_extras=ExternalToolchainComponentState.PRESENT,
        )
        values = resolved.values
        bin_directories = resolved.bin_directories
    fingerprint = (
        resolved.toolchain.fingerprint
        if state is ExternalToolchainState.AVAILABLE
        else None
    )
    status = ExternalToolchainSetupStatus(
        kind=ExternalToolchainKind.MACTEX,
        state=state,
        official_url=MACTEX_OFFICIAL_URL,
        restart_required=state is not ExternalToolchainState.AVAILABLE,
        components=components,
    )
    return MacOSExternalToolchainPreflight(
        status=status,
        bin_directories=bin_directories,
        values=values,
        fingerprint=fingerprint,
    )


def _empty_external_environment() -> dict[str, str]:
    return {
        "ACADEMATE_TEXLIVE_PROFILE": "",
        "ACADEMATE_TEXLIVE_RELEASE": "",
        "GS_LIB": "",
        "LIBGS": "",
    }


def _run_bounded_version_probe(
    command: Path,
    *,
    bin_directories: Sequence[Path],
    process_environment: Mapping[str, str],
) -> str:
    return _run_bounded_probe(
        command,
        ("--version",),
        bin_directories=bin_directories,
        process_environment=process_environment,
    )


def _run_bounded_probe(
    command: Path,
    arguments: Sequence[str],
    *,
    bin_directories: Sequence[Path],
    process_environment: Mapping[str, str],
) -> str:
    environment = {
        "PATH": os.pathsep.join(str(directory.absolute()) for directory in bin_directories),
        "LANG": "C",
        "LC_ALL": "C",
        **{
            key: process_environment[key]
            for key in _TEX_PROCESS_ENVIRONMENT_KEYS
        },
    }
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                (str(command), *arguments),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
        except OSError as error:
            raise MacOSExternalToolchainError(
                "external toolchain identity probe could not start",
                state=ExternalToolchainState.INVALID,
            ) from error
        try:
            return_code = process.wait(timeout=_VERSION_PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise MacOSExternalToolchainError(
                "external toolchain identity probe timed out",
                state=ExternalToolchainState.INVALID,
            ) from error
        output.seek(0, os.SEEK_END)
        length = output.tell()
        if return_code != 0 or length > _VERSION_OUTPUT_LIMIT:
            raise MacOSExternalToolchainError(
                "external toolchain identity probe failed",
                state=ExternalToolchainState.INVALID,
            )
        output.seek(0)
        return output.read(_VERSION_OUTPUT_LIMIT).decode("utf-8", errors="replace")


def _validated_tex_process_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Validate the private workspace shared by resolver probes and managed children."""

    values: dict[str, str] = {}
    paths: dict[str, Path] = {}
    for key in _TEX_PROCESS_ENVIRONMENT_KEYS:
        value = source.get(key)
        if (
            not isinstance(value, str)
            or not value
            or not value.isascii()
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise MacOSExternalToolchainError(
                "external toolchain process workspace is unavailable",
                state=ExternalToolchainState.INVALID,
            )
        raw = Path(value)
        if not raw.is_absolute():
            raise MacOSExternalToolchainError(
                "external toolchain process workspace is invalid",
                state=ExternalToolchainState.INVALID,
            )
        try:
            resolved = raw.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as error:
            raise MacOSExternalToolchainError(
                "external toolchain process workspace is unavailable",
                state=ExternalToolchainState.INVALID,
            ) from error
        if (
            raw.absolute() != resolved
            or raw.is_symlink()
            or not resolved.is_dir()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise MacOSExternalToolchainError(
                "external toolchain process workspace is not private",
                state=ExternalToolchainState.INVALID,
            )
        values[key] = str(resolved)
        paths[key] = resolved
    if len(set(paths.values())) != len(paths):
        raise MacOSExternalToolchainError(
            "external toolchain process workspace paths must be distinct",
            state=ExternalToolchainState.INVALID,
        )
    parents = {path.parent for path in paths.values()}
    if len(parents) != 1:
        raise MacOSExternalToolchainError(
            "external toolchain process workspace escapes its private root",
            state=ExternalToolchainState.INVALID,
        )
    root = next(iter(parents))
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise MacOSExternalToolchainError(
            "external toolchain process workspace root is unavailable",
            state=ExternalToolchainState.INVALID,
        ) from error
    if (
        not root.name.startswith(_TEX_WORKSPACE_PREFIX)
        or not str(root).isascii()
        or any(character.isspace() or ord(character) < 32 for character in str(root))
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise MacOSExternalToolchainError(
            "external toolchain process workspace root is not trusted",
            state=ExternalToolchainState.INVALID,
        )
    return values


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _required_mapping(parent: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise MacOSExternalToolchainError(
            f"toolchain manifest requires an object for {key}",
            state=ExternalToolchainState.INVALID,
        )
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
    invalid: Any,
) -> None:
    if set(value) != expected:
        raise invalid(f"{label} has an invalid field set")


def _require_root_path_entry(
    entry: Mapping[str, Any],
    *,
    expected: tuple[str, str],
    label: str,
    invalid: Any,
) -> None:
    _require_exact_keys(entry, {"root", "path"}, label, invalid)
    root, relative = expected
    if entry["root"] != root or entry["path"] != relative:
        raise invalid(f"{label} is not allowlisted")
    _require_canonical_relative_path(relative, label=label, invalid=invalid)


def _require_canonical_relative_path(relative: str, *, label: str, invalid: Any) -> None:
    path = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or any(ord(character) < 32 for character in relative)
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != relative
    ):
        raise invalid(f"{label} path is invalid")


def _required_release(parent: Mapping[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or re.fullmatch(r"\d{4}", value) is None:
        raise MacOSExternalToolchainError(
            f"toolchain manifest has an invalid {key}",
            state=ExternalToolchainState.INVALID,
        )
    return value


def _required_version(parent: Mapping[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or re.fullmatch(r"\d+(?:\.\d+){1,3}", value) is None:
        raise MacOSExternalToolchainError(
            f"toolchain manifest has an invalid {key}",
            state=ExternalToolchainState.INVALID,
        )
    return value


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in value.split("."))
    return (*parts, *(0 for _ in range(4 - len(parts))))


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as error:
        raise MacOSExternalToolchainError(
            "external toolchain fingerprint input is unavailable",
            state=ExternalToolchainState.INVALID,
        ) from error


def _sha256_resource_tree(
    directories: Sequence[Path],
    *,
    root_target: Path,
    validate: Any,
) -> str:
    """Hash a bounded, trusted Ghostscript search tree without following symlinked entries."""

    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    seen: set[Path] = set()
    for directory in sorted({path.resolve(strict=True) for path in directories}):
        if not directory.is_relative_to(root_target):
            raise MacOSExternalToolchainError(
                "external Ghostscript resources escape their root",
                state=ExternalToolchainState.INVALID,
            )
        for candidate in sorted((directory, *directory.rglob("*"))):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_symlink():
                raise MacOSExternalToolchainError(
                    "external Ghostscript resources contain a symbolic link",
                    state=ExternalToolchainState.INVALID,
                )
            try:
                metadata = candidate.stat()
            except OSError as error:
                raise MacOSExternalToolchainError(
                    "external Ghostscript resource identity is unavailable",
                    state=ExternalToolchainState.INVALID,
                ) from error
            validate(candidate)
            relative = candidate.relative_to(root_target).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                record = ("directory", relative)
            elif stat.S_ISREG(metadata.st_mode):
                file_count += 1
                total_bytes += metadata.st_size
                if (
                    file_count > _RESOURCE_TREE_MAX_FILES
                    or total_bytes > _RESOURCE_TREE_MAX_BYTES
                ):
                    raise MacOSExternalToolchainError(
                        "external Ghostscript resource tree exceeds its fingerprint bound",
                        state=ExternalToolchainState.INVALID,
                    )
                record = (
                    "file",
                    relative,
                    metadata.st_size,
                    _sha256_file(candidate),
                )
            else:
                raise MacOSExternalToolchainError(
                    "external Ghostscript resource tree contains a special file",
                    state=ExternalToolchainState.INVALID,
                )
            digest.update(
                json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def resolve_macos_acceptance_root(
    *,
    external_tex_bin_root: Path | None = None,
    external_usr_local_root: Path | None = None,
    disposable_roots: Sequence[Path] = (),
    additional_roots: Sequence[Path] = (),
    private_tmp_root: Path = Path("/private/tmp"),
) -> Path:
    """Validate and return the one private top-level tree for native acceptance.

    The production resolver never calls this function. It exists only for hidden native acceptance
    entrypoints, where every injected path must remain under one operator-owned, mode-0700 direct
    child of ``/private/tmp`` whose name begins with ``academate``.
    """

    if (external_tex_bin_root is None) is not (external_usr_local_root is None):
        raise ValueError("native acceptance external roots must be provided together")
    external_roots = (
        ()
        if external_tex_bin_root is None or external_usr_local_root is None
        else (external_tex_bin_root, external_usr_local_root)
    )
    roots = (*external_roots, *disposable_roots, *additional_roots)
    if len(disposable_roots) not in {0, 4}:
        raise ValueError("native acceptance requires exactly four disposable roots")
    if not roots:
        raise ValueError("native acceptance requires at least one explicit root")
    if any(not root.is_absolute() for root in roots):
        raise ValueError("native acceptance roots must be absolute")
    try:
        private_tmp = private_tmp_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("native acceptance private temporary root is unavailable") from error
    if not private_tmp.is_dir():
        raise ValueError("native acceptance private temporary root is invalid")

    absolute_roots = tuple(root.absolute() for root in roots)
    try:
        resolved_roots = tuple(root.resolve(strict=True) for root in absolute_roots)
    except OSError as error:
        raise ValueError("native acceptance root is unavailable") from error
    external_count = len(external_roots)
    directory_count = external_count + len(disposable_roots)
    if any(not root.is_dir() for root in resolved_roots[:directory_count]):
        raise ValueError("native acceptance root must be a directory")
    if any(not root.is_file() for root in resolved_roots[directory_count:]):
        raise ValueError("native acceptance auxiliary root must be a regular file")
    try:
        raw_parts = [root.relative_to(private_tmp).parts for root in absolute_roots]
        resolved_parts = [root.relative_to(private_tmp).parts for root in resolved_roots]
    except ValueError as error:
        raise ValueError("native acceptance root escapes /private/tmp") from error
    if any(not parts for parts in (*raw_parts, *resolved_parts)):
        raise ValueError("native acceptance cannot use /private/tmp itself")
    anchor_names = {parts[0] for parts in (*raw_parts, *resolved_parts)}
    if len(anchor_names) != 1:
        raise ValueError("native acceptance roots must share one private tree")
    anchor_name = next(iter(anchor_names))
    if not anchor_name.startswith(_ACCEPTANCE_ROOT_PREFIX):
        raise ValueError("native acceptance tree name is not allowlisted")
    anchor = private_tmp / anchor_name
    if anchor.is_symlink():
        raise ValueError("native acceptance tree must not be a symbolic link")
    try:
        resolved_anchor = anchor.resolve(strict=True)
        metadata = resolved_anchor.stat()
    except OSError as error:
        raise ValueError("native acceptance tree is unavailable") from error
    if (
        resolved_anchor != anchor
        or not resolved_anchor.is_dir()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("native acceptance tree is not private to the current user")

    for root in resolved_roots:
        if not root.is_relative_to(resolved_anchor):
            raise ValueError("native acceptance root escapes its private tree")
    if disposable_roots:
        disposable_absolute = absolute_roots[
            external_count : external_count + len(disposable_roots)
        ]
        disposable_resolved = resolved_roots[
            external_count : external_count + len(disposable_roots)
        ]
        if any(
            raw != resolved
            for raw, resolved in zip(
                disposable_absolute,
                disposable_resolved,
                strict=True,
            )
        ):
            raise ValueError("native acceptance disposable root must not use symlinks")
        if len(set(disposable_resolved)) != 4:
            raise ValueError("native acceptance disposable roots must be distinct")
        for index, root in enumerate(disposable_resolved):
            if any(
                index != other_index
                and (
                    root.is_relative_to(other)
                    or other.is_relative_to(root)
                )
                for other_index, other in enumerate(disposable_resolved)
            ):
                raise ValueError("native acceptance disposable roots must not overlap")
        for resolved in disposable_resolved:
            _require_private_acceptance_chain(
                resolved,
                anchor=resolved_anchor,
                invalid=ValueError,
                require_private_leaf=True,
            )
    additional_absolute = absolute_roots[directory_count:]
    additional_resolved = resolved_roots[directory_count:]
    for raw, resolved in zip(additional_absolute, additional_resolved, strict=True):
        if raw != resolved:
            raise ValueError("native acceptance auxiliary root must not use symlinks")
        _require_private_acceptance_chain(
            resolved,
            anchor=resolved_anchor,
            invalid=ValueError,
            require_private_leaf=True,
        )
    return resolved_anchor


def _require_private_acceptance_chain(
    path: Path,
    *,
    anchor: Path,
    invalid: Any,
    require_private_leaf: bool = False,
) -> None:
    current = path
    first = True
    while True:
        try:
            metadata = current.stat()
        except OSError as error:
            raise invalid("acceptance toolchain path is unavailable") from error
        if metadata.st_uid not in {0, os.geteuid()} or metadata.st_mode & 0o022:
            raise invalid("acceptance toolchain path is writable by another user")
        if first and require_private_leaf and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise invalid("acceptance disposable root is not private")
        if current == anchor:
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise invalid("acceptance root is not private to the current user")
            return
        if not current.is_relative_to(anchor) or current.parent == current:
            raise invalid("acceptance toolchain path escapes its isolated root")
        current = current.parent
        first = False


def _require_root_owned_chain(path: Path, *, anchor: Path, invalid: Any) -> None:
    current = path
    while True:
        try:
            metadata = current.stat()
        except OSError as error:
            raise invalid("trusted external toolchain path is unavailable") from error
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise invalid("external toolchain path is not protected by system ownership")
        if current == anchor:
            return
        if not current.is_relative_to(anchor) or current.parent == current:
            raise invalid("external toolchain path escapes its trusted system tree")
        current = current.parent
