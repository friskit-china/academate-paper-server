from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskVisibility(StrEnum):
    GLOBAL_DERIVED = "GLOBAL_DERIVED"
    WORKSPACE_DERIVED = "WORKSPACE_DERIVED"
    USER_PRIVATE = "USER_PRIVATE"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True)
class TaskScope:
    visibility: TaskVisibility
    workspace_id: str | None = None
    requested_by_user_id: str | None = None
    credential_id: str | None = None

    def __post_init__(self) -> None:
        workspace = bool(self.workspace_id)
        requester = bool(self.requested_by_user_id)
        credential = bool(self.credential_id)
        if self.visibility is TaskVisibility.GLOBAL_DERIVED:
            valid = not workspace and not requester and not credential
        elif self.visibility in {
            TaskVisibility.WORKSPACE_DERIVED,
            TaskVisibility.USER_PRIVATE,
        }:
            valid = workspace and requester
        else:
            valid = not workspace and not requester and not credential
        if not valid:
            raise ValueError(f"Invalid ownership fields for {self.visibility.value} task scope.")

    @property
    def scope_key(self) -> str:
        if self.visibility is TaskVisibility.GLOBAL_DERIVED:
            return "global"
        if self.visibility is TaskVisibility.SYSTEM:
            return "system:installation"
        if self.visibility is TaskVisibility.WORKSPACE_DERIVED:
            return f"workspace:{self.workspace_id}"
        return f"user:{self.workspace_id}:{self.requested_by_user_id}"

    @classmethod
    def global_derived(cls) -> TaskScope:
        return cls(TaskVisibility.GLOBAL_DERIVED)

    @classmethod
    def workspace_derived(
        cls,
        *,
        workspace_id: str,
        requested_by_user_id: str,
        credential_id: str | None = None,
    ) -> TaskScope:
        return cls(
            TaskVisibility.WORKSPACE_DERIVED,
            workspace_id=workspace_id,
            requested_by_user_id=requested_by_user_id,
            credential_id=credential_id,
        )

    @classmethod
    def user_private(
        cls,
        *,
        workspace_id: str,
        requested_by_user_id: str,
        credential_id: str | None = None,
    ) -> TaskScope:
        return cls(
            TaskVisibility.USER_PRIVATE,
            workspace_id=workspace_id,
            requested_by_user_id=requested_by_user_id,
            credential_id=credential_id,
        )

    @classmethod
    def system(cls) -> TaskScope:
        return cls(TaskVisibility.SYSTEM)


class TaskSecretError(ValueError):
    pass


class TaskLeaseLost(RuntimeError):
    """Raised when a stale worker tries to publish after its task lease was reclaimed."""


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
        "servicetoken",
        "bootstraptoken",
    }
)
_BEARER_VALUE = re.compile(r"^bearer\s+\S+", re.IGNORECASE)
_API_KEY_VALUE = re.compile(r"^sk-[A-Za-z0-9_-]{8,}$")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|access[_-]?token|"
    r"refresh[_-]?token|service[_-]?token|bootstrap[_-]?token)\b"
    r"([\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s,;\"'}]+"
)
_BEARER_IN_TEXT = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_API_KEY_IN_TEXT = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_DIAGNOSTIC_IDENTIFIER_UNSAFE = re.compile(r"[^A-Za-z0-9._:/+-]+")


def _normalized_key(key: object) -> str:
    return "".join(character for character in str(key).casefold() if character.isalnum())


def _secret_path(value: Any, path: str = "$") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if (
                normalized in _SENSITIVE_KEY_NAMES
                or normalized.endswith("apikey")
                or normalized.endswith("password")
                or normalized.endswith("secret")
                or (normalized.endswith("token") and not normalized.endswith("tokens"))
            ):
                return f"{path}.{key}"
            if (found := _secret_path(child, f"{path}.{key}")) is not None:
                return found
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            if (found := _secret_path(child, f"{path}[{index}]")) is not None:
                return found
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if (
            _BEARER_VALUE.match(stripped)
            or _API_KEY_VALUE.match(stripped)
            or _SENSITIVE_ASSIGNMENT.search(value)
            or _BEARER_IN_TEXT.search(value)
            or _API_KEY_IN_TEXT.search(value)
        ):
            return path
    return None


def require_secret_free_task_data(value: Any, *, field_name: str) -> None:
    if (path := _secret_path(value)) is not None:
        raise TaskSecretError(f"Task {field_name} contains prohibited secret material at {path}.")


def sanitize_diagnostic_identifier(
    value: object,
    *,
    max_chars: int,
) -> str | None:
    """Return a bounded identifier only when it is safe to persist or expose."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if value is None or isinstance(value, bool):
        return None
    compact = " ".join(str(value).split())
    if not compact or _secret_path(compact) is not None:
        return None
    sanitized = _DIAGNOSTIC_IDENTIFIER_UNSAFE.sub("_", compact[:max_chars])
    return sanitized.strip("._:/+-") or None


def redact_task_error_message(value: object, *, max_chars: int = 4000) -> str:
    """Return a bounded diagnostic while removing common credential representations."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    text = _BEARER_IN_TEXT.sub("Bearer [REDACTED]", text)
    text = _API_KEY_IN_TEXT.sub("[REDACTED_API_KEY]", text)
    return text[:max_chars]


class TaskPaidConfirmationRequired(PermissionError):
    """A serialized retry set includes an explicitly billable task."""
