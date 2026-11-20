"""Bounded shared cache for arXiv's exact upstream revision metadata."""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import monotonic, sleep

from app.domain.arxiv import arxiv_version_number
from app.infrastructure.arxiv.client import ArxivClientError

_CACHE_TTL_SECONDS = 24 * 60 * 60
_IMPORT_CACHE_TTL_SECONDS = 5 * 60
_MAX_CACHED_WORKS = 1024
_REQUEST_INTERVAL_SECONDS = 3.2
_TRANSIENT_BACKOFF_SECONDS = 30


@dataclass(frozen=True, slots=True)
class CheckedArxivVersion:
    version: str
    checked_at: datetime


class ArxivVersionIndex:
    def __init__(self, fetch: Callable[[str], str]) -> None:
        self._fetch = fetch
        self._entries: OrderedDict[str, tuple[CheckedArxivVersion, float]] = OrderedDict()
        self._cache_lock = Lock()
        self._request_lock = Lock()
        self._next_request = 0.0

    def lookup(
        self, arxiv_id: str, *, check: bool = False, require_fresh: bool = False,
    ) -> CheckedArxivVersion | None:
        key = arxiv_id.casefold()
        valid_for = _IMPORT_CACHE_TTL_SECONDS if require_fresh else _CACHE_TTL_SECONDS

        def cached() -> tuple[CheckedArxivVersion | None, bool]:
            with self._cache_lock:
                entry = self._entries.get(key)
                if entry is None:
                    return None, False
                self._entries.move_to_end(key)
            return entry[0], monotonic() - entry[1] < valid_for

        previous, fresh = cached()
        if not check or fresh:
            return previous
        # Reading a catalog can return immediately if another check is active. Imports
        # briefly wait for an in-flight result, so simultaneous requests share a lookup.
        acquired = (
            self._request_lock.acquire(timeout=6)
            if require_fresh
            else self._request_lock.acquire(blocking=False)
        )
        if not acquired:
            previous, fresh = cached()
            return previous if fresh or not require_fresh else None
        try:
            previous, fresh = cached()
            if fresh:
                return previous
            delay = self._next_request - monotonic()
            if delay > 0:
                if not require_fresh or delay > _REQUEST_INTERVAL_SECONDS:
                    return None if require_fresh else previous
                sleep(delay)

            self._next_request = monotonic() + _REQUEST_INTERVAL_SECONDS
            try:
                version = self._fetch(arxiv_id)
                if not arxiv_version_number(version):
                    raise ArxivClientError("arXiv returned an invalid version.")
            except ArxivClientError as error:
                backoff = min(300, error.retry_after_seconds or _TRANSIENT_BACKOFF_SECONDS)
                self._next_request = max(self._next_request, monotonic() + backoff)
                return None if require_fresh else previous

            if previous and arxiv_version_number(previous.version) > arxiv_version_number(version):
                version = previous.version
            result = CheckedArxivVersion(version=version, checked_at=datetime.now(UTC))
            with self._cache_lock:
                self._entries[key] = (result, monotonic())
                if len(self._entries) > _MAX_CACHED_WORKS:
                    self._entries.popitem(last=False)
            return result
        finally:
            self._request_lock.release()
