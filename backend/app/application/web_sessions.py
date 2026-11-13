from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.domain.setup import PasswordHasher
from app.domain.users import normalized_username
from app.domain.web_sessions import IssuedWebSessionToken, WebSessionTokenCodec


class WebAuthenticationFailedError(PermissionError):
    """Login evidence was invalid; callers must not disclose which field failed."""


class WebSessionInvalidError(PermissionError):
    pass


class WebSessionStateError(RuntimeError):
    """The persistent Server identity state cannot safely issue or use sessions."""


class AutomaticOwnerSessionUnavailableError(PermissionError):
    """The deployment or persisted installation mode forbids automatic owner sessions."""


class WebSessionSetupNotReadyError(WebSessionStateError):
    """The installation has not completed its first-run setup ceremony."""


@dataclass(frozen=True, slots=True)
class WebLoginCandidate:
    user_id: str
    password_hash: str
    status: str


@dataclass(frozen=True, slots=True)
class WebSessionBinding:
    session_id: str
    user_id: str
    active_workspace_id: str
    installation_state_version: int
    expires_at: datetime


class WebSessionRepository(Protocol):
    def find_login_candidate(self, *, username: str | None) -> WebLoginCandidate | None: ...

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        requested_workspace_id: str | None,
        issued: IssuedWebSessionToken,
        now: datetime,
        verified_password_hash: str,
    ) -> WebSessionBinding: ...

    def create_automatic_owner_session(
        self,
        *,
        session_id: str,
        issued: IssuedWebSessionToken,
        now: datetime,
    ) -> WebSessionBinding: ...

    def resolve_active_session(
        self,
        *,
        session_digest: str,
        now: datetime,
    ) -> str | None: ...

    def revoke_session(self, *, session_id: str, now: datetime) -> bool: ...


class IdentifierFactory(Protocol):
    def __call__(self) -> str: ...


def _new_identifier() -> str:
    return str(uuid.uuid4())


def _aware_time(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        raise ValueError("Web session lifecycle timestamps must be timezone-aware.")
    return resolved


def _valid_raw_token(raw_token: str) -> bool:
    return 32 <= len(raw_token) <= 512 and raw_token.isascii()


@dataclass(frozen=True, slots=True)
class WebLoginResult:
    raw_session_token: str
    user_id: str
    active_workspace_id: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "WebLoginResult(raw_session_token='**********', "
            f"user_id={self.user_id!r}, active_workspace_id={self.active_workspace_id!r}, "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class WebLoginService:
    repository: WebSessionRepository
    codec: WebSessionTokenCodec
    password_hasher: PasswordHasher
    dummy_password_hash: str
    identifier_factory: IdentifierFactory = _new_identifier

    def login(
        self,
        *,
        username: str | None,
        password: str,
        requested_workspace_id: str | None = None,
        now: datetime | None = None,
    ) -> WebLoginResult:
        logged_in_at = _aware_time(now)
        try:
            canonical_username = normalized_username(username) if username is not None else None
        except (AttributeError, TypeError, ValueError):
            raise WebAuthenticationFailedError("Invalid login credentials.") from None
        if (
            not password
            or len(password) > 1024
            or len(password.encode("utf-8")) > 4096
        ):
            raise WebAuthenticationFailedError("Invalid login credentials.")

        candidate = self.repository.find_login_candidate(username=canonical_username)
        password_hash = (
            candidate.password_hash if candidate is not None else self.dummy_password_hash
        )
        password_matches = self.password_hasher.verify(password, password_hash)
        if candidate is None or candidate.status != "ACTIVE" or not password_matches:
            raise WebAuthenticationFailedError("Invalid login credentials.")

        issued = self.codec.issue(now=logged_in_at)
        try:
            binding = self.repository.create_session(
                session_id=self.identifier_factory(),
                user_id=candidate.user_id,
                requested_workspace_id=requested_workspace_id,
                issued=issued,
                now=logged_in_at,
                verified_password_hash=password_hash,
            )
        except WebAuthenticationFailedError:
            # A guessed workspace, or a user disabled between password verification and commit,
            # must remain indistinguishable from any other failed credential.
            raise WebAuthenticationFailedError("Invalid login credentials.") from None
        return WebLoginResult(
            raw_session_token=issued.raw_token,
            user_id=binding.user_id,
            active_workspace_id=binding.active_workspace_id,
            expires_at=binding.expires_at,
        )


@dataclass(frozen=True, slots=True)
class AutomaticOwnerSessionService:
    """Issue a normal Web session without accepting browser-selected identity fields.

    The explicit deployment policy is validated at process startup. The repository remains the
    authority for installation mode and atomically selects the unique owner/workspace while it
    stores only the session-token digest.
    """

    repository: WebSessionRepository
    codec: WebSessionTokenCodec
    enabled: bool = False
    identifier_factory: IdentifierFactory = _new_identifier

    def issue(self, *, now: datetime | None = None) -> WebLoginResult:
        if not self.enabled:
            raise AutomaticOwnerSessionUnavailableError(
                "Automatic owner sessions are disabled by deployment policy."
            )
        issued_at = _aware_time(now)
        issued = self.codec.issue(now=issued_at)
        binding = self.repository.create_automatic_owner_session(
            session_id=self.identifier_factory(),
            issued=issued,
            now=issued_at,
        )
        return WebLoginResult(
            raw_session_token=issued.raw_token,
            user_id=binding.user_id,
            active_workspace_id=binding.active_workspace_id,
            expires_at=binding.expires_at,
        )


@dataclass(frozen=True, slots=True)
class WebSessionVerifier:
    codec: WebSessionTokenCodec
    repository: WebSessionRepository

    def verify(self, raw_session_token: str, *, now: datetime | None = None) -> str:
        verified_at = _aware_time(now)
        if not _valid_raw_token(raw_session_token):
            raise WebSessionInvalidError("The browser session is invalid.")
        session_id = self.repository.resolve_active_session(
            session_digest=self.codec.digest(raw_session_token),
            now=verified_at,
        )
        if session_id is None:
            raise WebSessionInvalidError("The browser session is invalid.")
        return session_id


@dataclass(frozen=True, slots=True)
class WebLogoutService:
    repository: WebSessionRepository

    def logout(self, *, session_id: str, now: datetime | None = None) -> None:
        logged_out_at = _aware_time(now)
        if not self.repository.revoke_session(session_id=session_id, now=logged_out_at):
            raise WebSessionInvalidError("The browser session is invalid.")
