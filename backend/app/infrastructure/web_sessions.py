from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.installation_identity import (
    InstallationIdentityInvariantError,
    InstallationIdentitySnapshot,
)
from app.application.web_sessions import (
    AutomaticOwnerSessionUnavailableError,
    WebAuthenticationFailedError,
    WebLoginCandidate,
    WebSessionBinding,
    WebSessionRepository,
    WebSessionSetupNotReadyError,
    WebSessionStateError,
)
from app.domain.runtime import RuntimeEdition
from app.domain.setup import InstallationMode
from app.domain.users import PERSONAL_ACCOUNT
from app.domain.web_sessions import IssuedWebSessionToken
from app.infrastructure.db.models import (
    InstallationStateModel,
    UserModel,
    WebSessionModel,
    WorkspaceMembershipModel,
)
from app.infrastructure.installation_identity import (
    DatabaseInstallationOwnerIdentityInvariant,
)

_INSTALLATION_KEY = "installation"
_SESSION_DIGEST_DOMAIN = b"academatepaper.web-session-token.v1\0"


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _begin_serialized(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


@dataclass(frozen=True, slots=True)
class SecureWebSessionTokenCodec:
    session_ttl: timedelta = timedelta(hours=12)
    entropy_bytes: int = 32

    def __post_init__(self) -> None:
        if self.entropy_bytes < 32:
            raise ValueError("Web session credentials require at least 256 bits of entropy.")
        if not timedelta(minutes=5) <= self.session_ttl <= timedelta(days=7):
            raise ValueError("Web session TTL must be between five minutes and seven days.")

    def issue(self, *, now: datetime) -> IssuedWebSessionToken:
        raw_token = secrets.token_urlsafe(self.entropy_bytes)
        return IssuedWebSessionToken(
            raw_token=raw_token,
            token_digest=self.digest(raw_token),
            expires_at=now + self.session_ttl,
        )

    @staticmethod
    def digest(raw_token: str) -> str:
        digest = hashlib.sha256(_SESSION_DIGEST_DOMAIN + raw_token.encode("utf-8"))
        return f"sha256-v1:{digest.hexdigest()}"


def password_login_user(session: Session, username: str | None) -> UserModel | None:
    """Resolve the personal login alias to the owner, including on pre-admin installations."""
    if username in {None, PERSONAL_ACCOUNT}:
        installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
        if installation is not None and installation.installation_mode == "SINGLE_USER":
            if installation.owner_user_id is None:
                return None
            return session.get(UserModel, installation.owner_user_id)
        if username is None:
            return None
    return session.scalar(select(UserModel).where(func.lower(UserModel.username) == username))


@dataclass(slots=True)
class DatabaseWebSessionRepository(WebSessionRepository):
    session_factory: sessionmaker[Session]

    def find_login_candidate(self, *, username: str | None) -> WebLoginCandidate | None:
        with self.session_factory() as session:
            user = password_login_user(session, username)
            if user is None:
                return None
            return WebLoginCandidate(
                user_id=user.id,
                password_hash=user.password_hash,
                status=user.status,
            )

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        requested_workspace_id: str | None,
        issued: IssuedWebSessionToken,
        now: datetime,
        verified_password_hash: str | None = None,
    ) -> WebSessionBinding:
        if issued.expires_at.tzinfo is None or issued.expires_at <= now:
            raise ValueError("Web session expiry must be timezone-aware and in the future.")
        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = self._ready_server_installation(session)
            user = session.get(UserModel, user_id)
            if user is None or user.status != "ACTIVE":
                raise WebAuthenticationFailedError("Invalid login credentials.")
            if verified_password_hash is not None and user.password_hash != verified_password_hash:
                raise WebAuthenticationFailedError("Invalid login credentials.")
            if installation.installation_mode == "SINGLE_USER":
                identity = self._verify_single_user_identity(session)
                if user.id != identity.owner_user_id:
                    raise WebSessionStateError(
                        "Single-user sessions must belong to the installation owner."
                    )
                if (
                    requested_workspace_id is not None
                    and requested_workspace_id != identity.workspace_id
                ):
                    raise WebAuthenticationFailedError("Invalid login credentials.")
                active_workspace_id = identity.workspace_id
            else:
                workspace_ids = tuple(
                    session.scalars(
                        select(WorkspaceMembershipModel.workspace_id)
                        .where(WorkspaceMembershipModel.user_id == user.id)
                        .order_by(
                            WorkspaceMembershipModel.created_at,
                            WorkspaceMembershipModel.workspace_id,
                        )
                    ).all()
                )
                if requested_workspace_id is not None:
                    if requested_workspace_id not in workspace_ids:
                        raise WebAuthenticationFailedError("Invalid login credentials.")
                    active_workspace_id = requested_workspace_id
                elif workspace_ids:
                    active_workspace_id = workspace_ids[0]
                else:
                    raise WebSessionStateError("The active user has no workspace membership.")

            binding = WebSessionBinding(
                session_id=session_id,
                user_id=user.id,
                active_workspace_id=active_workspace_id,
                installation_state_version=installation.state_version,
                expires_at=issued.expires_at,
            )
            session.add(
                WebSessionModel(
                    id=binding.session_id,
                    token_digest=issued.token_digest,
                    user_id=binding.user_id,
                    active_workspace_id=binding.active_workspace_id,
                    installation_state_version=binding.installation_state_version,
                    auth_generation=session.get(UserModel, binding.user_id).auth_generation,
                    expires_at=binding.expires_at,
                    created_at=now,
                )
            )
            session.commit()
            return binding
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def create_automatic_owner_session(
        self,
        *,
        session_id: str,
        issued: IssuedWebSessionToken,
        now: datetime,
    ) -> WebSessionBinding:
        if issued.expires_at.tzinfo is None or issued.expires_at <= now:
            raise ValueError("Web session expiry must be timezone-aware and in the future.")
        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
            if installation is None:
                raise WebSessionStateError("The installation-state singleton is missing.")
            if installation.state != "READY":
                raise WebSessionSetupNotReadyError(
                    "Automatic owner sessions require a completed setup."
                )
            installation = self._server_installation(session)
            if installation.installation_mode != "SINGLE_USER":
                raise AutomaticOwnerSessionUnavailableError(
                    "Automatic owner sessions require single-user installation mode."
                )
            identity = self._verify_single_user_identity(session)
            binding = WebSessionBinding(
                session_id=session_id,
                user_id=identity.owner_user_id,
                active_workspace_id=identity.workspace_id,
                installation_state_version=installation.state_version,
                expires_at=issued.expires_at,
            )
            session.add(
                WebSessionModel(
                    id=binding.session_id,
                    token_digest=issued.token_digest,
                    user_id=binding.user_id,
                    active_workspace_id=binding.active_workspace_id,
                    installation_state_version=binding.installation_state_version,
                    auth_generation=session.get(UserModel, binding.user_id).auth_generation,
                    expires_at=binding.expires_at,
                    created_at=now,
                )
            )
            session.commit()
            return binding
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
            installation = self._ready_server_installation(session)
            web_session = session.scalar(
                select(WebSessionModel).where(WebSessionModel.token_digest == session_digest)
            )
            if (
                web_session is None
                or web_session.revoked_at is not None
                or now >= _aware_utc(web_session.expires_at)
                or web_session.installation_state_version != installation.state_version
                or not secrets.compare_digest(web_session.token_digest, session_digest)
            ):
                return None
            user_is_active = session.scalar(
                select(func.count())
                .select_from(UserModel)
                .where(
                    UserModel.id == web_session.user_id,
                    UserModel.status == "ACTIVE",
                    UserModel.auth_generation == web_session.auth_generation,
                )
            )
            membership_exists = session.get(
                WorkspaceMembershipModel,
                (web_session.active_workspace_id, web_session.user_id),
            )
            if user_is_active != 1 or membership_exists is None:
                return None
            if installation.installation_mode == "SINGLE_USER":
                identity = self._verify_single_user_identity(session)
                if (
                    web_session.user_id != identity.owner_user_id
                    or web_session.active_workspace_id != identity.workspace_id
                ):
                    raise WebSessionStateError(
                        "The single-user browser session is not bound to the unique owner."
                    )
            return web_session.id

    def revoke_session(self, *, session_id: str, now: datetime) -> bool:
        session = self.session_factory()
        try:
            _begin_serialized(session)
            web_session = session.get(WebSessionModel, session_id)
            if web_session is None or web_session.revoked_at is not None:
                session.rollback()
                return False
            web_session.revoked_at = now
            session.commit()
            return True
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _server_installation(session: Session) -> InstallationStateModel:
        installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
        if (
            installation is None
            or installation.edition != "SERVER"
            or installation.installation_mode not in {"SINGLE_USER", "MULTI_USER"}
            or installation.owner_user_id is None
        ):
            raise WebSessionStateError(
                "Web sessions require a Server installation with a selected mode and owner."
            )
        return installation

    @classmethod
    def _ready_server_installation(cls, session: Session) -> InstallationStateModel:
        installation = cls._server_installation(session)
        if installation.state != "READY":
            raise WebSessionStateError("Web sessions require a READY Server installation.")
        return installation

    def _verify_single_user_identity(self, session: Session) -> InstallationIdentitySnapshot:
        try:
            return DatabaseInstallationOwnerIdentityInvariant(
                self.session_factory,
                expected_edition=RuntimeEdition.SERVER,
                expected_installation_mode=InstallationMode.SINGLE_USER,
            ).verify_in_session(session)
        except InstallationIdentityInvariantError as error:
            raise WebSessionStateError(
                "Single-user installation identity invariant is not satisfied."
            ) from error
