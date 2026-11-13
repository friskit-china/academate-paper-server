from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.runtime import RuntimeEdition
from app.domain.setup import InstallationMode

MAX_WORKSPACE_NAME_LENGTH = 128


def normalized_workspace_name(value: str) -> str:
    """Return the safe persisted workspace label used by first-run setup."""

    normalized = value.strip()
    if not normalized:
        raise ValueError("Workspace name must not be empty.")
    if len(normalized) > MAX_WORKSPACE_NAME_LENGTH:
        raise ValueError(
            f"Workspace name must be at most {MAX_WORKSPACE_NAME_LENGTH} characters."
        )
    if any(character in "\x00\r\n" for character in normalized):
        raise ValueError("Workspace name must be a single line without null characters.")
    return normalized


def edition_for_installation_mode(mode: InstallationMode) -> RuntimeEdition:
    # Both first-run choices are Docker B/S deployments.  The mode controls product policy and
    # user capacity; it is deliberately independent of CPU architecture and network exposure.
    del mode
    return RuntimeEdition.SERVER


@dataclass(frozen=True, slots=True)
class StoredBootstrapToken:
    """Persisted bootstrap proof and its non-secret consumption metadata.

    The raw token is intentionally absent.  A repository may persist this record alongside the
    installation singleton or in a dedicated table, but it must update the consumption fields in
    the same transaction that provisions or claims the owner.
    """

    token_digest: str
    expires_at: datetime
    consumed_at: datetime | None = None
    request_digest: str | None = None
    owner_user_id: str | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingLocalOwnerClaim:
    """The unique migration-created owner/workspace pair that setup must claim in place."""

    owner_user_id: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class LocalOwnerCredentials:
    """Validated setup input.

    Both secrets are excluded from repr so application errors and debug logs cannot disclose
    them.  Request idempotency is derived inside the application service; a browser cannot choose
    the persisted request digest.
    """

    bootstrap_token: str = field(repr=False)
    username: str
    display_name: str
    password: str = field(repr=False)
    installation_mode: InstallationMode
    workspace_name: str
    expected_state_version: int
    credential_request_binding: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.bootstrap_token:
            raise ValueError("Bootstrap token must not be empty.")
        if not self.username.strip():
            raise ValueError("Owner username must not be empty.")
        if not self.display_name.strip():
            raise ValueError("Owner display name must not be empty.")
        if not self.password:
            raise ValueError("Owner password must not be empty.")
        normalized_workspace_name(self.workspace_name)
        if self.expected_state_version < 0:
            raise ValueError("Expected setup state version must not be negative.")


@dataclass(frozen=True, slots=True)
class LocalOwnerProvisioning:
    """Secret-safe write set for one atomic local-owner setup transaction."""

    owner_user_id: str
    workspace_id: str
    username: str
    display_name: str
    password_hash: str = field(repr=False)
    claim_existing: bool
    consumed_at: datetime
    request_digest: str
    installation_mode: InstallationMode
    edition: RuntimeEdition
    administrators_group_key: str = "administrators"
    workspace_kind: str = "PRIVATE"
    workspace_name: str = "My Library"
    workspace_role: str = "OWNER"
