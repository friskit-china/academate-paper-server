from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.credentials import (
    CipherEnvelope,
    DefaultCredentialFactory,
    EncryptedCredential,
)
from app.infrastructure.db.models import (
    UserLlmCredentialModel,
    UserModel,
    WorkspaceMembershipModel,
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_domain(model: UserLlmCredentialModel) -> EncryptedCredential:
    return EncryptedCredential(
        id=model.id,
        user_id=model.user_id,
        provider=model.provider,
        api_base=model.api_base,
        model=model.model,
        envelope=CipherEnvelope(
            algorithm=model.encryption_algorithm,
            key_version=model.key_version,
            nonce=model.nonce,
            ciphertext=model.ciphertext,
        ),
        created_at=_as_utc(model.created_at),
        updated_at=_as_utc(model.updated_at),
    )


def _begin_serialized(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


def _assign_record(
    record: UserLlmCredentialModel,
    credential: EncryptedCredential,
) -> None:
    if record.user_id != credential.user_id:
        raise ValueError("Credential ownership cannot be changed.")
    record.provider = credential.provider
    record.api_base = credential.api_base
    record.model = credential.model
    record.ciphertext = credential.envelope.ciphertext
    record.nonce = credential.envelope.nonce
    record.encryption_algorithm = credential.envelope.algorithm
    record.key_version = credential.envelope.key_version
    record.created_at = credential.created_at
    record.updated_at = credential.updated_at


def upsert_default_credential_in_session(
    session: Session,
    *,
    user_id: str,
    factory: DefaultCredentialFactory,
) -> UserLlmCredentialModel:
    """Select and write one default credential inside the caller's transaction."""

    existing_model = session.scalar(
        select(UserLlmCredentialModel)
        .where(UserLlmCredentialModel.user_id == user_id)
        .order_by(
            UserLlmCredentialModel.updated_at.desc(),
            UserLlmCredentialModel.id,
        )
        .limit(1)
    )
    existing = _as_domain(existing_model) if existing_model is not None else None
    credential = factory(existing)
    if credential.user_id != user_id:
        raise ValueError("Default credential ownership does not match the user.")
    if existing_model is None:
        record = UserLlmCredentialModel(
            id=credential.id,
            user_id=credential.user_id,
        )
        session.add(record)
    else:
        if credential.id != existing_model.id:
            raise ValueError("Default credential identity cannot be changed.")
        record = existing_model
    _assign_record(record, credential)
    return record


class DatabaseCredentialRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_for_user(self, user_id: str) -> list[EncryptedCredential]:
        with self._session_factory() as session:
            records = session.scalars(
                select(UserLlmCredentialModel)
                .where(UserLlmCredentialModel.user_id == user_id)
                .order_by(
                    UserLlmCredentialModel.updated_at.desc(),
                    UserLlmCredentialModel.id,
                )
            ).all()
            return [_as_domain(record) for record in records]

    def get_for_user(
        self,
        credential_id: str,
        user_id: str,
    ) -> EncryptedCredential | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(UserLlmCredentialModel).where(
                    UserLlmCredentialModel.id == credential_id,
                    UserLlmCredentialModel.user_id == user_id,
                )
            )
            return _as_domain(record) if record is not None else None

    def get_for_active_workspace_user(
        self,
        credential_id: str,
        user_id: str,
        workspace_id: str,
    ) -> EncryptedCredential | None:
        """Resolve a worker reference and current membership in one database statement."""

        with self._session_factory() as session:
            record = session.scalar(
                select(UserLlmCredentialModel)
                .join(UserModel, UserModel.id == UserLlmCredentialModel.user_id)
                .join(
                    WorkspaceMembershipModel,
                    WorkspaceMembershipModel.user_id == UserLlmCredentialModel.user_id,
                )
                .where(
                    UserLlmCredentialModel.id == credential_id,
                    UserLlmCredentialModel.user_id == user_id,
                    UserModel.status == "ACTIVE",
                    WorkspaceMembershipModel.workspace_id == workspace_id,
                )
            )
            return _as_domain(record) if record is not None else None

    def save(self, credential: EncryptedCredential) -> EncryptedCredential:
        with self._session_factory() as session:
            record = session.get(UserLlmCredentialModel, credential.id)
            if record is None:
                record = UserLlmCredentialModel(id=credential.id, user_id=credential.user_id)
                session.add(record)
            _assign_record(record, credential)
            session.commit()
            session.refresh(record)
            return _as_domain(record)

    def upsert_default_for_user(
        self,
        user_id: str,
        factory: DefaultCredentialFactory,
    ) -> EncryptedCredential:
        """Serialize selection and write of a user's default credential on SQLite.

        The onboarding saga can otherwise race its list-then-create sequence and leave two
        credentials.  Keeping the factory invocation inside the same BEGIN IMMEDIATE transaction
        also preserves the existing credential id (and therefore its authenticated-data binding)
        when a default is replaced.
        """

        session = self._session_factory()
        try:
            _begin_serialized(session)
            record = upsert_default_credential_in_session(
                session,
                user_id=user_id,
                factory=factory,
            )
            session.commit()
            session.refresh(record)
            return _as_domain(record)
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_for_user(self, credential_id: str, user_id: str) -> bool:
        with self._session_factory() as session:
            record = session.scalar(
                select(UserLlmCredentialModel).where(
                    UserLlmCredentialModel.id == credential_id,
                    UserLlmCredentialModel.user_id == user_id,
                )
            )
            if record is None:
                return False
            session.delete(record)
            session.commit()
            return True
