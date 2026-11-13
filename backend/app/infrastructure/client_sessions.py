"""Opaque v2 client credentials with atomic, strict refresh-family rotation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.domain.users import PERSONAL_ACCOUNT, normalized_username
from app.infrastructure.db.client_models import (
    ClientAccessCredentialModel,
    ClientRefreshCredentialModel,
    ClientSessionModel,
    ServerIdentityModel,
)
from app.infrastructure.db.models import (
    InstallationStateModel,
    UserModel,
    WebSessionModel,
    WorkspaceMembershipModel,
)
from app.infrastructure.security import ScryptPasswordHasher
from app.infrastructure.web_sessions import (
    _aware_utc,
    _begin_serialized,
    password_login_user,
)

CLIENT_BINDING_SCOPE_KEY = "academatepaper.verified-client-session.v2"
CLIENT_TRANSPORT_SCOPE_KEY = "academatepaper.client-transport.v2"
ACCESS_COOKIE = "academate_v2_access"
REFRESH_COOKIE = "academate_v2_refresh"
ACCESS_TTL = timedelta(minutes=15)
FAMILY_TTL = timedelta(days=30)
IDLE_TTL = timedelta(days=7)


class ClientAuthenticationError(PermissionError):
    pass


class ClientRateLimitError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedClientSessionBinding:
    credential_id: str
    session_id: str
    user_id: str
    active_workspace_id: str
    installation_state_version: int
    auth_generation: int
    client_kind: str
    token_kind: str = "access"


@dataclass(frozen=True, slots=True)
class ClientTokenPair:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    session_id: str
    user_id: str
    active_workspace_id: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    server_id: str


def _new_id() -> str:
    return str(uuid.uuid4())


def _token(kind: str) -> str:
    return f"ap2{kind}_" + secrets.token_urlsafe(32)


def _valid_token(raw: str, kind: str) -> bool:
    return isinstance(raw, str) and re.fullmatch(rf"ap2{kind}_[A-Za-z0-9_-]{{43}}", raw) is not None


def _digest(raw: str, kind: str, server_id: str) -> str:
    value = f"academatepaper.client-{kind}.v2\0{server_id}\0{raw}".encode()
    return "sha256-v2:" + hashlib.sha256(value).hexdigest()


class LoginRateLimiter:
    """Bounded per-peer and per-account limits; forwarded headers are never identity."""

    def __init__(self, *, capacity: int = 2048, window: float = 60, limit: int = 10) -> None:
        self.capacity, self.window, self.limit = capacity, window, limit
        self._buckets: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, peer: str, username: str) -> None:
        now = time.monotonic()
        try:
            account_key = normalized_username(username)
        except (ValueError, TypeError):
            account_key = username.casefold()
        keys = [
            hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
            for value in (
                "peer:" + peer,
                "user:" + account_key,
            )
        ]
        with self._lock:
            expired = [
                key for key, (start, _) in self._buckets.items() if now - start >= self.window
            ]
            for key in expired:
                del self._buckets[key]
            if any(self._buckets.get(key, (now, 0))[1] >= self.limit for key in keys):
                raise ClientRateLimitError("LOGIN_RATE_LIMITED")
            if len(self._buckets) + sum(key not in self._buckets for key in keys) > self.capacity:
                raise ClientRateLimitError("LOGIN_RATE_LIMITED")
            for key in keys:
                start, count = self._buckets.get(key, (now, 0))
                self._buckets[key] = (start, count + 1)


class ClientSessionService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.password_hasher = ScryptPasswordHasher()
        self._dummy_hash: str | None = None
        self.rate_limiter = LoginRateLimiter()

    def server_id(self) -> str:
        with self.session_factory() as session:
            _begin_serialized(session)
            result = self._server_id(session)
            session.commit()
            return result

    @staticmethod
    def _server_id(session: Session) -> str:
        identity = session.get(ServerIdentityModel, "server")
        if identity is None:
            identity = ServerIdentityModel(singleton_key="server", server_id=_new_id())
            session.add(identity)
            session.flush()
        return identity.server_id

    def _active_family(
        self,
        session: Session,
        family: ClientSessionModel | None,
        now: datetime,
    ) -> ClientSessionModel:
        if (
            family is None
            or family.revoked_at is not None
            or now >= _aware_utc(family.expires_at)
            or now >= _aware_utc(family.idle_expires_at)
        ):
            raise ClientAuthenticationError("INVALID_CLIENT_SESSION")
        installation = session.get(InstallationStateModel, "installation")
        user = session.get(UserModel, family.user_id)
        membership = session.get(
            WorkspaceMembershipModel, (family.active_workspace_id, family.user_id)
        )
        if (
            installation is None
            or installation.state != "READY"
            or installation.edition != "SERVER"
            or installation.installation_mode not in {"SINGLE_USER", "MULTI_USER"}
            or family.installation_state_version != installation.state_version
            or user is None
            or user.status != "ACTIVE"
            or membership is None
            or family.auth_generation != user.auth_generation
        ):
            raise ClientAuthenticationError("INVALID_CLIENT_SESSION")
        from app.infrastructure.identity import (
            AuthenticatedDatabaseIdentityProvider,
            IdentityStateError,
            UntrustedServiceIdentityError,
        )

        try:
            AuthenticatedDatabaseIdentityProvider(
                self.session_factory
            ).resolve_principal_in_session(
                session,
                user_id=family.user_id,
                workspace_id=family.active_workspace_id,
                auth_generation=family.auth_generation,
            )
        except (IdentityStateError, UntrustedServiceIdentityError):
            raise ClientAuthenticationError("INVALID_CLIENT_SESSION") from None
        return family

    def login(
        self,
        *,
        username: str | None,
        password: str,
        client_kind: str,
        requested_workspace_id: str | None = None,
        client_name: str = "",
        device_name: str = "",
        peer: str = "unknown",
        now: datetime | None = None,
    ) -> ClientTokenPair:
        self.rate_limiter.check(peer, username if username is not None else PERSONAL_ACCOUNT)
        try:
            username = normalized_username(username) if username is not None else None
        except (ValueError, TypeError):
            raise ClientAuthenticationError("INVALID_LOGIN") from None
        try:
            password_size = len(password.encode("utf-8"))
        except UnicodeError:
            raise ClientAuthenticationError("INVALID_LOGIN") from None
        if (
            client_kind not in {"native", "web"}
            or not password
            or len(password) > 1024
            or password_size > 4096
        ):
            raise ClientAuthenticationError("INVALID_LOGIN")
        if self._dummy_hash is None:
            self._dummy_hash = self.password_hasher.hash("client-login-dummy-not-an-account")
        with self.session_factory() as session:
            candidate = password_login_user(session, username)
            user_id = candidate.id if candidate else None
            password_hash = candidate.password_hash if candidate else self._dummy_hash
        matches = self.password_hasher.verify(password, password_hash)
        if user_id is None or not matches:
            raise ClientAuthenticationError("INVALID_LOGIN")
        now = now or datetime.now(UTC)
        with self.session_factory() as session:
            _begin_serialized(session)
            user = session.get(UserModel, user_id)
            installation = session.get(InstallationStateModel, "installation")
            if (
                user is None
                or user.status != "ACTIVE"
                or user.password_hash != password_hash
                or installation is None
            ):
                raise ClientAuthenticationError("INVALID_LOGIN")
            memberships = list(
                session.scalars(
                    select(WorkspaceMembershipModel.workspace_id)
                    .where(
                        WorkspaceMembershipModel.user_id == user_id,
                    )
                    .order_by(
                        WorkspaceMembershipModel.created_at, WorkspaceMembershipModel.workspace_id
                    )
                )
            )
            workspace = requested_workspace_id or (memberships[0] if memberships else None)
            if workspace not in memberships:
                raise ClientAuthenticationError("INVALID_LOGIN")
            family = ClientSessionModel(
                id=_new_id(),
                user_id=user_id,
                active_workspace_id=workspace,
                client_kind=client_kind,
                client_name=client_name[:128],
                device_name=device_name[:128],
                installation_state_version=installation.state_version,
                auth_generation=user.auth_generation,
                created_at=now,
                last_seen_at=now,
                expires_at=now + FAMILY_TTL,
                idle_expires_at=now + IDLE_TTL,
            )
            self._active_family(session, family, now)
            session.add(family)
            session.flush()
            pair, _ = self._issue_pair(session, family, 0, now)
            session.commit()
            return pair

    def exchange_legacy_web_session(
        self,
        raw_legacy: str,
        *,
        now: datetime | None = None,
    ) -> ClientTokenPair:
        from app.application.web_sessions import (
            WebSessionInvalidError,
            WebSessionStateError,
            WebSessionVerifier,
        )
        from app.infrastructure.identity import (
            AuthenticatedDatabaseIdentityProvider,
            IdentityStateError,
            UntrustedServiceIdentityError,
        )
        from app.infrastructure.web_sessions import (
            DatabaseWebSessionRepository,
            SecureWebSessionTokenCodec,
        )

        now = now or datetime.now(UTC)
        codec = SecureWebSessionTokenCodec()
        verifier = WebSessionVerifier(
            codec=codec, repository=DatabaseWebSessionRepository(self.session_factory)
        )
        try:
            legacy_id = verifier.verify(raw_legacy, now=now)
        except (WebSessionInvalidError, WebSessionStateError, ValueError):
            raise ClientAuthenticationError("INVALID_LEGACY_WEB_SESSION") from None
        with self.session_factory() as session:
            _begin_serialized(session)
            legacy = session.get(WebSessionModel, legacy_id)
            installation = session.get(InstallationStateModel, "installation")
            if (
                legacy is None
                or legacy.revoked_at is not None
                or installation is None
                or now >= _aware_utc(legacy.expires_at)
                or legacy.installation_state_version != installation.state_version
                or not secrets.compare_digest(legacy.token_digest, codec.digest(raw_legacy))
            ):
                raise ClientAuthenticationError("INVALID_LEGACY_WEB_SESSION")
            try:
                principal = AuthenticatedDatabaseIdentityProvider(
                    self.session_factory
                ).resolve_principal_in_session(
                    session,
                    user_id=legacy.user_id,
                    workspace_id=legacy.active_workspace_id,
                    auth_generation=legacy.auth_generation,
                )
            except (IdentityStateError, UntrustedServiceIdentityError):
                raise ClientAuthenticationError("INVALID_LEGACY_WEB_SESSION") from None
            # Migration does not extend the original authentication lifetime without a password.
            absolute_expiry = min(now + FAMILY_TTL, _aware_utc(legacy.expires_at))
            family = ClientSessionModel(
                id=_new_id(),
                user_id=principal.user_id,
                active_workspace_id=principal.active_workspace_id,
                client_kind="web",
                client_name="legacy-web-migration",
                device_name="Migrated browser",
                installation_state_version=legacy.installation_state_version,
                auth_generation=legacy.auth_generation,
                created_at=now,
                last_seen_at=now,
                expires_at=absolute_expiry,
                idle_expires_at=min(now + IDLE_TTL, absolute_expiry),
            )
            self._active_family(session, family, now)
            session.add(family)
            session.flush()
            pair, _ = self._issue_pair(session, family, 0, now)
            legacy.revoked_at = now
            session.commit()
            return pair

    def _issue_pair(
        self,
        session: Session,
        family: ClientSessionModel,
        sequence: int,
        now: datetime,
    ) -> tuple[ClientTokenPair, str]:
        server_id = self._server_id(session)
        access, refresh = _token("a"), _token("r")
        access_expiry = min(now + ACCESS_TTL, _aware_utc(family.expires_at))
        refresh_expiry = min(now + IDLE_TTL, _aware_utc(family.expires_at))
        refresh_id = _new_id()
        session.add_all(
            [
                ClientAccessCredentialModel(
                    id=_new_id(),
                    session_id=family.id,
                    token_digest=_digest(access, "access", server_id),
                    issued_at=now,
                    expires_at=access_expiry,
                    auth_generation=family.auth_generation,
                ),
                ClientRefreshCredentialModel(
                    id=refresh_id,
                    session_id=family.id,
                    token_digest=_digest(refresh, "refresh", server_id),
                    sequence=sequence,
                    issued_at=now,
                    expires_at=refresh_expiry,
                ),
            ]
        )
        return ClientTokenPair(
            access,
            refresh,
            family.id,
            family.user_id,
            family.active_workspace_id,
            access_expiry,
            refresh_expiry,
            server_id,
        ), refresh_id

    def verify_access(
        self,
        raw: str,
        *,
        client_kind: str,
        now: datetime | None = None,
    ) -> VerifiedClientSessionBinding:
        if not _valid_token(raw, "a"):
            raise ClientAuthenticationError("INVALID_ACCESS_TOKEN")
        now = now or datetime.now(UTC)
        with self.session_factory() as session:
            identity = session.get(ServerIdentityModel, "server")
            if identity is None:
                raise ClientAuthenticationError("INVALID_ACCESS_TOKEN")
            credential = session.scalar(
                select(ClientAccessCredentialModel).where(
                    ClientAccessCredentialModel.token_digest
                    == _digest(raw, "access", identity.server_id),
                )
            )
            if credential is None:
                raise ClientAuthenticationError("INVALID_ACCESS_TOKEN")
            family = self._active_family(
                session, session.get(ClientSessionModel, credential.session_id), now
            )
            binding = VerifiedClientSessionBinding(
                credential.id,
                family.id,
                family.user_id,
                family.active_workspace_id,
                family.installation_state_version,
                family.auth_generation,
                family.client_kind,
            )
            self.validate_binding_in_session(session, binding, now, client_kind=client_kind)
            return binding

    def validate_binding_in_session(
        self,
        session: Session,
        binding: VerifiedClientSessionBinding,
        now: datetime,
        *,
        client_kind: str | None = None,
    ) -> ClientSessionModel:
        credential = session.get(ClientAccessCredentialModel, binding.credential_id)
        family = self._active_family(
            session, session.get(ClientSessionModel, binding.session_id), now
        )
        if (
            credential is None
            or credential.session_id != family.id
            or credential.revoked_at is not None
            or now >= _aware_utc(credential.expires_at)
            or binding.token_kind != "access"
            or credential.auth_generation != family.auth_generation
            or (client_kind is not None and family.client_kind != client_kind)
            or binding.user_id != family.user_id
            or binding.active_workspace_id != family.active_workspace_id
            or binding.installation_state_version != family.installation_state_version
            or binding.auth_generation != family.auth_generation
            or binding.client_kind != family.client_kind
        ):
            raise ClientAuthenticationError("INVALID_ACCESS_TOKEN")
        return family

    def refresh(
        self, raw: str, *, client_kind: str, now: datetime | None = None
    ) -> ClientTokenPair:
        if not _valid_token(raw, "r"):
            raise ClientAuthenticationError("INVALID_REFRESH_TOKEN")
        now = now or datetime.now(UTC)
        with self.session_factory() as session:
            _begin_serialized(session)
            identity = session.get(ServerIdentityModel, "server")
            if identity is None:
                raise ClientAuthenticationError("INVALID_REFRESH_TOKEN")
            previous = session.scalar(
                select(ClientRefreshCredentialModel).where(
                    ClientRefreshCredentialModel.token_digest
                    == _digest(raw, "refresh", identity.server_id),
                )
            )
            if previous is None:
                raise ClientAuthenticationError("INVALID_REFRESH_TOKEN")
            family = session.get(ClientSessionModel, previous.session_id)
            if family is None or family.client_kind != client_kind:
                raise ClientAuthenticationError("INVALID_REFRESH_TOKEN")
            if previous.consumed_at is not None:
                self._revoke_family(session, family.id, now, "REFRESH_REPLAY")
                session.commit()  # replay revocation must survive the authentication failure
                raise ClientAuthenticationError("INVALID_REFRESH_TOKEN")
            family = self._active_family(session, family, now)
            if previous.revoked_at is not None or now >= _aware_utc(previous.expires_at):
                raise ClientAuthenticationError("INVALID_REFRESH_TOKEN")
            pair, replacement_id = self._issue_pair(session, family, previous.sequence + 1, now)
            previous.consumed_at, previous.replacement_id = now, replacement_id
            family.last_seen_at = now
            family.idle_expires_at = pair.refresh_expires_at
            session.commit()
            return pair

    @staticmethod
    def _revoke_family(session: Session, session_id: str, now: datetime, reason: str) -> None:
        session.execute(
            update(ClientSessionModel)
            .where(ClientSessionModel.id == session_id)
            .values(
                revoked_at=now,
                revocation_reason=reason,
            )
        )
        for model in (ClientAccessCredentialModel, ClientRefreshCredentialModel):
            session.execute(
                update(model).where(model.session_id == session_id).values(revoked_at=now)
            )

    def revoke(self, binding: VerifiedClientSessionBinding, target_id: str | None = None) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            _begin_serialized(session)
            self.validate_binding_in_session(session, binding, now)
            target = session.get(ClientSessionModel, target_id or binding.session_id)
            if target is None or target.user_id != binding.user_id:
                return False
            self._revoke_family(session, target.id, now, "USER_REVOKED")
            session.commit()
            return True

    def revoke_all(self, binding: VerifiedClientSessionBinding) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            _begin_serialized(session)
            self.validate_binding_in_session(session, binding, now)
            user = session.get(UserModel, binding.user_id)
            assert user is not None
            user.auth_generation += 1
            ids = session.scalars(
                select(ClientSessionModel.id).where(ClientSessionModel.user_id == user.id)
            )
            for session_id in ids:
                self._revoke_family(session, session_id, now, "USER_REVOKED_ALL")
            session.execute(
                update(WebSessionModel)
                .where(WebSessionModel.user_id == user.id)
                .values(revoked_at=now)
            )
            session.commit()

    def list_sessions(
        self,
        binding: VerifiedClientSessionBinding,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        if not 1 <= limit <= 200:
            raise ValueError("INVALID_SESSION_LIMIT")
        before = None
        if cursor is not None:
            try:
                if len(cursor) > 256:
                    raise ValueError
                created, identifier = json.loads(
                    base64.b64decode(
                        cursor + "=" * (-len(cursor) % 4),
                        altchars=b"-_",
                        validate=True,
                    )
                )
                timestamp = datetime.fromisoformat(created)
                if timestamp.tzinfo is None or str(uuid.UUID(identifier)) != identifier:
                    raise ValueError
                before = timestamp, identifier
            except (ValueError, TypeError, UnicodeError):
                raise ValueError("INVALID_SESSION_CURSOR") from None
        with self.session_factory() as session:
            self.validate_binding_in_session(session, binding, datetime.now(UTC))
            query = select(ClientSessionModel).where(ClientSessionModel.user_id == binding.user_id)
            if before is not None:
                timestamp, identifier = before
                query = query.where(
                    (ClientSessionModel.created_at < timestamp)
                    | (
                        (ClientSessionModel.created_at == timestamp)
                        & (ClientSessionModel.id < identifier)
                    )
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        ClientSessionModel.created_at.desc(),
                        ClientSessionModel.id.desc(),
                    ).limit(limit + 1)
                )
            )
            more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = None
            if more:
                next_cursor = (
                    base64.urlsafe_b64encode(
                        json.dumps(
                            [
                                _aware_utc(rows[-1].created_at).isoformat(),
                                rows[-1].id,
                            ]
                        ).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            return {
                "items": [
                    {
                        "id": row.id,
                        "client_kind": row.client_kind,
                        "client_name": row.client_name,
                        "device_name": row.device_name,
                        "created_at": _aware_utc(row.created_at),
                        "last_seen_at": _aware_utc(row.last_seen_at),
                        "revoked": row.revoked_at is not None,
                        "expires_at": _aware_utc(row.expires_at),
                        "current": row.id == binding.session_id,
                    }
                    for row in rows
                ],
                "next_cursor": next_cursor,
            }
