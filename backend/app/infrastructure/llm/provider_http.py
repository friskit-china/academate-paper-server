from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime

from app.domain.tasks import redact_task_error_message, sanitize_diagnostic_identifier


def _compact_text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    compact = " ".join(str(value).split())
    return compact[:limit] or None


def sanitize_provider_identifier(value: object, *, limit: int) -> str | None:
    """Normalize untrusted provider identifiers before they cross the adapter boundary."""

    compact = _compact_text(value, limit=limit)
    if compact is None:
        return None
    return sanitize_diagnostic_identifier(compact, max_chars=limit)


def _sanitize_retry_after(value: object) -> str | None:
    compact = _compact_text(value, limit=64)
    if compact is None or sanitize_provider_identifier(compact, limit=64) is None:
        return None
    if compact.isdecimal() and len(compact) <= 10:
        return str(int(compact))
    try:
        parsed = parsedate_to_datetime(compact)
        if parsed.tzinfo is None:
            return None
        return format_datetime(parsed.astimezone(UTC), usegmt=True)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class ProviderContentFilter:
    role: str | None
    level: str | None


@dataclass(frozen=True, slots=True)
class ProviderHTTPDiagnostic:
    status_code: int
    request_id: str
    provider_code: str | None = None
    provider_type: str | None = None
    provider_message: str | None = None
    content_filters: tuple[ProviderContentFilter, ...] = ()
    retry_after: str | None = None

    @property
    def is_content_safety_rejection(self) -> bool:
        return self.provider_code == "1301"

    @property
    def error_code(self) -> str:
        return self.provider_code or self.provider_type or f"http_{self.status_code}"


def read_provider_http_error(
    error: urllib.error.HTTPError,
    client_request_id: str,
) -> ProviderHTTPDiagnostic:
    """Extract bounded, user-safe diagnostics without retaining request secrets or prompts."""
    payload: object = {}
    try:
        body = error.read(65_536)
        payload = json.loads(body) if body else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}

    details: object = payload.get("error", {}) if isinstance(payload, dict) else {}
    provider_code = None
    provider_type = None
    provider_message = None
    if isinstance(details, dict):
        provider_code = sanitize_provider_identifier(details.get("code"), limit=128)
        provider_type = sanitize_provider_identifier(details.get("type"), limit=128)
        raw_provider_message = _compact_text(details.get("message"), limit=1_000)
        if raw_provider_message is not None:
            provider_message = redact_task_error_message(
                raw_provider_message,
                max_chars=1_000,
            )

    content_filter_payload: object = []
    if isinstance(payload, dict):
        content_filter_payload = payload.get("contentFilter", payload.get("content_filter", []))
    parsed_filters: list[ProviderContentFilter] = []
    if isinstance(content_filter_payload, list):
        for item in content_filter_payload[:8]:
            if not isinstance(item, dict):
                continue
            parsed_filters.append(
                ProviderContentFilter(
                    role=sanitize_provider_identifier(item.get("role"), limit=32),
                    level=sanitize_provider_identifier(item.get("level"), limit=32),
                )
            )

    header_request_id = error.headers.get("x-request-id") if error.headers else None
    payload_request_id = payload.get("request_id") if isinstance(payload, dict) else None
    request_id = (
        sanitize_provider_identifier(header_request_id, limit=255)
        or sanitize_provider_identifier(payload_request_id, limit=255)
        or sanitize_provider_identifier(client_request_id, limit=255)
        or "unavailable"
    )
    retry_after = error.headers.get("retry-after") if error.headers else None
    return ProviderHTTPDiagnostic(
        status_code=error.code,
        request_id=request_id,
        provider_code=provider_code,
        provider_type=provider_type,
        provider_message=provider_message,
        content_filters=tuple(parsed_filters),
        retry_after=_sanitize_retry_after(retry_after),
    )


def provider_http_error_message(operation: str, diagnostic: ProviderHTTPDiagnostic) -> str:
    if diagnostic.is_content_safety_rejection:
        lead = f"{operation} was rejected by the provider's content safety policy"
    else:
        lead = f"{operation} provider request failed"
    fields = [f"HTTP {diagnostic.status_code}", f"code={diagnostic.error_code}"]
    if diagnostic.provider_type and diagnostic.provider_type != diagnostic.error_code:
        fields.append(f"type={diagnostic.provider_type}")
    fields.append(f"request_id={diagnostic.request_id}")
    if diagnostic.retry_after:
        fields.append(f"retry_after={diagnostic.retry_after}")
    for item in diagnostic.content_filters:
        filter_fields = []
        if item.role:
            filter_fields.append(f"role={item.role}")
        if item.level:
            filter_fields.append(f"level={item.level}")
        if filter_fields:
            fields.append(f"content_filter({', '.join(filter_fields)})")
    message = f"{lead} ({', '.join(fields)})"
    if diagnostic.provider_message:
        message += f". Provider message: {diagnostic.provider_message}"
    return redact_task_error_message(message, max_chars=4_000)
