from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.application.runtime import RuntimeSetupState
from app.application.setup import SetupTransitionPolicy
from app.domain.owner_setup import (
    LocalOwnerCredentials,
    LocalOwnerProvisioning,
    PendingLocalOwnerClaim,
    StoredBootstrapToken,
    edition_for_installation_mode,
    normalized_workspace_name,
)
from app.domain.setup import BootstrapTokenIssuer, InstallationMode, PasswordHasher
from app.domain.users import (
    PERSONAL_ACCOUNT,
    normalized_display_name,
    normalized_username,
    validated_password,
)

_REQUEST_DIGEST_DOMAIN = b"academatepaper.owner-setup-request.v2\0"


class BootstrapTokenInvalidError(PermissionError):
    pass


class BootstrapTokenExpiredError(PermissionError):
    pass


class BootstrapTokenReplayError(PermissionError):
    pass


class LocalActiveUserLimitError(PermissionError):
    pass


class LocalOwnerSetupConflictError(RuntimeError):
    pass


class SetupStateVersionConflict(LocalOwnerSetupConflictError):
    """Raised by a repository when the installation compare-and-swap loses a race."""


class SetupRepositoryContractError(LocalOwnerSetupConflictError):
    pass


@dataclass(frozen=True, slots=True)
class LocalOwnerSetupSnapshot:
    """One consistent installation snapshot read inside a repository transaction."""

    setup_state: RuntimeSetupState
    state_version: int
    installation_mode: InstallationMode | None
    owner_user_id: str | None
    active_user_count: int
    bootstrap_token: StoredBootstrapToken | None
    pending_claim: PendingLocalOwnerClaim | None


@dataclass(frozen=True, slots=True)
class LocalOwnerSetupCommit:
    owner_user_id: str
    workspace_id: str
    setup_state: RuntimeSetupState
    state_version: int
    installation_mode: InstallationMode


@dataclass(frozen=True, slots=True)
class LocalOwnerSetupResult:
    owner_user_id: str
    workspace_id: str
    setup_state: RuntimeSetupState
    state_version: int
    installation_mode: InstallationMode
    replayed: bool


class LocalOwnerSetupTransaction(Protocol):
    """Atomic repository view used by ``LocalOwnerSetupService``.

    ``apply`` must compare-and-swap both ``expected_state`` and ``expected_state_version`` and
    verify ``expected_token_digest`` before changing data.  A successful call must, atomically:

    * consume the token with request/owner/workspace metadata;
    * create the fresh owner or update the specified pending owner in place;
    * create or retain the administrators and private-workspace OWNER memberships;
    * write the immutable installation mode and its mapped edition;
    * set the installation owner, transition to CONFIGURING, and increment state_version once.

    Zero affected installation rows must raise ``SetupStateVersionConflict``.  The surrounding
    transaction must roll back every staged row if this method or context-manager commit fails.
    """

    def load_for_update(self) -> LocalOwnerSetupSnapshot: ...

    def apply(
        self,
        *,
        expected_state: RuntimeSetupState,
        expected_state_version: int,
        expected_token_digest: str,
        provisioning: LocalOwnerProvisioning,
    ) -> LocalOwnerSetupCommit: ...


class LocalOwnerSetupRepository(Protocol):
    """Transaction boundary for owner setup; implementations must commit on clean exit only."""

    def transaction(self) -> AbstractContextManager[LocalOwnerSetupTransaction]: ...


class IdentifierFactory(Protocol):
    def __call__(self) -> str: ...


def _new_identifier() -> str:
    return str(uuid.uuid4())


@dataclass(slots=True)
class LocalOwnerSetupService:
    """Atomically create or claim the sole Local Edition owner.

    This service deliberately exposes no token-issuance method.  A trusted launcher issues and
    registers the short-lived challenge before a browser can reach setup; this browser-facing
    operation only verifies and consumes that already-persisted proof.
    """

    repository: LocalOwnerSetupRepository
    token_issuer: BootstrapTokenIssuer
    password_hasher: PasswordHasher
    transition_policy: SetupTransitionPolicy = SetupTransitionPolicy()
    identifier_factory: IdentifierFactory = _new_identifier

    def submit(
        self,
        credentials: LocalOwnerCredentials,
        *,
        now: datetime | None = None,
    ) -> LocalOwnerSetupResult:
        submitted_at = now or datetime.now(UTC)
        if submitted_at.tzinfo is None:
            raise ValueError("Owner setup time must be timezone-aware.")

        username = normalized_username(credentials.username)
        display_name = normalized_display_name(credentials.display_name)
        password = validated_password(credentials.password)
        workspace_name = normalized_workspace_name(credentials.workspace_name)
        request_digest = self._request_digest(
            raw_token=credentials.bootstrap_token,
            username=username,
            display_name=display_name,
            password=password,
            installation_mode=credentials.installation_mode,
            workspace_name=workspace_name,
            expected_state_version=credentials.expected_state_version,
            credential_request_binding=credentials.credential_request_binding,
        )

        with self.repository.transaction() as transaction:
            snapshot = transaction.load_for_update()
            self.transition_policy.require_bootstrap_mutable(snapshot.setup_state)
            token = snapshot.bootstrap_token
            if token is None or not self.token_issuer.matches(
                credentials.bootstrap_token,
                token.token_digest,
            ):
                raise BootstrapTokenInvalidError("Bootstrap token is invalid.")

            replay_result = self._idempotent_replay(
                snapshot=snapshot,
                token=token,
                request_digest=request_digest,
            )
            if replay_result is not None:
                return replay_result

            if snapshot.state_version != credentials.expected_state_version:
                raise SetupStateVersionConflict(
                    "Installation setup state changed concurrently."
                )
            if snapshot.installation_mode is not None:
                raise LocalOwnerSetupConflictError(
                    "The installation mode has already been selected."
                )

            if credentials.installation_mode is not InstallationMode.SINGLE_USER:
                raise ValueError("New installations support only SINGLE_USER.")

            if username != PERSONAL_ACCOUNT or display_name != PERSONAL_ACCOUNT:
                raise ValueError("The personal account name is fixed to admin.")

            if token.expires_at.tzinfo is None:
                raise SetupRepositoryContractError(
                    "Persisted bootstrap expiry must be timezone-aware."
                )
            if submitted_at >= token.expires_at:
                raise BootstrapTokenExpiredError("Bootstrap token has expired.")
            if snapshot.active_user_count != 0:
                raise LocalActiveUserLimitError(
                    "Local Edition permits exactly one active user."
                )

            self.transition_policy.require_transition(
                snapshot.setup_state,
                RuntimeSetupState.CONFIGURING,
            )
            owner_user_id, workspace_id, claim_existing = self._provisioning_target(snapshot)
            password_hash = self.password_hasher.hash(password)
            provisioning = LocalOwnerProvisioning(
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                username=username,
                display_name=display_name,
                password_hash=password_hash,
                claim_existing=claim_existing,
                consumed_at=submitted_at,
                request_digest=request_digest,
                installation_mode=credentials.installation_mode,
                edition=edition_for_installation_mode(credentials.installation_mode),
                workspace_name=workspace_name,
            )
            commit = transaction.apply(
                expected_state=snapshot.setup_state,
                expected_state_version=snapshot.state_version,
                expected_token_digest=token.token_digest,
                provisioning=provisioning,
            )
            self._validate_commit(snapshot, provisioning, commit)
            return LocalOwnerSetupResult(
                owner_user_id=commit.owner_user_id,
                workspace_id=commit.workspace_id,
                setup_state=commit.setup_state,
                state_version=commit.state_version,
                installation_mode=commit.installation_mode,
                replayed=False,
            )

    @staticmethod
    def _request_digest(
        *,
        raw_token: str,
        username: str,
        display_name: str,
        password: str,
        installation_mode: InstallationMode,
        workspace_name: str,
        expected_state_version: int,
        credential_request_binding: str | None = None,
    ) -> str:
        payload = json.dumps(
            {
                "display_name": display_name,
                "credential_request_binding": credential_request_binding,
                "expected_state_version": expected_state_version,
                "installation_mode": installation_mode.value,
                "password": password,
                "username": username,
                "workspace_name": workspace_name,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hmac.new(
            raw_token.encode("utf-8"),
            _REQUEST_DIGEST_DOMAIN + payload,
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256-v1:{digest}"

    @staticmethod
    def _idempotent_replay(
        *,
        snapshot: LocalOwnerSetupSnapshot,
        token: StoredBootstrapToken,
        request_digest: str,
    ) -> LocalOwnerSetupResult | None:
        if token.consumed_at is None:
            if any(
                value is not None
                for value in (token.request_digest, token.owner_user_id, token.workspace_id)
            ):
                raise SetupRepositoryContractError(
                    "Unconsumed bootstrap token contains consumption metadata."
                )
            return None

        if token.request_digest != request_digest:
            raise BootstrapTokenReplayError(
                "Bootstrap token was already consumed by a different request."
            )
        if token.owner_user_id is None or token.workspace_id is None:
            raise SetupRepositoryContractError(
                "Consumed bootstrap token is missing its owner result metadata."
            )
        if snapshot.owner_user_id != token.owner_user_id:
            raise SetupRepositoryContractError(
                "Consumed bootstrap owner does not match the installation owner."
            )
        if snapshot.installation_mode is None:
            raise SetupRepositoryContractError(
                "Consumed bootstrap token is missing the installation mode result."
            )
        return LocalOwnerSetupResult(
            owner_user_id=token.owner_user_id,
            workspace_id=token.workspace_id,
            setup_state=snapshot.setup_state,
            state_version=snapshot.state_version,
            installation_mode=snapshot.installation_mode,
            replayed=True,
        )

    def _provisioning_target(
        self,
        snapshot: LocalOwnerSetupSnapshot,
    ) -> tuple[str, str, bool]:
        pending_claim = snapshot.pending_claim
        if pending_claim is not None:
            if snapshot.owner_user_id != pending_claim.owner_user_id:
                raise SetupRepositoryContractError(
                    "Pending owner does not match the installation owner."
                )
            return pending_claim.owner_user_id, pending_claim.workspace_id, True
        if snapshot.owner_user_id is not None:
            raise LocalOwnerSetupConflictError(
                "Installation owner exists but is not a claimable pending owner."
            )
        return self.identifier_factory(), self.identifier_factory(), False

    @staticmethod
    def _validate_commit(
        snapshot: LocalOwnerSetupSnapshot,
        provisioning: LocalOwnerProvisioning,
        commit: LocalOwnerSetupCommit,
    ) -> None:
        if (
            commit.owner_user_id != provisioning.owner_user_id
            or commit.workspace_id != provisioning.workspace_id
            or commit.setup_state is not RuntimeSetupState.CONFIGURING
            or commit.state_version != snapshot.state_version + 1
            or commit.installation_mode is not provisioning.installation_mode
        ):
            raise SetupRepositoryContractError(
                "Owner setup repository returned an invalid compare-and-swap result."
            )
