from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class FileFailureKind(StrEnum):
    PERMISSION_DENIED = "PERMISSION_DENIED"
    READ_ONLY_FILESYSTEM = "READ_ONLY_FILESYSTEM"
    DISK_FULL = "DISK_FULL"
    PATH_TOO_LONG = "PATH_TOO_LONG"
    IO_ERROR = "IO_ERROR"


class FilesystemOperationError(OSError):
    """OS-neutral filesystem failure exposed by platform adapters.

    Callers make recovery decisions from ``kind`` instead of parsing platform-specific error
    strings.  The original exception remains available through exception chaining for diagnostics.
    """

    def __init__(self, *, kind: FileFailureKind, operation: str, path: Path) -> None:
        self.kind = kind
        self.operation = operation
        self.path = path
        super().__init__(f"{operation} failed for {path}: {kind.value}")


@dataclass(frozen=True, slots=True)
class AppDirectoryPaths:
    data: Path
    config: Path
    logs: Path
    cache: Path
    temporary: Path


class AppDirectories(Protocol):
    def paths(self) -> AppDirectoryPaths: ...

    def initialize(self) -> AppDirectoryPaths: ...


class ToolchainCapability(StrEnum):
    PDFLATEX = "pdflatex"
    XELATEX = "xelatex"
    BIBER_BIBLATEXML = "biber-biblatexml"
    BIBTEX_IEEE = "bibtex-ieeetran"
    PDF_TEXT = "pdf-text"
    PDF_INFO = "pdf-info"
    TABLE_PNG = "table-png"
    FIGURE_SVG = "figure-svg"
    FIGURE_PNG = "figure-png"


class ExternalToolchainKind(StrEnum):
    MACTEX = "MACTEX"


class ExternalToolchainState(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    INVALID = "INVALID"


class ExternalToolchainComponentState(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ExternalToolchainComponents:
    """Public-safe prerequisite groups without paths or diagnostic text."""

    mactex: ExternalToolchainComponentState
    ghostscript: ExternalToolchainComponentState
    ghostscript_extras: ExternalToolchainComponentState

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, ExternalToolchainComponentState)
            for value in (
                self.mactex,
                self.ghostscript,
                self.ghostscript_extras,
            )
        ):
            raise TypeError("external toolchain component states must use the public enum")


@dataclass(frozen=True, slots=True)
class ExternalToolchainSetupStatus:
    """Small public-safe snapshot produced by a native startup adapter.

    The contract deliberately has no free-form diagnostics, command paths, versions, or process
    output.  Setup may expose it before authentication without turning host discovery into an
    information oracle.
    """

    kind: ExternalToolchainKind
    state: ExternalToolchainState
    official_url: str
    restart_required: bool
    components: ExternalToolchainComponents

    def __post_init__(self) -> None:
        if self.official_url != "https://www.tug.org/mactex/":
            raise ValueError("external toolchain support URL is not allowlisted")
        if self.restart_required is (self.state is ExternalToolchainState.AVAILABLE):
            raise ValueError("external toolchain restart flag contradicts startup state")
        if not isinstance(self.components, ExternalToolchainComponents):
            raise TypeError("external toolchain components must use the public-safe contract")
        if self.state is ExternalToolchainState.AVAILABLE and any(
            component is not ExternalToolchainComponentState.PRESENT
            for component in (
                self.components.mactex,
                self.components.ghostscript,
                self.components.ghostscript_extras,
            )
        ):
            raise ValueError("available external toolchain requires all components present")


@dataclass(frozen=True, slots=True)
class ToolchainCommands:
    biber: str
    bibtex: str
    tex: str
    pdftex: str
    latex: str
    pdflatex: str
    xelatex: str
    kpsewhich: str
    dvisvgm: str
    pdftocairo: str
    pdftotext: str
    pdfinfo: str

    def as_dict(self) -> dict[str, str]:
        return {
            "biber": self.biber,
            "bibtex": self.bibtex,
            "tex": self.tex,
            "pdftex": self.pdftex,
            "latex": self.latex,
            "pdflatex": self.pdflatex,
            "xelatex": self.xelatex,
            "kpsewhich": self.kpsewhich,
            "dvisvgm": self.dvisvgm,
            "pdftocairo": self.pdftocairo,
            "pdftotext": self.pdftotext,
            "pdfinfo": self.pdfinfo,
        }


@dataclass(frozen=True, slots=True)
class ResolvedToolchain:
    resolver: str
    profile: str
    version: str
    commands: ToolchainCommands
    capabilities: tuple[ToolchainCapability, ...]
    managed_root: str | None
    manifest_path: str | None
    fingerprint: str | None = None
    retry_on_failure: bool = False
    acceptance_only: bool = False

    def __post_init__(self) -> None:
        if self.fingerprint is not None and (
            len(self.fingerprint) != 71
            or not self.fingerprint.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in self.fingerprint.removeprefix("sha256:")
            )
        ):
            raise ValueError("toolchain fingerprint must be a lowercase SHA-256 identity")


class ToolchainResolver(Protocol):
    def resolve(self) -> ResolvedToolchain: ...


def toolchain_cache_identity(
    renderer_version: str,
    fingerprint: str | None,
) -> str:
    """Derive a bounded idempotency identity without changing renderer provenance."""

    if not renderer_version or "\x00" in renderer_version:
        raise ValueError("renderer version is invalid")
    if fingerprint is None:
        return renderer_version
    if (
        len(fingerprint) != 71
        or not fingerprint.startswith("sha256:")
        or any(
            character not in "0123456789abcdef"
            for character in fingerprint.removeprefix("sha256:")
        )
    ):
        raise ValueError("toolchain fingerprint is invalid")
    return f"{renderer_version}:tc:{fingerprint.removeprefix('sha256:')[:32]}"
