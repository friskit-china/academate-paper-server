from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.local_sessions import (
    LocalLaunchTokenExpiredError,
    LocalLaunchTokenInvalidError,
    LocalLaunchTokenReplayError,
    LocalSessionRepository,
    LocalSessionStateError,
)
from app.domain.local_sessions import (
    IssuedLocalLaunchToken,
    IssuedLocalSessionToken,
)
from app.infrastructure.db.models import (
    InstallationStateModel,
    LocalLaunchTokenModel,
    LocalSessionModel,
    UserModel,
)

_INSTALLATION_KEY = "installation"
_LAUNCH_KEY = "browser-launch"
_LAUNCH_DIGEST_DOMAIN = b"academatepaper.local-launch-token.v1\0"
_SESSION_DIGEST_DOMAIN = b"academatepaper.local-session-token.v1\0"


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _begin_serialized(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


@dataclass(frozen=True, slots=True)
class SecureLocalSessionTokenCodec:
    launch_ttl: timedelta = timedelta(minutes=5)
    session_ttl: timedelta = timedelta(hours=12)
    entropy_bytes: int = 32

    def __post_init__(self) -> None:
        if self.entropy_bytes < 32:
            raise ValueError("Local browser credentials require at least 256 bits of entropy.")
        if not timedelta(seconds=1) <= self.launch_ttl <= timedelta(minutes=15):
            raise ValueError("Local launch-token TTL must be between one second and 15 minutes.")
        if not timedelta(minutes=1) <= self.session_ttl <= timedelta(days=7):
            raise ValueError("Local session TTL must be between one minute and seven days.")

    def issue_launch(self, *, now: datetime) -> IssuedLocalLaunchToken:
        raw_token = secrets.token_urlsafe(self.entropy_bytes)
        return IssuedLocalLaunchToken(
            raw_token=raw_token,
            token_digest=self.launch_digest(raw_token),
            expires_at=now + self.launch_ttl,
        )

    def issue_session(self, *, now: datetime) -> IssuedLocalSessionToken:
        raw_token = secrets.token_urlsafe(self.entropy_bytes)
        return IssuedLocalSessionToken(
            raw_token=raw_token,
            token_digest=self.session_digest(raw_token),
            expires_at=now + self.session_ttl,
        )

    def launch_digest(self, raw_token: str) -> str:
        return self._digest(_LAUNCH_DIGEST_DOMAIN, raw_token)

    def session_digest(self, raw_token: str) -> str:
        return self._digest(_SESSION_DIGEST_DOMAIN, raw_token)

    @staticmethod
    def _digest(domain: bytes, raw_token: str) -> str:
        digest = hashlib.sha256(domain + raw_token.encode("utf-8")).hexdigest()
        return f"sha256-v1:{digest}"


@dataclass(slots=True)
class DatabaseLocalSessionRepository(LocalSessionRepository):
    session_factory: sessionmaker[Session]

    def register_launch(self, issued: IssuedLocalLaunchToken, *, now: datetime) -> None:
        if issued.expires_at.tzinfo is None or issued.expires_at <= now:
            raise ValueError("Local launch-token expiry must be timezone-aware and in the future.")
        session = self.session_factory()
        try:
            _begin_serialized(session)
            self._ready_installation(session)
            token = session.get(LocalLaunchTokenModel, _LAUNCH_KEY)
            if token is None:
                token = LocalLaunchTokenModel(singleton_key=_LAUNCH_KEY)
                session.add(token)
            token.token_digest = issued.token_digest
            token.expires_at = issued.expires_at
            token.consumed_at = None
            token.created_at = now
            token.updated_at = now
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def exchange_launch(
        self,
        *,
        launch_digest: str,
        session_id: str,
        session_token: IssuedLocalSessionToken,
        now: datetime,
    ) -> None:
        if session_token.expires_at.tzinfo is None or session_token.expires_at <= now:
            raise ValueError("Local session expiry must be timezone-aware and in the future.")
        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = self._ready_installation(session)
            launch = session.get(LocalLaunchTokenModel, _LAUNCH_KEY)
            if launch is None or not hmac.compare_digest(
                launch_digest,
                launch.token_digest,
            ):
                raise LocalLaunchTokenInvalidError("The browser launch proof is invalid.")
            if launch.consumed_at is not None:
                raise LocalLaunchTokenReplayError("The browser launch proof was already used.")
            if now >= _aware_utc(launch.expires_at):
                raise LocalLaunchTokenExpiredError("The browser launch proof has expired.")

            launch.consumed_at = now
            launch.updated_at = now
            session.add(
                LocalSessionModel(
                    id=session_id,
                    token_digest=session_token.token_digest,
                    installation_state_version=installation.state_version,
                    expires_at=session_token.expires_at,
                    created_at=now,
                )
            )
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def resolve_active_session(
        self,
        *,
        session_digest: str,
        now: datetime,
    ) -> str | None:
        with self.session_factory() as session:
            installation = self._ready_installation(session)
            local_session = session.scalar(
                select(LocalSessionModel).where(
                    LocalSessionModel.token_digest == session_digest
                )
            )
            if (
                local_session is None
                or local_session.revoked_at is not None
                or now >= _aware_utc(local_session.expires_at)
                or local_session.installation_state_version
                != installation.state_version
                or not secrets.compare_digest(
                    local_session.token_digest,
                    session_digest,
                )
            ):
                return None
            return local_session.id

    def revoke_session(self, *, session_id: str, now: datetime) -> bool:
        session = self.session_factory()
        try:
            _begin_serialized(session)
            local_session = session.get(LocalSessionModel, session_id)
            if local_session is None or local_session.revoked_at is not None:
                session.rollback()
                return False
            local_session.revoked_at = now
            session.commit()
            return True
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _ready_installation(session: Session) -> InstallationStateModel:
        installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
        if (
            installation is None
            or installation.edition != "LOCAL"
            or installation.state != "READY"
            or installation.owner_user_id is None
        ):
            raise LocalSessionStateError(
                "Local browser credentials require a READY Local installation."
            )
        active_user_count = int(
            session.scalar(
                select(func.count()).select_from(UserModel).where(UserModel.status == "ACTIVE")
            )
            or 0
        )
        owner_is_active = session.scalar(
            select(func.count())
            .select_from(UserModel)
            .where(
                UserModel.id == installation.owner_user_id,
                UserModel.status == "ACTIVE",
            )
        )
        if active_user_count != 1 or owner_is_active != 1:
            raise LocalSessionStateError(
                "Local browser credentials require exactly one active installation owner."
            )
        return installation
