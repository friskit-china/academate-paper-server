from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from app.domain.credentials import (
    AuthenticatedCipher,
    CipherAuthenticationError,
    CipherUnavailableError,
    CredentialBinding,
    CredentialRepository,
    DefaultCredentialFactory,
    EncryptedCredential,
    SecretKeyNotFoundError,
    SecretStore,
    SecretStoreUnavailableError,
    SensitiveText,
)
from app.domain.ports import QueuedTask
from app.domain.tasks import TaskScope, TaskVisibility

MASTER_KEY_NAME = "academatepaper.llm-credentials.master"


class CredentialApplicationError(RuntimeError):
    error_code = "CREDENTIAL_ERROR"
    retryable = False
    recoverable = True
    requires_reentry = False


class CredentialNotFoundError(CredentialApplicationError):
    error_code = "CREDENTIAL_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("Credential not found.")


class CredentialReentryRequiredError(CredentialApplicationError):
    error_code = "CREDENTIAL_REENTRY_REQUIRED"
    requires_reentry = True

    def __init__(self) -> None:
        super().__init__("The credential must be entered again before this operation can continue.")


class CredentialStoreUnavailableError(CredentialApplicationError):
    error_code = "CREDENTIAL_STORE_UNAVAILABLE"
    retryable = True

    def __init__(self) -> None:
        super().__init__("Credential storage is temporarily unavailable.")


class LlmCredentialNotConfiguredError(CredentialApplicationError):
    error_code = "LLM_CREDENTIAL_NOT_CONFIGURED"
    requires_reentry = True

    def __init__(self) -> None:
        super().__init__("A usable LLM credential is not configured for this user.")


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    id: str
    provider: str
    api_base: str | None
    model: str | None
    configured: bool
    masked_suffix: str | None
    requires_reentry: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CredentialWrite:
    provider: str
    api_key: SensitiveText = field(repr=False)
    api_base: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        raw_key = self.api_key.reveal()
        if not raw_key.strip() or re.fullmatch(r"(?:\*{8}|••••)(?:.{4})?", raw_key):
            raise ValueError("Enter a new API key to replace the saved credential.")
        provider = self.provider.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", provider):
            raise ValueError("Credential provider must contain between 1 and 64 characters.")
        if self.model is not None and len(self.model) > 128:
            raise ValueError("Credential model must not exceed 128 characters.")
        if self.api_base is not None and len(self.api_base) > 2048:
            raise ValueError("Credential API base must not exceed 2048 characters.")
        if self.api_base is not None:
            parsed = urlsplit(self.api_base)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Credential API base must be an HTTP(S) origin/path without userinfo, "
                    "query, or fragment."
                )


@dataclass(frozen=True, slots=True)
class ResolvedLlmCredential:
    credential_id: str
    user_id: str
    provider: str
    api_key: SensitiveText = field(repr=False)
    api_base: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class LlmTaskCredential:
    """Non-secret provider metadata plus the server-selected owner credential reference."""

    credential_id: str | None
    provider: str
    api_base: str
    model: str


class CredentialService:
    def __init__(
        self,
        repository: CredentialRepository,
        secret_store: SecretStore,
        cipher: AuthenticatedCipher,
    ) -> None:
        self._repository = repository
        self._secret_store = secret_store
        self._cipher = cipher

    def list_for_user(self, user_id: str) -> list[CredentialMetadata]:
        return [self._metadata(record) for record in self._repository.list_for_user(user_id)]

    def get_for_user(self, credential_id: str, user_id: str) -> CredentialMetadata:
        record = self._repository.get_for_user(credential_id, user_id)
        if record is None:
            raise CredentialNotFoundError
        return self._metadata(record)

    def default_for_user(self, user_id: str) -> CredentialMetadata | None:
        records = self._repository.list_for_user(user_id)
        return self._metadata(records[0]) if records else None

    def upsert_default_for_user(
        self,
        user_id: str,
        value: CredentialWrite,
    ) -> CredentialMetadata:
        encrypted_value = self.default_write_factory(user_id, value)
        record = self._repository.upsert_default_for_user(user_id, encrypted_value)
        return self._metadata_from_plaintext(record, value.api_key)

    def default_write_factory(
        self,
        user_id: str,
        value: CredentialWrite,
        *,
        now: datetime | None = None,
    ) -> DefaultCredentialFactory:
        """Build an encryption callback suitable for an adapter-owned transaction."""

        updated_at = now or datetime.now(UTC)
        if updated_at.tzinfo is None:
            raise ValueError("Credential update time must be timezone-aware.")

        def encrypted_value(
            existing: EncryptedCredential | None,
        ) -> EncryptedCredential:
            return self._encrypt(
                credential_id=(existing.id if existing is not None else str(uuid4())),
                user_id=user_id,
                value=value,
                created_at=(existing.created_at if existing is not None else updated_at),
                updated_at=updated_at,
            )

        return encrypted_value

    def create_for_user(self, user_id: str, value: CredentialWrite) -> CredentialMetadata:
        now = datetime.now(UTC)
        record = self._encrypt(
            credential_id=str(uuid4()),
            user_id=user_id,
            value=value,
            created_at=now,
            updated_at=now,
        )
        self._repository.save(record)
        return self._metadata_from_plaintext(record, value.api_key)

    def replace_for_user(
        self,
        credential_id: str,
        user_id: str,
        value: CredentialWrite,
    ) -> CredentialMetadata:
        existing = self._repository.get_for_user(credential_id, user_id)
        if existing is None:
            raise CredentialNotFoundError
        record = self._encrypt(
            credential_id=credential_id,
            user_id=user_id,
            value=value,
            created_at=existing.created_at,
            updated_at=datetime.now(UTC),
        )
        self._repository.save(record)
        return self._metadata_from_plaintext(record, value.api_key)

    def delete_for_user(self, credential_id: str, user_id: str) -> None:
        if not self._repository.delete_for_user(credential_id, user_id):
            raise CredentialNotFoundError

    def resolve_for_task(self, task: QueuedTask) -> ResolvedLlmCredential:
        """Resolve only the requester's credential reference; UUID possession grants nothing."""

        if (
            task.visibility not in {TaskVisibility.WORKSPACE_DERIVED, TaskVisibility.USER_PRIVATE}
            or task.workspace_id is None
            or task.requested_by_user_id is None
            or task.credential_id is None
        ):
            raise CredentialReentryRequiredError
        try:
            scope = TaskScope(
                visibility=task.visibility,
                workspace_id=task.workspace_id,
                requested_by_user_id=task.requested_by_user_id,
                credential_id=task.credential_id,
            )
        except ValueError as error:
            raise CredentialReentryRequiredError from error
        if task.scope_key != scope.scope_key:
            raise CredentialReentryRequiredError
        record = self._repository.get_for_active_workspace_user(
            task.credential_id,
            task.requested_by_user_id,
            task.workspace_id,
        )
        # Missing, cross-user, disabled-user, and non-member references deliberately have the
        # same result.  The membership check and encrypted-row lookup share one DB statement;
        # handlers still recheck resource scope before use and again before publication.
        if record is None:
            raise CredentialReentryRequiredError
        plaintext = self._decrypt(record)
        return ResolvedLlmCredential(
            credential_id=record.id,
            user_id=record.user_id,
            provider=record.provider,
            api_base=record.api_base,
            model=record.model,
            api_key=plaintext,
        )

    def _encrypt(
        self,
        *,
        credential_id: str,
        user_id: str,
        value: CredentialWrite,
        created_at: datetime,
        updated_at: datetime,
    ) -> EncryptedCredential:
        try:
            availability = self._secret_store.availability()
            if not availability.available:
                raise CredentialStoreUnavailableError
            key_version = self._secret_store.active_key_version(MASTER_KEY_NAME)
        except SecretStoreUnavailableError as error:
            raise CredentialStoreUnavailableError from error
        if key_version is None:
            raise CredentialReentryRequiredError
        try:
            key = self._secret_store.get(MASTER_KEY_NAME, key_version)
        except SecretStoreUnavailableError as error:
            raise CredentialStoreUnavailableError from error
        except SecretKeyNotFoundError as error:
            raise CredentialReentryRequiredError from error
        provider = value.provider.strip().lower()
        binding = CredentialBinding(
            credential_id=credential_id,
            user_id=user_id,
            provider=provider,
            api_base=value.api_base,
            model=value.model,
        )
        try:
            envelope = self._cipher.encrypt(
                value.api_key,
                key=key,
                key_version=key_version,
                authenticated_data=binding.authenticated_data(),
            )
        except CipherUnavailableError as error:
            raise CredentialStoreUnavailableError from error
        return EncryptedCredential(
            id=credential_id,
            user_id=user_id,
            provider=provider,
            api_base=value.api_base,
            model=value.model,
            envelope=envelope,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _decrypt(self, record: EncryptedCredential) -> SensitiveText:
        try:
            availability = self._secret_store.availability()
            if not availability.available:
                raise CredentialStoreUnavailableError
            key = self._secret_store.get(MASTER_KEY_NAME, record.envelope.key_version)
        except SecretStoreUnavailableError as error:
            raise CredentialStoreUnavailableError from error
        except SecretKeyNotFoundError as error:
            raise CredentialReentryRequiredError from error
        try:
            return self._cipher.decrypt(
                record.envelope,
                key=key,
                authenticated_data=record.binding.authenticated_data(),
            )
        except CipherUnavailableError as error:
            raise CredentialStoreUnavailableError from error
        except CipherAuthenticationError as error:
            raise CredentialReentryRequiredError from error

    def _metadata(self, record: EncryptedCredential) -> CredentialMetadata:
        try:
            plaintext = self._decrypt(record)
        except (CredentialReentryRequiredError, CredentialStoreUnavailableError):
            return CredentialMetadata(
                id=record.id,
                provider=record.provider,
                api_base=record.api_base,
                model=record.model,
                configured=False,
                masked_suffix=None,
                requires_reentry=True,
                updated_at=record.updated_at,
            )
        return self._metadata_from_plaintext(record, plaintext)

    @staticmethod
    def _metadata_from_plaintext(
        record: EncryptedCredential,
        plaintext: SensitiveText,
    ) -> CredentialMetadata:
        key = plaintext.reveal()
        # Old keys are masked on read, so the same rule applies without rewriting ciphertext.
        suffix = key[-4:] if len(key) > 8 else ""
        return CredentialMetadata(
            id=record.id,
            provider=record.provider,
            api_base=record.api_base,
            model=record.model,
            configured=True,
            masked_suffix=f"••••{suffix}",
            requires_reentry=False,
            updated_at=record.updated_at,
        )


def select_llm_task_credential(
    *,
    credential_service: CredentialService | None,
    user_id: str,
    environment_configured: bool,
    environment_provider: str,
    environment_api_base: str,
    environment_model: str,
) -> LlmTaskCredential | None:
    """Select native owner metadata, or the legacy Compose environment configuration.

    Presence of a credential service selects the protected personal boundary. Only the tested
    GLM/DeepSeek Chat runtimes are selectable; a missing, unsupported or re-entry-required
    credential never falls back to another provider or an environment key.
    """

    if credential_service is not None:
        metadata = credential_service.default_for_user(user_id)
        if (
            metadata is None
            or not metadata.configured
            or metadata.requires_reentry
            or metadata.provider.strip().lower() not in {"glm", "deepseek"}
            or not (metadata.api_base or "").strip()
            or not (metadata.model or "").strip()
        ):
            return None
        return LlmTaskCredential(
            credential_id=metadata.id,
            provider=metadata.provider.strip().lower(),
            api_base=(metadata.api_base or "").strip(),
            model=(metadata.model or "").strip(),
        )
    if not environment_configured:
        return None
    return LlmTaskCredential(
        credential_id=None,
        provider=environment_provider.strip().lower(),
        api_base=environment_api_base.strip(),
        model=environment_model.strip(),
    )


class WorkerCredentialResolver:
    """Narrow worker-facing resolver; task ownership is the only accepted identity input."""

    def __init__(self, credential_service: CredentialService) -> None:
        self._credential_service = credential_service

    def resolve(self, task: QueuedTask) -> ResolvedLlmCredential:
        return self._credential_service.resolve_for_task(task)
