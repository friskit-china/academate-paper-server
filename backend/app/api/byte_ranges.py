from __future__ import annotations

import re
from collections.abc import Iterator
from typing import BinaryIO

from fastapi import Request
from starlette.responses import Response, StreamingResponse

from app.domain.ports import Storage

_SINGLE_BYTE_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CHUNK_SIZE = 64 * 1024


def _requested_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    match = _SINGLE_BYTE_RANGE.fullmatch(value.strip())
    if match is None or "," in value or size < 1:
        raise ValueError("unsatisfiable byte range")
    raw_start, raw_end = match.groups()
    if not raw_start and not raw_end:
        raise ValueError("unsatisfiable byte range")
    if raw_start:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
        if start >= size or end < start:
            raise ValueError("unsatisfiable byte range")
        return start, min(end, size - 1)
    suffix_length = int(raw_end)
    if suffix_length < 1:
        raise ValueError("unsatisfiable byte range")
    return max(0, size - suffix_length), size - 1


def _bounded_chunks(stream: BinaryIO, *, start: int, length: int) -> Iterator[bytes]:
    try:
        stream.seek(start)
        remaining = length
        while remaining:
            chunk = stream.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()


def storage_byte_response(
    request: Request,
    *,
    storage: Storage,
    storage_key: str,
    byte_size: int,
    media_type: str,
    headers: dict[str, str] | None = None,
) -> Response:
    """Serve one storage object with bounded single-range and HEAD semantics."""

    if byte_size < 0:
        raise ValueError("byte_size must not be negative")
    response_headers = dict(headers or {})
    response_headers["Accept-Ranges"] = "bytes"
    try:
        selected = _requested_range(request.headers.get("range"), byte_size)
    except ValueError:
        response_headers["Content-Range"] = f"bytes */{byte_size}"
        response_headers["Content-Length"] = "0"
        return Response(status_code=416, headers=response_headers, media_type=media_type)

    if selected is None:
        start = 0
        end = max(0, byte_size - 1)
        status_code = 200
    else:
        start, end = selected
        status_code = 206
        response_headers["Content-Range"] = f"bytes {start}-{end}/{byte_size}"
    length = byte_size if selected is None else end - start + 1
    response_headers["Content-Length"] = str(length)
    if request.method == "HEAD" or length == 0:
        return Response(status_code=status_code, headers=response_headers, media_type=media_type)

    stream = storage.get(storage_key)
    return StreamingResponse(
        _bounded_chunks(stream, start=start, length=length),
        status_code=status_code,
        media_type=media_type,
        headers=response_headers,
    )
