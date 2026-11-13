from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, TypeVar

from app.domain.os_adapters import FilesystemOperationError
from app.infrastructure.platform.filesystem import classify_filesystem_error
from app.infrastructure.platform.storage_paths import (
    PosixStorageKeyMapper,
    StorageKeyMapper,
    StoragePathMappingError,
)

_MutationResult = TypeVar("_MutationResult")
MutationGuard = Callable[[Callable[[], _MutationResult]], _MutationResult]


class InvalidStorageKey(ValueError):
    pass


class LocalFileStorage:
    def __init__(
        self,
        root: Path,
        *,
        path_mapper: StorageKeyMapper | None = None,
        mutation_guard: MutationGuard | None = None,
    ) -> None:
        self._path_mapper = path_mapper or PosixStorageKeyMapper(root)
        self._mutation_guard = mutation_guard
        self.root = self._path_mapper.root

    def with_mutation_guard(self, guard: MutationGuard) -> LocalFileStorage:
        return LocalFileStorage(
            self.root,
            path_mapper=self._path_mapper,
            mutation_guard=guard,
        )

    def _mutate(self, mutation: Callable[[], _MutationResult]) -> _MutationResult:
        if self._mutation_guard is not None:
            return self._mutation_guard(mutation)
        return mutation()

    def initialize(self) -> None:
        for child in ("database", "papers", "exports", "tmp"):
            path = self.root / child
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise classify_filesystem_error(
                    error,
                    operation="initialize storage directory",
                    path=path,
                ) from error

    def _resolve(self, key: str) -> Path:
        try:
            return self._path_mapper.resolve(key)
        except StoragePathMappingError as error:
            raise InvalidStorageKey(str(error)) from error

    def put(self, key: str, stream: BinaryIO, metadata: dict[str, str] | None = None) -> None:
        del metadata
        destination = self._resolve(key)
        temporary_path: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                shutil.copyfileobj(stream, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            # Windows cannot replace an open NamedTemporaryFile.  Close it before the atomic
            # rename on every platform so Local storage follows one OS-neutral write path.
            self._mutate(lambda: temporary_path.replace(destination))
        except BaseException as error:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
            if isinstance(error, OSError) and not isinstance(error, FilesystemOperationError):
                raise classify_filesystem_error(
                    error,
                    operation="write storage object",
                    path=destination,
                ) from error
            raise

    def get(self, key: str) -> BinaryIO:
        path = self._resolve(key)
        try:
            return path.open("rb")
        except OSError as error:
            raise classify_filesystem_error(
                error,
                operation="read storage object",
                path=path,
            ) from error

    def exists(self, key: str) -> bool:
        path = self._resolve(key)
        try:
            return path.is_file()
        except OSError as error:
            raise classify_filesystem_error(
                error,
                operation="inspect storage object",
                path=path,
            ) from error

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        try:
            self._mutate(lambda: path.unlink(missing_ok=True))
        except OSError as error:
            raise classify_filesystem_error(
                error,
                operation="delete storage object",
                path=path,
            ) from error

    def delete_prefix(self, prefix: str) -> int:
        target = self._resolve(prefix)
        detached = target.with_name(f".{target.name}.deleting-{uuid.uuid4().hex}")
        try:
            # Detach the whole namespace atomically while the claim is fenced.  Counting and
            # recursively removing the private detached tree can then happen outside the database
            # critical section without ever touching files published by a replacement claim.
            moved = self._mutate(
                lambda: target.replace(detached) if target.exists() else None
            )
            if moved is None:
                return 0
            if detached.is_file():
                detached.unlink()
                return 1
            deleted_files = sum(1 for path in detached.rglob("*") if path.is_file())
            shutil.rmtree(detached)
            return deleted_files
        except OSError as error:
            raise classify_filesystem_error(
                error,
                operation="delete storage prefix",
                path=target,
            ) from error

    def list(self, prefix: str = "") -> list[str]:
        base = self.root if not prefix else self._resolve(prefix)
        try:
            if not base.exists():
                return []
            paths = [base] if base.is_file() else base.rglob("*")
            return sorted(
                self._path_mapper.key_for_path(path) for path in paths if path.is_file()
            )
        except OSError as error:
            raise classify_filesystem_error(
                error,
                operation="list storage prefix",
                path=base,
            ) from error

    def internal_url(self, key: str) -> str:
        self._resolve(key)
        return f"/api/v1/assets/{key}"

    def probe_writable(self) -> None:
        self.initialize()
        path = self.root / "tmp"
        try:
            with tempfile.NamedTemporaryFile(dir=path) as probe:
                probe.write(b"ok")
                probe.flush()
        except OSError as error:
            raise classify_filesystem_error(
                error,
                operation="probe storage write",
                path=path,
            ) from error
