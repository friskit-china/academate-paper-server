from __future__ import annotations

import errno
from pathlib import Path

from app.domain.os_adapters import (
    FileFailureKind,
    FilesystemOperationError,
)

_WINDOWS_ACCESS_DENIED = 5
_WINDOWS_WRITE_PROTECT = 19
_WINDOWS_DISK_FULL = 112
_WINDOWS_FILENAME_EXCEEDED = 206


def classify_filesystem_error(
    error: OSError,
    *,
    operation: str,
    path: Path,
) -> FilesystemOperationError:
    winerror = getattr(error, "winerror", None)
    if error.errno in {errno.EACCES, errno.EPERM} or winerror == _WINDOWS_ACCESS_DENIED:
        kind = FileFailureKind.PERMISSION_DENIED
    elif error.errno == errno.EROFS or winerror == _WINDOWS_WRITE_PROTECT:
        kind = FileFailureKind.READ_ONLY_FILESYSTEM
    elif (
        error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
        or winerror == _WINDOWS_DISK_FULL
    ):
        kind = FileFailureKind.DISK_FULL
    elif error.errno == errno.ENAMETOOLONG or winerror == _WINDOWS_FILENAME_EXCEEDED:
        kind = FileFailureKind.PATH_TOO_LONG
    else:
        kind = FileFailureKind.IO_ERROR
    return FilesystemOperationError(kind=kind, operation=operation, path=path)
