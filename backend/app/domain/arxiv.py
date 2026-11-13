from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse


class InvalidArxivIdentifier(ValueError):
    pass


_ARXIV_ID = re.compile(
    r"^(?P<base>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))"
    r"(?P<version>v[1-9]\d*)?$",
    re.IGNORECASE,
)
_ALLOWED_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_URL_PREFIXES = ("/abs/", "/pdf/", "/e-print/")


@dataclass(frozen=True, slots=True)
class ArxivIdentifier:
    arxiv_id: str
    version: str = ""

    @property
    def canonical(self) -> str:
        return f"{self.arxiv_id}{self.version}"


def normalize_arxiv_identifier(value: str) -> ArxivIdentifier:
    candidate = value.strip()
    if not candidate:
        raise InvalidArxivIdentifier("Enter an arXiv URL or identifier.")

    if "://" in candidate:
        parsed = urlparse(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS
        ):
            raise InvalidArxivIdentifier("Only arxiv.org URLs are accepted.")
        path = unquote(parsed.path)
        prefix = next((item for item in _URL_PREFIXES if path.startswith(item)), None)
        if prefix is None:
            raise InvalidArxivIdentifier("Use an arXiv abstract, PDF, or source URL.")
        candidate = path[len(prefix) :]

    candidate = unquote(candidate).strip().removesuffix(".pdf").strip("/")
    match = _ARXIV_ID.fullmatch(candidate)
    if match is None:
        raise InvalidArxivIdentifier(f"Invalid arXiv identifier: {value!r}")

    base = match.group("base")
    version = (match.group("version") or "").lower()
    if "/" in base:
        archive, number = base.split("/", 1)
        base = f"{archive}/{number}"
    return ArxivIdentifier(arxiv_id=base, version=version)
