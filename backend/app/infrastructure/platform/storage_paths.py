from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import unquote


class StoragePathMappingError(ValueError):
    pass


class StorageKeyMapper(Protocol):
    @property
    def root(self) -> Path: ...

    def resolve(self, key: str) -> Path: ...

    def key_for_path(self, path: Path) -> str: ...


def _validated_parts(key: str) -> tuple[str, ...]:
    if "\\" in key or any(ord(character) < 32 for character in key):
        raise StoragePathMappingError(f"Invalid POSIX storage key: {key!r}")
    pure_key = PurePosixPath(key)
    if pure_key.is_absolute() or ".." in pure_key.parts or not pure_key.parts:
        raise StoragePathMappingError(f"Invalid POSIX storage key: {key!r}")
    return pure_key.parts


class PosixStorageKeyMapper:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, key: str) -> Path:
        resolved = (self._root / Path(*_validated_parts(key))).resolve()
        if not resolved.is_relative_to(self._root):
            raise StoragePathMappingError(f"Storage key escapes root: {key!r}")
        return resolved

    def key_for_path(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self._root)
        except ValueError as error:
            raise StoragePathMappingError(f"Storage path escapes root: {path}") from error
        return PurePosixPath(*relative.parts).as_posix()


_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_WINDOWS_SEGMENT_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def _windows_encode_segment(segment: str) -> str:
    # Encoding every non-lowercase-ASCII byte avoids case-insensitive Windows collisions while
    # retaining a reversible mapping for Unicode, spaces, percent signs, and forbidden names.
    encoded = "".join(
        character
        if character in _WINDOWS_SEGMENT_SAFE
        else "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
        for character in segment
    )
    if (
        segment.partition(".")[0].upper() in _WINDOWS_RESERVED_NAMES
        and not encoded.startswith("%")
    ):
        encoded = f"%{ord(segment[0]):02X}{encoded[1:]}"
    return encoded


def _windows_decode_segment(segment: str) -> str:
    return unquote(segment, encoding="utf-8", errors="strict")


class WindowsStorageKeyMapper:
    """Reversible mapping from POSIX storage keys to Windows-safe path segments.

    Python's native Windows path layer is kept in charge of long-path prefixes.  The mapper never
    truncates a key or imposes the historical MAX_PATH limit; the native packaging matrix must run
    the resulting >260-character path against the selected Windows minimum.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, key: str) -> Path:
        parts = tuple(_windows_encode_segment(part) for part in _validated_parts(key))
        resolved = (self._root / Path(*parts)).resolve()
        if not resolved.is_relative_to(self._root):
            raise StoragePathMappingError(f"Storage key escapes root: {key!r}")
        return resolved

    def key_for_path(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self._root)
        except ValueError as error:
            raise StoragePathMappingError(f"Storage path escapes root: {path}") from error
        return PurePosixPath(*(_windows_decode_segment(part) for part in relative.parts)).as_posix()
