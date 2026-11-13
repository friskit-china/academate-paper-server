from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

RELEASE_MANIFEST_SCHEMA_VERSION = 3
RELEASE_FULL_INVENTORY_SCHEMA_VERSION = 1
RELEASE_STARTUP_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
RELEASE_FULL_INVENTORY_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_RELEASE_MANIFEST_PATH = PurePosixPath(
    "Contents/Resources/runtime/manifests/release.json"
)
DEFAULT_FULL_INVENTORY_PATH = PurePosixPath(
    "Contents/Resources/runtime/manifests/release-inventory.json"
)
_HASH_BUFFER_BYTES = 1024 * 1024
_SHA256_LENGTH = 64


class ReleaseIntegrityError(RuntimeError):
    """Raised when a packaged release cannot be trusted or verified completely."""


class ReleaseVerificationMode(StrEnum):
    STARTUP = "startup"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class ReleaseIntegrityReport:
    mode: ReleaseVerificationMode
    schema_version: int
    manifest_sha256: str
    total_entries: int
    checked_files: int
    checked_symlinks: int
    unchecked_entries: int

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "passed",
            "mode": self.mode.value,
            "schema_version": self.schema_version,
            "manifest_sha256": self.manifest_sha256,
            "total_entries": self.total_entries,
            "checked_files": self.checked_files,
            "checked_symlinks": self.checked_symlinks,
            "unchecked_entries": self.unchecked_entries,
        }


@dataclass(frozen=True, slots=True)
class _ReleaseEntry:
    path: str
    kind: str
    size: int | None = None
    sha256: str | None = None
    executable: bool | None = None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class _FullInventoryDescriptor:
    path: str
    size: int
    sha256: str
    entries: int


def _sha256_stream(source: BinaryIO) -> str:
    value = hashlib.sha256()
    while chunk := source.read(_HASH_BUFFER_BYTES):
        value.update(chunk)
    return value.hexdigest()


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_regular_file(path: Path) -> tuple[int, os.stat_result]:
    try:
        path_before = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseIntegrityError(f"Cannot inspect release file: {path}") from error
    if not stat.S_ISREG(path_before.st_mode):
        raise ReleaseIntegrityError(f"Release entry is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseIntegrityError(f"Cannot open release file safely: {path}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or _file_identity(path_before) != _file_identity(opened):
        os.close(descriptor)
        raise ReleaseIntegrityError(f"Release file changed before reading: {path}")
    return descriptor, opened


def _finish_regular_file_read(
    path: Path,
    descriptor: int,
    opened_before: os.stat_result,
) -> os.stat_result:
    opened_after = os.fstat(descriptor)
    try:
        path_after = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseIntegrityError(f"Release file disappeared while reading: {path}") from error
    if (
        _file_identity(opened_before) != _file_identity(opened_after)
        or _file_identity(opened_after) != _file_identity(path_after)
    ):
        raise ReleaseIntegrityError(f"Release file changed while reading: {path}")
    return opened_after


def _stable_regular_file_metadata(path: Path) -> tuple[int, str, bool]:
    """Hash one regular file while rejecting symlink swaps and concurrent mutation."""

    descriptor, opened_before = _open_regular_file(path)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            digest = _sha256_stream(source)
        opened_after = _finish_regular_file_read(path, descriptor, opened_before)
    finally:
        os.close(descriptor)
    return (
        opened_after.st_size,
        digest,
        bool(opened_after.st_mode & 0o111),
    )


def _stable_bounded_file_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor, opened_before = _open_regular_file(path)
    try:
        if opened_before.st_size > maximum_bytes:
            raise ReleaseIntegrityError(f"Release metadata file exceeds size limit: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ReleaseIntegrityError(f"Release metadata file exceeds size limit: {path}")
        _finish_regular_file_read(path, descriptor, opened_before)
    finally:
        os.close(descriptor)
    return raw


def _canonical_relative_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseIntegrityError(f"{field} must be a non-empty POSIX path.")
    if "\\" in value or any(ord(character) < 32 for character in value):
        raise ReleaseIntegrityError(f"{field} is not a canonical POSIX path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ReleaseIntegrityError(f"{field} is not a canonical POSIX path: {value!r}")
    return value


def _path_under_root(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def _is_platform_signature_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return len(parts) >= 2 and parts[:2] == ("Contents", "_CodeSignature")


def _iter_release_nodes(
    root: Path,
    *,
    excluded_relative_paths: frozenset[str],
) -> Iterator[tuple[str, Path, str]]:
    """Walk without following links so the inventory represents the bundle itself."""

    def visit(directory: Path) -> Iterator[tuple[str, Path, str]]:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise ReleaseIntegrityError(
                f"Cannot enumerate release directory: {directory}"
            ) from error
        for child in children:
            child_path = Path(child.path)
            relative = child_path.relative_to(root).as_posix()
            if relative in excluded_relative_paths:
                continue
            try:
                if _is_platform_signature_path(relative):
                    if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                        raise ReleaseIntegrityError(
                            "Platform signature metadata must be a real directory: "
                            f"{relative}"
                        )
                    continue
                if child.is_symlink():
                    yield relative, child_path, "symlink"
                elif child.is_dir(follow_symlinks=False):
                    yield from visit(child_path)
                elif child.is_file(follow_symlinks=False):
                    yield relative, child_path, "file"
                else:
                    raise ReleaseIntegrityError(
                        f"Unsupported filesystem entry in release bundle: {relative}"
                    )
            except OSError as error:
                raise ReleaseIntegrityError(
                    f"Cannot inspect release filesystem entry: {relative}"
                ) from error

    yield from visit(root)


def _validated_symlink_target(root: Path, path: Path, relative: str) -> str:
    try:
        target = os.readlink(path)
    except OSError as error:
        raise ReleaseIntegrityError(f"Cannot read release symlink: {relative}") from error
    if not target or "\\" in target or any(ord(character) < 32 for character in target):
        raise ReleaseIntegrityError(f"Release symlink target is invalid: {relative}")
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise ReleaseIntegrityError(f"Release symlink target must be relative: {relative}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ReleaseIntegrityError(f"Release symlink is broken: {relative}") from error
    if not resolved.is_relative_to(root):
        raise ReleaseIntegrityError(f"Release symlink escapes the bundle: {relative}")
    return target


def build_release_inventory(
    app_root: Path,
    *,
    excluded_relative_paths: Iterable[str] = (
        DEFAULT_RELEASE_MANIFEST_PATH.as_posix(),
        DEFAULT_FULL_INVENTORY_PATH.as_posix(),
    ),
) -> list[dict[str, object]]:
    """Create a deterministic, relocatable inventory for a private app bundle tree."""

    root = app_root.resolve(strict=True)
    if not root.is_dir():
        raise ReleaseIntegrityError("Release root must be a directory.")
    excluded = frozenset(
        _canonical_relative_path(path, field="excluded release metadata path")
        for path in excluded_relative_paths
    )
    if not excluded:
        raise ReleaseIntegrityError("Release inventory requires excluded metadata paths.")
    inventory: list[dict[str, object]] = []
    for relative, path, kind in _iter_release_nodes(
        root,
        excluded_relative_paths=excluded,
    ):
        relative = _canonical_relative_path(relative, field="release file path")
        if kind == "file":
            size, digest, executable = _stable_regular_file_metadata(path)
            inventory.append(
                {
                    "path": relative,
                    "kind": "file",
                    "size": size,
                    "sha256": digest,
                    "executable": executable,
                }
            )
        else:
            inventory.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": _validated_symlink_target(root, path, relative),
                }
            )
    return sorted(inventory, key=lambda entry: str(entry["path"]))


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseIntegrityError(f"Release metadata contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_metadata(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[dict[str, Any], str, int]:
    raw = _stable_bounded_file_bytes(path, maximum_bytes=maximum_bytes)
    manifest_digest = hashlib.sha256(raw).hexdigest()
    try:
        decoded = raw.decode("utf-8")
        manifest = json.loads(
            decoded,
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseIntegrityError(f"{label} is not valid UTF-8 JSON.") from error
    if not isinstance(manifest, dict):
        raise ReleaseIntegrityError(f"{label} root must be an object.")
    return manifest, manifest_digest, len(raw)


def _require_nonempty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseIntegrityError(f"Release manifest requires {field}.")
    return value


def _parse_release_entry(raw: object) -> _ReleaseEntry:
    if not isinstance(raw, dict):
        raise ReleaseIntegrityError("Every release manifest file entry must be an object.")
    relative = _canonical_relative_path(raw.get("path"), field="release file path")
    kind = raw.get("kind")
    if kind == "file":
        allowed = {"path", "kind", "size", "sha256", "executable"}
        if set(raw) != allowed:
            raise ReleaseIntegrityError(f"Release file entry has unexpected fields: {relative}")
        size = raw.get("size")
        digest = raw.get("sha256")
        executable = raw.get("executable")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ReleaseIntegrityError(f"Release file size is invalid: {relative}")
        if (
            not isinstance(digest, str)
            or len(digest) != _SHA256_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ReleaseIntegrityError(f"Release file SHA-256 is invalid: {relative}")
        if not isinstance(executable, bool):
            raise ReleaseIntegrityError(f"Release executable marker is invalid: {relative}")
        return _ReleaseEntry(
            path=relative,
            kind=kind,
            size=size,
            sha256=digest,
            executable=executable,
        )
    if kind == "symlink":
        allowed = {"path", "kind", "target"}
        if set(raw) != allowed:
            raise ReleaseIntegrityError(f"Release symlink entry has unexpected fields: {relative}")
        target = raw.get("target")
        if not isinstance(target, str) or not target:
            raise ReleaseIntegrityError(f"Release symlink target is invalid: {relative}")
        return _ReleaseEntry(path=relative, kind=kind, target=target)
    raise ReleaseIntegrityError(f"Release entry kind is unsupported: {relative}")


def _parse_release_entries(
    raw_entries: object,
    *,
    label: str,
    forbidden_paths: frozenset[str],
) -> dict[str, _ReleaseEntry]:
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ReleaseIntegrityError(f"{label} requires non-empty release entries.")
    entries: dict[str, _ReleaseEntry] = {}
    ordered_paths: list[str] = []
    for raw in raw_entries:
        entry = _parse_release_entry(raw)
        if entry.path in forbidden_paths:
            raise ReleaseIntegrityError(
                f"Release metadata cannot inventory itself: {entry.path}"
            )
        if _is_platform_signature_path(entry.path):
            raise ReleaseIntegrityError(
                "Platform signature metadata must be verified by the platform signature gate."
            )
        if entry.path in entries:
            raise ReleaseIntegrityError(f"Release manifest path is duplicated: {entry.path}")
        entries[entry.path] = entry
        ordered_paths.append(entry.path)
    if ordered_paths != sorted(ordered_paths):
        raise ReleaseIntegrityError(f"{label} entries must be sorted by path.")
    return entries


def _parse_sha256(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReleaseIntegrityError(f"{field} SHA-256 is invalid.")
    return value


def _parse_full_inventory_descriptor(
    raw: object,
    *,
    manifest_relative_path: str,
) -> _FullInventoryDescriptor:
    if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256", "entries"}:
        raise ReleaseIntegrityError("Release manifest full-inventory descriptor is invalid.")
    path = _canonical_relative_path(raw.get("path"), field="full inventory path")
    expected_path = PurePosixPath(manifest_relative_path).with_name(
        DEFAULT_FULL_INVENTORY_PATH.name
    ).as_posix()
    if path != expected_path:
        raise ReleaseIntegrityError(
            "Full inventory must be the canonical sibling of the release manifest."
        )
    size = raw.get("size")
    entries = raw.get("entries")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ReleaseIntegrityError("Release manifest full-inventory size is invalid.")
    if size > RELEASE_FULL_INVENTORY_MAX_BYTES:
        raise ReleaseIntegrityError("Release manifest full-inventory size exceeds the limit.")
    if isinstance(entries, bool) or not isinstance(entries, int) or entries <= 0:
        raise ReleaseIntegrityError("Release manifest full-inventory entry count is invalid.")
    return _FullInventoryDescriptor(
        path=path,
        size=size,
        sha256=_parse_sha256(raw.get("sha256"), field="Full inventory"),
        entries=entries,
    )


def _parse_manifest(
    manifest: dict[str, Any],
    *,
    manifest_relative_path: str,
    expected_platform: str | None,
    expected_architecture: str | None,
) -> tuple[dict[str, _ReleaseEntry], tuple[str, ...], _FullInventoryDescriptor]:
    if manifest.get("schema_version") != RELEASE_MANIFEST_SCHEMA_VERSION:
        raise ReleaseIntegrityError(
            f"Release manifest must use schema {RELEASE_MANIFEST_SCHEMA_VERSION}."
        )
    allowed_fields = {
        "schema_version",
        "artifact_kind",
        "product_version",
        "platform",
        "architecture",
        "minimum_os",
        "runtimes",
        "signing",
        "startup_critical_entries",
        "full_inventory",
    }
    if set(manifest) != allowed_fields:
        raise ReleaseIntegrityError("Release manifest fields do not match schema 3.")
    for field in (
        "artifact_kind",
        "product_version",
        "platform",
        "architecture",
        "minimum_os",
    ):
        _require_nonempty_string(manifest.get(field), field=field)
    if expected_platform is not None and manifest.get("platform") != expected_platform:
        raise ReleaseIntegrityError("Release manifest platform does not match this launcher.")
    if (
        expected_architecture is not None
        and manifest.get("architecture") != expected_architecture
    ):
        raise ReleaseIntegrityError("Release manifest architecture does not match this launcher.")
    runtimes = manifest.get("runtimes")
    if not isinstance(runtimes, dict) or not runtimes:
        raise ReleaseIntegrityError("Release manifest requires runtime versions.")
    for name, version in runtimes.items():
        _require_nonempty_string(name, field="runtime name")
        _require_nonempty_string(version, field=f"runtime version for {name}")
    signing = manifest.get("signing")
    if not isinstance(signing, dict):
        raise ReleaseIntegrityError("Release manifest requires signing metadata.")
    _require_nonempty_string(signing.get("identity"), field="signing identity")
    if not isinstance(signing.get("notarized"), bool):
        raise ReleaseIntegrityError("Release manifest notarization state is invalid.")

    descriptor = _parse_full_inventory_descriptor(
        manifest.get("full_inventory"),
        manifest_relative_path=manifest_relative_path,
    )
    forbidden_paths = frozenset({manifest_relative_path, descriptor.path})
    critical_entries = _parse_release_entries(
        manifest.get("startup_critical_entries"),
        label="Startup manifest",
        forbidden_paths=forbidden_paths,
    )
    if len(critical_entries) > descriptor.entries:
        raise ReleaseIntegrityError(
            "Startup-critical entry count exceeds the full inventory entry count."
        )
    return critical_entries, tuple(critical_entries), descriptor


def _load_full_inventory(
    root: Path,
    *,
    manifest_relative_path: str,
    descriptor: _FullInventoryDescriptor,
    critical_entries: dict[str, _ReleaseEntry],
) -> dict[str, _ReleaseEntry]:
    inventory_path = _path_under_root(root, descriptor.path)
    if inventory_path.is_symlink():
        raise ReleaseIntegrityError("Full inventory must not be a symlink.")
    try:
        resolved_inventory = inventory_path.resolve(strict=True)
    except OSError as error:
        raise ReleaseIntegrityError("Full inventory is missing or unreadable.") from error
    if not resolved_inventory.is_relative_to(root):
        raise ReleaseIntegrityError("Full inventory is outside the release root.")
    payload, digest, size = _load_json_metadata(
        resolved_inventory,
        maximum_bytes=RELEASE_FULL_INVENTORY_MAX_BYTES,
        label="Full release inventory",
    )
    if size != descriptor.size or digest != descriptor.sha256:
        raise ReleaseIntegrityError("Full inventory does not match its release-manifest binding.")
    if set(payload) != {"schema_version", "files"}:
        raise ReleaseIntegrityError("Full inventory fields are invalid.")
    if payload.get("schema_version") != RELEASE_FULL_INVENTORY_SCHEMA_VERSION:
        raise ReleaseIntegrityError(
            f"Full inventory must use schema {RELEASE_FULL_INVENTORY_SCHEMA_VERSION}."
        )
    entries = _parse_release_entries(
        payload.get("files"),
        label="Full inventory",
        forbidden_paths=frozenset({manifest_relative_path, descriptor.path}),
    )
    if len(entries) != descriptor.entries:
        raise ReleaseIntegrityError("Full inventory entry count does not match its descriptor.")
    for relative, critical_entry in critical_entries.items():
        if entries.get(relative) != critical_entry:
            raise ReleaseIntegrityError(
                f"Startup-critical metadata differs from the full inventory: {relative}"
            )
    return entries


def _verify_entry(root: Path, entry: _ReleaseEntry) -> str:
    path = _path_under_root(root, entry.path)
    if entry.kind == "file":
        size, digest, executable = _stable_regular_file_metadata(path)
        if size != entry.size or digest != entry.sha256 or executable is not entry.executable:
            raise ReleaseIntegrityError(f"Release file integrity check failed: {entry.path}")
        return "file"
    try:
        is_symlink = path.is_symlink()
    except OSError as error:
        raise ReleaseIntegrityError(f"Cannot inspect release symlink: {entry.path}") from error
    if not is_symlink:
        raise ReleaseIntegrityError(f"Release symlink is missing or replaced: {entry.path}")
    target = _validated_symlink_target(root, path, entry.path)
    if target != entry.target:
        raise ReleaseIntegrityError(f"Release symlink target changed: {entry.path}")
    return "symlink"


def verify_release_manifest(
    app_root: Path,
    manifest_path: Path,
    *,
    mode: ReleaseVerificationMode = ReleaseVerificationMode.STARTUP,
    required_startup_paths: Iterable[str] = (),
    expected_platform: str | None = None,
    expected_architecture: str | None = None,
) -> ReleaseIntegrityReport:
    """Verify either the startup gate or every manifest-covered bundle entry.

    The platform signature is the trust anchor for ``release.json`` and its excluded signature
    metadata. Startup mode hashes only the declared critical subset; full mode additionally proves
    that the bundle contains neither missing nor unlisted files or symlinks.
    """

    try:
        root = app_root.resolve(strict=True)
    except OSError as error:
        raise ReleaseIntegrityError("Release root is missing or unreadable.") from error
    if not root.is_dir():
        raise ReleaseIntegrityError("Release root must be a directory.")
    if manifest_path.is_symlink():
        raise ReleaseIntegrityError("Release manifest must not be a symlink.")
    try:
        resolved_manifest = manifest_path.resolve(strict=True)
    except OSError as error:
        raise ReleaseIntegrityError("Release manifest is missing or unreadable.") from error
    if not resolved_manifest.is_relative_to(root):
        raise ReleaseIntegrityError("Release manifest is outside the release root.")
    manifest_relative_path = resolved_manifest.relative_to(root).as_posix()
    manifest_relative_path = _canonical_relative_path(
        manifest_relative_path,
        field="release manifest path",
    )
    manifest, manifest_digest, _manifest_size = _load_json_metadata(
        resolved_manifest,
        maximum_bytes=RELEASE_STARTUP_MANIFEST_MAX_BYTES,
        label="Release manifest",
    )
    critical_entries, critical, inventory_descriptor = _parse_manifest(
        manifest,
        manifest_relative_path=manifest_relative_path,
        expected_platform=expected_platform,
        expected_architecture=expected_architecture,
    )

    required = {
        _canonical_relative_path(path, field="required startup path")
        for path in required_startup_paths
    }
    missing_required = sorted(required.difference(critical))
    if missing_required:
        raise ReleaseIntegrityError(
            "Release manifest omitted required startup paths: " + ", ".join(missing_required)
        )

    verification_paths: tuple[str, ...]
    if mode is ReleaseVerificationMode.STARTUP:
        entries = critical_entries
        verification_paths = critical
    elif mode is ReleaseVerificationMode.FULL:
        entries = _load_full_inventory(
            root,
            manifest_relative_path=manifest_relative_path,
            descriptor=inventory_descriptor,
            critical_entries=critical_entries,
        )
        excluded_paths = frozenset(
            {manifest_relative_path, inventory_descriptor.path}
        )
        actual = {
            relative: kind
            for relative, _path, kind in _iter_release_nodes(
                root,
                excluded_relative_paths=excluded_paths,
            )
        }
        expected = {relative: entry.kind for relative, entry in entries.items()}
        if actual != expected:
            missing = sorted(set(expected).difference(actual))
            unexpected = sorted(set(actual).difference(expected))
            wrong_kind = sorted(
                relative
                for relative in set(actual).intersection(expected)
                if actual[relative] != expected[relative]
            )
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if unexpected:
                details.append("unexpected=" + ",".join(unexpected))
            if wrong_kind:
                details.append("kind_changed=" + ",".join(wrong_kind))
            raise ReleaseIntegrityError(
                "Release inventory does not match the bundle" + (": " + "; ".join(details))
            )
        verification_paths = tuple(entries)
    else:
        raise ReleaseIntegrityError(f"Unsupported release verification mode: {mode}")

    checked_files = 0
    checked_symlinks = 0
    for relative in verification_paths:
        kind = _verify_entry(root, entries[relative])
        if kind == "file":
            checked_files += 1
        else:
            checked_symlinks += 1
    return ReleaseIntegrityReport(
        mode=mode,
        schema_version=RELEASE_MANIFEST_SCHEMA_VERSION,
        manifest_sha256=manifest_digest,
        total_entries=inventory_descriptor.entries,
        checked_files=checked_files,
        checked_symlinks=checked_symlinks,
        unchecked_entries=inventory_descriptor.entries - len(verification_paths),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="academatepaper-release-integrity",
        description="Verify a signed release manifest against its relocatable bundle.",
    )
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in ReleaseVerificationMode),
        default=ReleaseVerificationMode.STARTUP.value,
    )
    parser.add_argument("--require-startup-path", action="append", default=[])
    parser.add_argument("--expected-platform")
    parser.add_argument("--expected-architecture")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = verify_release_manifest(
            arguments.app_root,
            arguments.manifest,
            mode=ReleaseVerificationMode(arguments.mode),
            required_startup_paths=arguments.require_startup_path,
            expected_platform=arguments.expected_platform,
            expected_architecture=arguments.expected_architecture,
        )
    except ReleaseIntegrityError as error:
        if arguments.json:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "mode": arguments.mode,
                        "reason": str(error),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"Release integrity verification failed: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(report.as_dict(), sort_keys=True))
    else:
        print(
            "Release integrity verification passed: "
            f"mode={report.mode.value} checked_files={report.checked_files} "
            f"checked_symlinks={report.checked_symlinks} "
            f"unchecked_entries={report.unchecked_entries}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
