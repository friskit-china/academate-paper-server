from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

INSTALLATION_ID_FILENAME = "installation-id"
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_MAX_IDENTITY_BYTES = 64


class InstallationIdentityError(RuntimeError):
    """The stable native installation identity cannot be safely created or reused."""


class MacOSInstallationIdentityStore:
    """Persist one stable UUID in a private native configuration directory.

    A fully written 0600 temporary file is linked into place with create-if-absent semantics. A
    concurrent creator can win without either process overwriting the established identity.
    """

    def __init__(
        self,
        configuration_root: Path,
        *,
        identity_factory: Callable[[], UUID] = uuid4,
        platform_name: str = sys.platform,
    ) -> None:
        self._configuration_root = configuration_root
        self._identity_factory = identity_factory
        self._platform_name = platform_name
        self._path = configuration_root / INSTALLATION_ID_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load_or_create(self) -> UUID:
        if self._platform_name != "darwin":
            raise InstallationIdentityError(
                f"The macOS installation identity is unsupported on {self._platform_name}."
            )
        self._prepare_private_directory()
        try:
            return self._read_existing()
        except FileNotFoundError:
            pass

        identity = self._identity_factory()
        if not isinstance(identity, UUID):
            raise InstallationIdentityError("Installation identity generator returned no UUID.")
        encoded = f"{identity}\n".encode("ascii")
        temporary = self._configuration_root / f".{INSTALLATION_ID_FILENAME}.{uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                _FILE_MODE,
            )
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.chmod(temporary, _FILE_MODE, follow_symlinks=False)
            try:
                os.link(temporary, self._path, follow_symlinks=False)
            except FileExistsError:
                # Another trusted Supervisor won the create-if-absent race. Reuse its fully
                # published identity; never replace it with this process' candidate.
                return self._read_existing()
            _fsync_directory(self._configuration_root)
            return self._read_existing()
        except InstallationIdentityError:
            raise
        except OSError as error:
            raise InstallationIdentityError(
                "Could not create the private native installation identity."
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _prepare_private_directory(self) -> None:
        try:
            self._configuration_root.mkdir(
                mode=_DIRECTORY_MODE,
                parents=True,
                exist_ok=True,
            )
            directory_status = self._configuration_root.lstat()
            if not stat.S_ISDIR(directory_status.st_mode) or stat.S_ISLNK(
                directory_status.st_mode
            ):
                raise InstallationIdentityError(
                    "Native configuration root must be a real directory."
                )
            if directory_status.st_uid != os.geteuid():
                raise InstallationIdentityError(
                    "Native configuration root must be owned by the current user."
                )
            os.chmod(self._configuration_root, _DIRECTORY_MODE)
            if stat.S_IMODE(self._configuration_root.stat().st_mode) != _DIRECTORY_MODE:
                raise InstallationIdentityError(
                    "Native configuration root permissions are not private."
                )
        except InstallationIdentityError:
            raise
        except OSError as error:
            raise InstallationIdentityError(
                "Could not prepare the private native configuration directory."
            ) from error

    def _read_existing(self) -> UUID:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise InstallationIdentityError(
                "Could not open the native installation identity."
            ) from error
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid():
                raise InstallationIdentityError(
                    "Native installation identity must be a current-user regular file."
                )
            os.fchmod(descriptor, _FILE_MODE)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != _FILE_MODE:
                raise InstallationIdentityError(
                    "Native installation identity permissions are not private."
                )
            raw = os.read(descriptor, _MAX_IDENTITY_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_IDENTITY_BYTES:
            raise InstallationIdentityError("Native installation identity is invalid.")
        try:
            text = raw.decode("ascii").strip()
            identity = UUID(text)
        except (UnicodeDecodeError, ValueError) as error:
            raise InstallationIdentityError("Native installation identity is invalid.") from error
        if str(identity) != text:
            raise InstallationIdentityError("Native installation identity is not canonical.")
        return identity


def _write_all(descriptor: int, value: bytes) -> None:
    written = 0
    while written < len(value):
        count = os.write(descriptor, value[written:])
        if count <= 0:
            raise OSError("short write while persisting installation identity")
        written += count


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
