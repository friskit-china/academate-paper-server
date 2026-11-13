from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import json
import lzma
import mimetypes
import tarfile
import tempfile
from pathlib import PurePosixPath
from typing import Any

from app.domain.ports import Storage


class ArchiveSafetyError(RuntimeError):
    pass


class ArchiveIntegrityError(RuntimeError):
    pass


_COMPRESSED_ARCHIVE_MAGICS = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")


def load_validated_source_manifest(
    storage: Storage,
    *,
    paper_id: str,
    manifest_key: str,
    required: bool = True,
) -> dict[str, Any] | None:
    """Load one paper's canonical expanded-source manifest without crossing its namespace."""

    expected_manifest_key = f"papers/{paper_id}/derived/source-manifest.json"
    if manifest_key != expected_manifest_key:
        raise ArchiveSafetyError(
            "The expanded-source manifest is outside the current paper storage scope."
        )
    if not storage.exists(manifest_key):
        if not required:
            return None
        raise ArchiveIntegrityError("The expanded-source manifest is unavailable.")

    try:
        with storage.get(manifest_key) as stream:
            manifest = json.load(stream)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveIntegrityError("The expanded-source manifest is invalid JSON.") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ArchiveIntegrityError("The expanded-source manifest has an invalid file list.")

    expected_source_prefix = f"papers/{paper_id}/source/expanded/"
    seen_paths: set[str] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise ArchiveIntegrityError(
                "The expanded-source manifest contains an invalid file entry."
            )
        raw_path = item.get("path")
        storage_key = item.get("storage_key")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or any(ord(character) < 32 for character in raw_path)
        ):
            raise ArchiveSafetyError(
                "The expanded-source manifest contains an unsafe file path."
            )
        try:
            normalized_path = _safe_name(raw_path)
        except ArchiveSafetyError as error:
            raise ArchiveSafetyError(
                "The expanded-source manifest contains an unsafe file path."
            ) from error
        if normalized_path != raw_path or normalized_path in seen_paths:
            raise ArchiveSafetyError(
                "The expanded-source manifest contains a non-canonical file path."
            )
        if not isinstance(storage_key, str) or storage_key != (
            f"{expected_source_prefix}{normalized_path}"
        ):
            raise ArchiveSafetyError(
                "The expanded-source manifest file is outside the current paper storage scope."
            )
        seen_paths.add(normalized_path)
    return manifest


def _decompress_single_source(data: bytes, *, max_file_bytes: int) -> bytes:
    if data.startswith(b"\x1f\x8b"):
        stream = gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb")  # noqa: SIM115
    elif data.startswith(b"BZh"):
        stream = bz2.BZ2File(io.BytesIO(data), mode="rb")  # noqa: SIM115
    elif data.startswith(b"\xfd7zXZ\x00"):
        stream = lzma.LZMAFile(io.BytesIO(data), mode="rb")  # noqa: SIM115
    else:
        raise ArchiveIntegrityError("The compressed source archive is incomplete or corrupt.")

    chunks: list[bytes] = []
    total = 0
    try:
        with stream:
            while chunk := stream.read(min(64 * 1024, max_file_bytes + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > max_file_bytes:
                    raise ArchiveSafetyError(
                        "The decompressed single-file source exceeds the file size limit."
                    )
    except ArchiveSafetyError:
        raise
    except (EOFError, OSError, lzma.LZMAError) as error:
        raise ArchiveIntegrityError(
            "The compressed source archive is incomplete or corrupt."
        ) from error
    content = b"".join(chunks)
    if not content or b"\x00" in content[:4096]:
        raise ArchiveIntegrityError(
            "The compressed arXiv response is neither a tar archive nor a text source file."
        )
    return content


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        raise ArchiveSafetyError(f"Unsafe archive path: {name!r}")
    normalized = PurePosixPath(*[part for part in path.parts if part not in {"", "."}])
    if not normalized.parts:
        raise ArchiveSafetyError(f"Unsafe archive path: {name!r}")
    return normalized.as_posix()


def _single_file_manifest(data: bytes, storage: Storage, prefix: str) -> dict[str, Any]:
    if b"\x00" in data[:4096]:
        raise ArchiveSafetyError(
            "The arXiv response is neither a tar archive nor a text source file."
        )
    key = f"{prefix}/main.tex"
    storage.put(key, io.BytesIO(data))
    return {
        "format": "single-file",
        "file_count": 1,
        "total_bytes": len(data),
        "files": [
            {
                "path": "main.tex",
                "storage_key": key,
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mime_type": "text/x-tex",
            }
        ],
    }


def extract_source_archive(
    data: bytes,
    storage: Storage,
    prefix: str,
    *,
    max_files: int = 2_000,
    max_total_bytes: int = 250_000_000,
    max_file_bytes: int = 50_000_000,
) -> dict[str, Any]:
    """Validate every tar member before atomically storing regular files."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")  # noqa: SIM115
    except (tarfile.ReadError, EOFError, OSError):
        if data.startswith(_COMPRESSED_ARCHIVE_MAGICS):
            single_source = _decompress_single_source(data, max_file_bytes=max_file_bytes)
            return _single_file_manifest(single_source, storage, prefix)
        return _single_file_manifest(data, storage, prefix)

    with archive:
        try:
            members = archive.getmembers()
        except (tarfile.ReadError, EOFError, OSError) as error:
            raise ArchiveIntegrityError(
                "The compressed source archive is incomplete or corrupt."
            ) from error
        files: list[tuple[tarfile.TarInfo, str]] = []
        seen: set[str] = set()
        total = 0
        for member in members:
            name = _safe_name(member.name)
            if member.isdir():
                continue
            if not member.isfile():
                raise ArchiveSafetyError(f"Archive contains a non-regular file: {member.name!r}")
            if name in seen:
                raise ArchiveSafetyError(f"Archive contains a duplicate path: {name!r}")
            if member.size < 0 or member.size > max_file_bytes:
                raise ArchiveSafetyError(f"Archive member exceeds the file size limit: {name!r}")
            seen.add(name)
            files.append((member, name))
            total += member.size
            if len(files) > max_files or total > max_total_bytes:
                raise ArchiveSafetyError("Archive exceeds the configured extraction limits.")

        if not files:
            raise ArchiveSafetyError("The source archive contains no regular files.")

        manifest_files: list[dict[str, Any]] = []
        for member, name in files:
            source = archive.extractfile(member)
            if source is None:
                raise ArchiveSafetyError(f"Could not read archive member: {name!r}")
            digest = hashlib.sha256()
            size = 0
            with source, tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as temporary:
                try:
                    while chunk := source.read(64 * 1024):
                        size += len(chunk)
                        if size > member.size or size > max_file_bytes:
                            raise ArchiveSafetyError(
                                f"Archive member expanded unexpectedly: {name!r}"
                            )
                        digest.update(chunk)
                        temporary.write(chunk)
                except (tarfile.ReadError, EOFError, OSError) as error:
                    raise ArchiveIntegrityError(
                        "The compressed source archive is incomplete or corrupt."
                    ) from error
                if size != member.size:
                    raise ArchiveSafetyError(
                        f"Archive member size did not match its header: {name!r}"
                    )
                temporary.seek(0)
                storage_key = f"{prefix}/{name}"
                storage.put(storage_key, temporary)
            mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            manifest_files.append(
                {
                    "path": name,
                    "storage_key": storage_key,
                    "byte_size": size,
                    "sha256": digest.hexdigest(),
                    "mime_type": mime_type,
                }
            )

    return {
        "format": "tar",
        "file_count": len(manifest_files),
        "total_bytes": total,
        "files": manifest_files,
    }
