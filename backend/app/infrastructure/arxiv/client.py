from __future__ import annotations

import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from math import ceil
from urllib.parse import urlparse

from app.domain.arxiv import ArxivIdentifier


class ArxivClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after_seconds: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class ArxivResumeError(ArxivClientError):
    """The persisted partial file cannot safely be attached to the current response."""


class ArxivSourceUnavailableError(ArxivClientError):
    """arXiv served a presentation artifact instead of the submitted TeX sources."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True, slots=True)
class ArxivMetadata:
    title: str
    abstract: str
    submitted_at: datetime | None
    authors: list[str]
    canonical_url: str


@dataclass(frozen=True, slots=True)
class DownloadedSource:
    content: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class SourceDownloadProgress:
    bytes_downloaded: int
    bytes_total: int | None
    resumed_bytes: int
    source_identity: str | None


SourceProgressCallback = Callable[[SourceDownloadProgress, bytes, bool], None]


_ALLOWED_RESPONSE_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_ATOM = "{http://www.w3.org/2005/Atom}"
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)", re.IGNORECASE)
_SOURCE_RANGE_CHUNK_BYTES = 1024 * 1024


class _ArxivMetadataHTMLParser(HTMLParser):
    """Read arXiv's standard citation metadata without interpreting page markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attributes = {name.lower(): value for name, value in attrs if value is not None}
        name = (attributes.get("name") or attributes.get("property") or "").lower()
        content = attributes.get("content", "").strip()
        if name.startswith("citation_") and content:
            self.values.setdefault(name, []).append(content)


class ArxivClient:
    def __init__(
        self,
        *,
        api_base: str,
        source_base: str,
        abstract_base: str = "https://arxiv.org/abs",
        user_agent: str,
        timeout_seconds: int = 45,
        metadata_max_bytes: int = 2_000_000,
        source_max_bytes: int = 50_000_000,
        source_range_chunk_bytes: int = _SOURCE_RANGE_CHUNK_BYTES,
        source_range_retry_attempts: int = 4,
        source_range_delay_seconds: float = 4.0,
        metadata_retry_attempts: int = 1,
        metadata_request_interval_seconds: float = 3.2,
        metadata_retry_base_seconds: float = 5.0,
        metadata_api_cooldown_seconds: float = 300.0,
    ) -> None:
        self.api_base = api_base.rstrip("?")
        self.source_base = source_base.rstrip("/")
        self.abstract_base = abstract_base.rstrip("/")
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.metadata_max_bytes = metadata_max_bytes
        self.source_max_bytes = source_max_bytes
        self.source_range_chunk_bytes = source_range_chunk_bytes
        self.source_range_retry_attempts = source_range_retry_attempts
        self.source_range_delay_seconds = source_range_delay_seconds
        self.metadata_retry_attempts = max(1, metadata_retry_attempts)
        self.metadata_request_interval_seconds = max(0.0, metadata_request_interval_seconds)
        self.metadata_retry_base_seconds = max(0.0, metadata_retry_base_seconds)
        self.metadata_api_cooldown_seconds = max(0.0, metadata_api_cooldown_seconds)
        self._last_metadata_request_at: float | None = None
        self._metadata_api_cooldown_until = 0.0

    @staticmethod
    def _retry_after_seconds(headers: object, *, fallback: float) -> float:
        value = headers.get("Retry-After")  # type: ignore[attr-defined]
        if value:
            stripped = value.strip()
            try:
                return min(300.0, max(0.0, float(stripped)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(stripped)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    return min(
                        300.0,
                        max(0.0, (retry_at - datetime.now(UTC)).total_seconds()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(300.0, max(0.0, fallback))

    def _wait_for_metadata_request(self, retry_delay_seconds: float = 0.0) -> None:
        now = time.monotonic()
        pacing_delay = 0.0
        if self._last_metadata_request_at is not None:
            elapsed = now - self._last_metadata_request_at
            pacing_delay = max(0.0, self.metadata_request_interval_seconds - elapsed)
        delay = max(pacing_delay, retry_delay_seconds)
        if delay > 0:
            time.sleep(delay)
        self._last_metadata_request_at = time.monotonic()

    @staticmethod
    def _declared_size(response: object) -> int | None:
        value = response.headers.get("Content-Length")  # type: ignore[attr-defined]
        if not value:
            return None
        try:
            size = int(value)
        except ValueError as error:
            raise ArxivClientError("arXiv returned an invalid Content-Length header.") from error
        if size < 0:
            raise ArxivClientError("arXiv returned an invalid Content-Length header.")
        return size

    @staticmethod
    def _validate_response_host(response: object) -> None:
        final_host = (urlparse(response.geturl()).hostname or "").lower()  # type: ignore[attr-defined]
        if final_host not in _ALLOWED_RESPONSE_HOSTS:
            raise ArxivClientError("arXiv redirected to an unexpected host.")

    @staticmethod
    def _network_error(error: BaseException) -> ArxivClientError:
        reason = error.reason if isinstance(error, urllib.error.URLError) else str(error)
        return ArxivClientError(f"Could not reach arXiv: {reason!s}")

    def _read_response(
        self,
        response: object,
        max_bytes: int,
        *,
        expected_bytes: int | None = None,
    ) -> bytes:
        declared_size = self._declared_size(response)
        if declared_size is not None and declared_size > max_bytes:
            raise ArxivClientError("arXiv response exceeds the configured size limit.")
        if expected_bytes is not None and declared_size not in {None, expected_bytes}:
            raise ArxivClientError("arXiv returned an inconsistent partial response size.")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes + 1 - total))  # type: ignore[attr-defined]
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ArxivClientError("arXiv response exceeds the configured size limit.")

        required_size = expected_bytes if expected_bytes is not None else declared_size
        if required_size is not None and total != required_size:
            raise ArxivClientError(
                f"arXiv response ended early ({total} of {required_size} bytes received)."
            )
        return b"".join(chunks)

    def _download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "*/*"},
        )
        retry_delay_seconds = 0.0
        last_error: ArxivClientError | None = None
        for attempt in range(1, self.metadata_retry_attempts + 1):
            self._wait_for_metadata_request(retry_delay_seconds)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    self._validate_response_host(response)
                    content = self._read_response(response, max_bytes)
                    return content, response.headers.get_content_type()
            except ArxivClientError:
                # A response that reached us but failed validation is deterministic;
                # retrying it only repeats an invalid or unsafe response.
                raise
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise ArxivClientError(
                        "The requested arXiv item or source was not found.",
                        retryable=False,
                        status_code=error.code,
                    ) from error
                if error.code != 429 and error.code < 500:
                    raise ArxivClientError(
                        f"arXiv returned HTTP {error.code}.",
                        retryable=False,
                        status_code=error.code,
                    ) from error
                fallback = self.metadata_retry_base_seconds * (2 ** (attempt - 1))
                retry_delay_seconds = self._retry_after_seconds(
                    error.headers,
                    fallback=fallback,
                )
                last_error = ArxivClientError(
                    f"arXiv returned HTTP {error.code}.",
                    retry_after_seconds=ceil(retry_delay_seconds),
                    status_code=error.code,
                )
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                ConnectionError,
                EOFError,
            ) as error:
                retry_delay_seconds = min(
                    300.0,
                    self.metadata_retry_base_seconds * (2 ** (attempt - 1)),
                )
                last_error = ArxivClientError(
                    str(self._network_error(error)),
                    retry_after_seconds=ceil(retry_delay_seconds),
                )

            if attempt == self.metadata_retry_attempts:
                assert last_error is not None
                raise last_error

        raise AssertionError("metadata retry loop terminated unexpectedly")

    @staticmethod
    def _interrupted_message(
        error: ArxivClientError,
        *,
        bytes_downloaded: int,
        bytes_total: int | None,
    ) -> ArxivClientError:
        if bytes_total is None:
            progress = f"{bytes_downloaded} bytes downloaded"
        else:
            remaining = max(0, bytes_total - bytes_downloaded)
            progress = (
                f"{bytes_downloaded} of {bytes_total} bytes downloaded; "
                f"{remaining} bytes remaining"
            )
        return ArxivClientError(
            f"{error} Download paused with {progress}; the verified partial file was kept "
            "for the next retry."
        )

    def _download_source(
        self,
        url: str,
        *,
        partial_content: bytes = b"",
        expected_total: int | None = None,
        expected_identity: str | None = None,
        progress_callback: SourceProgressCallback | None = None,
    ) -> tuple[bytes, str]:
        """Download source archives in bounded ranges.

        Some network paths close long arXiv responses after exactly 5 MiB while
        retaining the original Content-Length. Rate-limited one-MiB range requests
        avoid that transport limit and let us verify every byte before extraction.
        """
        if len(partial_content) > self.source_max_bytes:
            raise ArxivResumeError(
                "The saved partial source exceeds the configured size limit."
            )
        if expected_total is not None and len(partial_content) > expected_total:
            raise ArxivResumeError("The saved partial source is inconsistent with arXiv.")

        chunks: list[bytes] = [partial_content] if partial_content else []
        total_size: int | None = expected_total
        offset = len(partial_content)
        resumed_bytes = offset
        content_type = "application/octet-stream"
        download_url = url
        source_identity: str | None = expected_identity

        while total_size is None or offset < total_size:
            requested_end = min(
                offset + self.source_range_chunk_bytes - 1,
                self.source_max_bytes - 1,
            )
            last_error: ArxivClientError | None = None
            retry_after_seconds = self.source_range_delay_seconds
            for attempt in range(1, self.source_range_retry_attempts + 1):
                headers = {
                    "User-Agent": self.user_agent,
                    "Accept": "*/*",
                    "Range": f"bytes={offset}-{requested_end}",
                }
                request = urllib.request.Request(download_url, headers=headers)
                try:
                    with urllib.request.urlopen(
                        request, timeout=self.timeout_seconds
                    ) as response:
                        self._validate_response_host(response)
                        download_url = response.geturl()
                        status = getattr(response, "status", response.getcode())
                        content_type = response.headers.get_content_type()

                        if status == 200:
                            content = self._read_response(response, self.source_max_bytes)
                            if progress_callback is not None:
                                progress_callback(
                                    SourceDownloadProgress(
                                        bytes_downloaded=len(content),
                                        bytes_total=len(content),
                                        resumed_bytes=0,
                                        source_identity=response.headers.get(
                                            "Content-Disposition"
                                        ),
                                    ),
                                    content,
                                    bool(offset),
                                )
                            return content, content_type
                        if status != 206:
                            raise ArxivClientError(
                                "arXiv did not honor the source archive range request."
                            )

                        content_range = response.headers.get("Content-Range", "")
                        match = _CONTENT_RANGE.fullmatch(content_range.strip())
                        if match is None:
                            raise ArxivClientError(
                                "arXiv returned an invalid Content-Range header."
                            )
                        response_start, response_end, response_total = map(int, match.groups())
                        if (
                            response_start != offset
                            or response_end < response_start
                            or response_end > requested_end
                            or response_end >= response_total
                        ):
                            raise ArxivClientError(
                                "arXiv returned an inconsistent source range."
                            )
                        if response_total > self.source_max_bytes:
                            raise ArxivClientError(
                                "arXiv response exceeds the configured size limit."
                            )
                        if total_size is not None and response_total != total_size:
                            raise ArxivResumeError(
                                "The saved partial source no longer matches the current arXiv file."
                            )

                        response_identity = response.headers.get("Content-Disposition")
                        if (
                            source_identity is not None
                            and response_identity not in {None, source_identity}
                        ):
                            raise ArxivResumeError(
                                "The saved partial source belongs to a different arXiv response."
                            )

                        expected_bytes = response_end - response_start + 1
                        chunk = self._read_response(
                            response,
                            expected_bytes,
                            expected_bytes=expected_bytes,
                        )
                        total_size = response_total
                        source_identity = source_identity or response_identity
                        break
                except urllib.error.HTTPError as error:
                    if error.code == 404:
                        raise ArxivClientError(
                            "The requested arXiv item or source was not found.",
                            retryable=False,
                        ) from error
                    if error.code != 429 and error.code < 500:
                        raise ArxivClientError(
                            f"arXiv returned HTTP {error.code}.",
                            retryable=False,
                        ) from error
                    retry_after = error.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        retry_after_seconds = max(retry_after_seconds, float(retry_after))
                    last_error = ArxivClientError(f"arXiv returned HTTP {error.code}.")
                except ArxivResumeError:
                    raise
                except ArxivClientError as error:
                    last_error = error
                except (
                    urllib.error.URLError,
                    http.client.HTTPException,
                    TimeoutError,
                    ConnectionError,
                    EOFError,
                ) as error:
                    last_error = self._network_error(error)

                if attempt < self.source_range_retry_attempts:
                    time.sleep(retry_after_seconds)
            else:
                assert last_error is not None
                raise self._interrupted_message(
                    last_error,
                    bytes_downloaded=offset,
                    bytes_total=total_size,
                ) from last_error

            chunks.append(chunk)
            offset += len(chunk)
            if progress_callback is not None:
                progress_callback(
                    SourceDownloadProgress(
                        bytes_downloaded=offset,
                        bytes_total=total_size,
                        resumed_bytes=resumed_bytes,
                        source_identity=source_identity,
                    ),
                    chunk,
                    False,
                )
            if total_size is not None and offset < total_size:
                time.sleep(self.source_range_delay_seconds)

        content = b"".join(chunks)
        if total_size is None or len(content) != total_size:
            raise ArxivClientError("arXiv source archive download was incomplete.")
        return content, content_type

    @staticmethod
    def _parse_atom_metadata(content: bytes) -> ArxivMetadata:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as error:
            raise ArxivClientError("arXiv returned invalid metadata XML.") from error
        entry = root.find(f"{_ATOM}entry")
        if entry is None:
            raise ArxivClientError("No metadata was found for this arXiv identifier.")

        def text(name: str) -> str:
            element = entry.find(f"{_ATOM}{name}")
            return re.sub(r"\s+", " ", element.text or "").strip() if element is not None else ""

        title = text("title")
        abstract = text("summary")
        canonical_url = text("id")
        if not title or not canonical_url:
            raise ArxivClientError("arXiv metadata was missing required fields.")
        published = text("published")
        try:
            submitted_at = (
                datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            )
        except ValueError:
            submitted_at = None
        authors = [
            re.sub(r"\s+", " ", name.text or "").strip()
            for author in entry.findall(f"{_ATOM}author")
            if (name := author.find(f"{_ATOM}name")) is not None and (name.text or "").strip()
        ]
        return ArxivMetadata(
            title=title,
            abstract=abstract,
            submitted_at=submitted_at,
            authors=authors,
            canonical_url=canonical_url,
        )

    def _fetch_html_metadata(self, identifier: ArxivIdentifier) -> ArxivMetadata:
        encoded_id = urllib.parse.quote(identifier.canonical, safe="/.")
        content, _ = self._download(
            f"{self.abstract_base}/{encoded_id}",
            self.metadata_max_bytes,
        )
        parser = _ArxivMetadataHTMLParser()
        try:
            parser.feed(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ArxivClientError("arXiv returned invalid metadata HTML.") from error

        def first(name: str) -> str:
            return parser.values.get(name, [""])[0]

        title = first("citation_title")
        if not title:
            raise ArxivClientError("arXiv HTML metadata was missing the paper title.")
        submitted_at: datetime | None = None
        date_value = first("citation_date")
        if date_value:
            for date_format in ("%Y/%m/%d", "%Y-%m-%d", "%Y/%m", "%Y-%m"):
                try:
                    submitted_at = datetime.strptime(date_value, date_format).replace(tzinfo=UTC)
                    break
                except ValueError:
                    continue
        canonical_url = first("citation_abstract_html_url") or (
            f"{self.abstract_base}/{encoded_id}"
        )
        return ArxivMetadata(
            title=title,
            abstract=first("citation_abstract"),
            submitted_at=submitted_at,
            authors=parser.values.get("citation_author", []),
            canonical_url=canonical_url,
        )

    def fetch_metadata(self, identifier: ArxivIdentifier) -> ArxivMetadata:
        if time.monotonic() < self._metadata_api_cooldown_until:
            return self._fetch_html_metadata(identifier)

        query = urllib.parse.urlencode({"id_list": identifier.canonical, "max_results": 1})
        try:
            content, _ = self._download(f"{self.api_base}?{query}", self.metadata_max_bytes)
            return self._parse_atom_metadata(content)
        except ArxivClientError as error:
            if not error.retryable:
                raise
            if error.status_code == 429:
                self._metadata_api_cooldown_until = time.monotonic() + max(
                    self.metadata_api_cooldown_seconds,
                    float(error.retry_after_seconds or 0),
                )
            # The public abstract page exposes standard Highwire citation meta tags.
            # It is a deterministic first-party fallback and avoids making local
            # parsing/rendering depend on temporary Atom API availability.
            return self._fetch_html_metadata(identifier)

    def fetch_source(
        self,
        identifier: ArxivIdentifier,
        *,
        partial_content: bytes = b"",
        expected_total: int | None = None,
        expected_identity: str | None = None,
        progress_callback: SourceProgressCallback | None = None,
    ) -> DownloadedSource:
        encoded_id = urllib.parse.quote(identifier.canonical, safe="/.")
        content, content_type = self._download_source(
            f"{self.source_base}/{encoded_id}",
            partial_content=partial_content,
            expected_total=expected_total,
            expected_identity=expected_identity,
            progress_callback=progress_callback,
        )
        if not content:
            raise ArxivClientError("arXiv returned an empty source archive.")
        return DownloadedSource(content=content, content_type=content_type)
