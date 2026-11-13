from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.application.bootstrap import BootstrapTokenRegistration
from app.application.owner_setup import (
    LocalActiveUserLimitError,
    LocalOwnerSetupCommit,
    LocalOwnerSetupSnapshot,
    LocalOwnerSetupTransaction,
    SetupRepositoryContractError,
    SetupStateVersionConflict,
)
from app.application.runtime import RuntimeSetupState
from app.application.setup import (
    SetupStateSnapshot,
    SetupStatusSnapshot,
    SetupTransitionError,
    SetupTransitionPolicy,
)
from app.application.setup import (
    SetupStateVersionConflict as InstallationStateVersionConflict,
)
from app.domain.owner_setup import (
    LocalOwnerProvisioning,
    PendingLocalOwnerClaim,
    StoredBootstrapToken,
)
from app.domain.setup import InstallationMode, IssuedBootstrapToken
from app.infrastructure.db.models import (
    BootstrapTokenModel,
    GroupMembershipModel,
    GroupModel,
    InstallationStateModel,
    UserModel,
    UserOnboardingStateModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)

_INSTALLATION_KEY = "installation"
_BOOTSTRAP_KEY = "owner-bootstrap"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    # SQLite stores timestamps without a timezone suffix.  The schema contract is UTC, so the
    # adapter restores that information at its persistence boundary.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _begin_serialized(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.begin()


@dataclass(slots=True)
class DatabaseRuntimeSetupStateProvider:
    session_factory: Any

    def get_setup_state(self) -> RuntimeSetupState:
        with self.session_factory() as session:
            installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
            if installation is None:
                raise SetupRepositoryContractError(
                    "The installation-state singleton is missing."
                )
            try:
                return RuntimeSetupState(installation.state)
            except ValueError as error:
                raise SetupRepositoryContractError(
                    "The installation-state singleton contains an unknown state."
                ) from error


@dataclass(slots=True)
class DatabaseSetupStateStore:
    session_factory: sessionmaker[Session]
    transition_policy: SetupTransitionPolicy = SetupTransitionPolicy()

    def read_snapshot(self) -> SetupStateSnapshot:
        status = self.read_status()
        return SetupStateSnapshot(
            state=status.state,
            state_version=status.state_version,
        )

    def read_status(self) -> SetupStatusSnapshot:
        with self.session_factory() as session:
            installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
            if installation is None:
                raise SetupRepositoryContractError(
                    "The installation-state singleton is missing."
                )
            try:
                state = RuntimeSetupState(installation.state)
            except ValueError as error:
                raise SetupRepositoryContractError(
                    "The installation-state singleton contains an unknown state."
                ) from error
            try:
                installation_mode = (
                    InstallationMode(installation.installation_mode)
                    if installation.installation_mode is not None
                    else None
                )
            except ValueError as error:
                raise SetupRepositoryContractError(
                    "The installation-state singleton contains an unknown mode."
                ) from error
            return SetupStatusSnapshot(
                state=state,
                state_version=installation.state_version,
                installation_mode=installation_mode,
            )

    def transition(
        self,
        *,
        expected: SetupStateSnapshot,
        target: RuntimeSetupState,
        now: datetime,
    ) -> SetupStateSnapshot:
        if now.tzinfo is None:
            raise ValueError("Setup transition time must be timezone-aware.")
        self.transition_policy.require_transition(expected.state, target)
        session = self.session_factory()
        try:
            _begin_serialized(session)
            result = session.execute(
                update(InstallationStateModel)
                .where(
                    InstallationStateModel.singleton_key == _INSTALLATION_KEY,
                    InstallationStateModel.state == expected.state.value,
                    InstallationStateModel.state_version == expected.state_version,
                )
                .values(
                    state=target.value,
                    state_version=expected.state_version + 1,
                    completed_at=(now if target is RuntimeSetupState.READY else None),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise InstallationStateVersionConflict(
                    "Installation setup state changed concurrently."
                )
            session.commit()
            return SetupStateSnapshot(
                state=target,
                state_version=expected.state_version + 1,
            )
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


@dataclass(slots=True)
class DatabaseBootstrapTokenRegistry:
    session_factory: sessionmaker[Session]
    transition_policy: SetupTransitionPolicy = SetupTransitionPolicy()

    def register(
        self,
        issued: IssuedBootstrapToken,
        *,
        now: datetime,
    ) -> BootstrapTokenRegistration:
        if now.tzinfo is None or issued.expires_at.tzinfo is None:
            raise ValueError("Bootstrap token timestamps must be timezone-aware.")
        if issued.expires_at <= now:
            raise ValueError("Bootstrap token expiry must be later than issuance.")

        session = self.session_factory()
        try:
            _begin_serialized(session)
            installation = session.get(InstallationStateModel, _INSTALLATION_KEY)
            if installation is None:
                raise SetupRepositoryContractError(
                    "The installation-state singleton is missing."
                )
            current = RuntimeSetupState(installation.state)
            self.transition_policy.require_bootstrap_mutable(current)
            if current in {
                RuntimeSetupState.CONFIGURING,
                RuntimeSetupState.VERIFYING_RUNTIME,
            }:
                raise SetupTransitionError(
                    f"Owner bootstrap is already complete while setup is {current.value}."
                )
            active_user_count = session.scalar(
                select(func.count()).select_from(UserModel).where(UserModel.status == "ACTIVE")
            )
            if active_user_count:
                raise LocalActiveUserLimitError(
                    "Local Edition already has an active owner."
                )

            if current is not RuntimeSetupState.AWAITING_OWNER:
                self.transition_policy.require_transition(
                    current,
                    RuntimeSetupState.AWAITING_OWNER,
                )
            installation.state = RuntimeSetupState.AWAITING_OWNER.value
            installation.state_version += 1
            installation.updated_at = now

            token = session.get(BootstrapTokenModel, _BOOTSTRAP_KEY)
            if token is None:
                token = BootstrapTokenModel(singleton_key=_BOOTSTRAP_KEY)
                session.add(token)
            token.token_digest = issued.token_digest
            token.expires_at = issued.expires_at
            token.consumed_at = None
            token.request_digest = None
            token.owner_user_id = None
            token.workspace_id = None
            token.created_at = now
            token.updated_at = now
            session.commit()
            return BootstrapTokenRegistration(
                setup_state=RuntimeSetupState.AWAITING_OWNER,
                state_version=installation.state_version,
            )
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


@dataclass(slots=True)
class DatabaseLocalOwnerSetupRepository:
    session_factory: sessionmaker[Session]

    @contextmanager
    def transaction(self) -> Iterator[LocalOwnerSetupTransaction]:
        session = self.session_factory()
        try:
            _begin_serialized(session)
            yield _DatabaseLocalOwnerSetupTransaction(session)
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


@dataclass(slots=True)
class _DatabaseLocalOwnerSetupTransaction:
    session: Session

    def load_for_update(self) -> LocalOwnerSetupSnapshot:
        installation = self.session.get(InstallationStateModel, _INSTALLATION_KEY)
        if installation is None:
            raise SetupRepositoryContractError(
                "The installation-state singleton is missing."
            )
        try:
            setup_state = RuntimeSetupState(installation.state)
        except ValueError as error:
            raise SetupRepositoryContractError(
                "The installation-state singleton contains an unknown state."
            ) from error
        try:
            installation_mode = (
                InstallationMode(installation.installation_mode)
                if installation.installation_mode is not None
                else None
            )
        except ValueError as error:
            raise SetupRepositoryContractError(
                "The installation-state singleton contains an unknown mode."
            ) from error

        active_user_count = int(
            self.session.scalar(
                select(func.count()).select_from(UserModel).where(UserModel.status == "ACTIVE")
            )
            or 0
        )
        pending_user_ids = list(
            self.session.scalars(
                select(UserModel.id)
                .where(UserModel.status == "PENDING_CLAIM")
                .order_by(UserModel.id)
            ).all()
        )
        if len(pending_user_ids) > 1:
            raise SetupRepositoryContractError(
                "More than one pending local owner exists."
            )

        pending_claim: PendingLocalOwnerClaim | None = None
        if pending_user_ids:
            pending_owner_id = pending_user_ids[0]
            if installation.owner_user_id != pending_owner_id:
                raise SetupRepositoryContractError(
                    "The pending owner does not match the installation owner."
                )
            workspace_ids = list(
                self.session.scalars(
                    select(WorkspaceMembershipModel.workspace_id)
                    .where(
                        WorkspaceMembershipModel.user_id == pending_owner_id,
                        WorkspaceMembershipModel.role == "OWNER",
                    )
                    .order_by(WorkspaceMembershipModel.workspace_id)
                ).all()
            )
            if len(workspace_ids) != 1:
                raise SetupRepositoryContractError(
                    "The pending owner must have exactly one owner workspace."
                )
            pending_claim = PendingLocalOwnerClaim(
                owner_user_id=pending_owner_id,
                workspace_id=workspace_ids[0],
            )
        elif installation.owner_user_id is not None and active_user_count == 0:
            raise SetupRepositoryContractError(
                "The installation owner is neither active nor pending claim."
            )

        token_model = self.session.get(BootstrapTokenModel, _BOOTSTRAP_KEY)
        token = None
        if token_model is not None:
            token = StoredBootstrapToken(
                token_digest=token_model.token_digest,
                expires_at=_aware_utc(token_model.expires_at),
                consumed_at=(
                    _aware_utc(token_model.consumed_at)
                    if token_model.consumed_at is not None
                    else None
                ),
                request_digest=token_model.request_digest,
                owner_user_id=token_model.owner_user_id,
                workspace_id=token_model.workspace_id,
            )
        return LocalOwnerSetupSnapshot(
            setup_state=setup_state,
            state_version=installation.state_version,
            installation_mode=installation_mode,
            owner_user_id=installation.owner_user_id,
            active_user_count=active_user_count,
            bootstrap_token=token,
            pending_claim=pending_claim,
        )

    def apply(
        self,
        *,
        expected_state: RuntimeSetupState,
        expected_state_version: int,
        expected_token_digest: str,
        provisioning: LocalOwnerProvisioning,
    ) -> LocalOwnerSetupCommit:
        installation = self.session.get(InstallationStateModel, _INSTALLATION_KEY)
        token = self.session.get(BootstrapTokenModel, _BOOTSTRAP_KEY)
        if installation is None or token is None:
            raise SetupStateVersionConflict("Setup metadata changed during owner claim.")
        if (
            installation.state != expected_state.value
            or installation.state_version != expected_state_version
            or installation.installation_mode is not None
            or token.token_digest != expected_token_digest
            or token.consumed_at is not None
        ):
            raise SetupStateVersionConflict("Setup metadata changed during owner claim.")

        conflicting_user_id = self.session.scalar(
            select(UserModel.id).where(
                UserModel.username == provisioning.username,
                UserModel.id != provisioning.owner_user_id,
            )
        )
        if conflicting_user_id is not None:
            raise SetupRepositoryContractError("The requested owner username is unavailable.")

        if provisioning.claim_existing:
            owner = self.session.get(UserModel, provisioning.owner_user_id)
            workspace = self.session.get(WorkspaceModel, provisioning.workspace_id)
            if owner is None or owner.status != "PENDING_CLAIM" or workspace is None:
                raise SetupStateVersionConflict("The pending owner claim changed.")
            owner.username = provisioning.username
            owner.display_name = provisioning.display_name
            owner.password_hash = provisioning.password_hash
            owner.status = "ACTIVE"
            owner.updated_at = provisioning.consumed_at
            workspace.kind = provisioning.workspace_kind
            workspace.name = provisioning.workspace_name
            workspace.updated_at = provisioning.consumed_at
        else:
            if installation.owner_user_id is not None:
                raise SetupStateVersionConflict("An installation owner already exists.")
            owner = UserModel(
                id=provisioning.owner_user_id,
                username=provisioning.username,
                display_name=provisioning.display_name,
                password_hash=provisioning.password_hash,
                status="ACTIVE",
                created_at=provisioning.consumed_at,
                updated_at=provisioning.consumed_at,
            )
            workspace = WorkspaceModel(
                id=provisioning.workspace_id,
                kind=provisioning.workspace_kind,
                name=provisioning.workspace_name,
                created_at=provisioning.consumed_at,
                updated_at=provisioning.consumed_at,
            )
            self.session.add_all([owner, workspace])
            self.session.flush()

        administrators = self.session.scalar(
            select(GroupModel).where(GroupModel.key == provisioning.administrators_group_key)
        )
        if administrators is None:
            raise SetupRepositoryContractError(
                "The administrators group catalog row is missing."
            )
        if self.session.get(
            GroupMembershipModel,
            (provisioning.owner_user_id, administrators.id),
        ) is None:
            self.session.add(
                GroupMembershipModel(
                    user_id=provisioning.owner_user_id,
                    group_id=administrators.id,
                    created_at=provisioning.consumed_at,
                )
            )
        membership = self.session.get(
            WorkspaceMembershipModel,
            (provisioning.workspace_id, provisioning.owner_user_id),
        )
        if membership is None:
            self.session.add(
                WorkspaceMembershipModel(
                    workspace_id=provisioning.workspace_id,
                    user_id=provisioning.owner_user_id,
                    role=provisioning.workspace_role,
                    created_at=provisioning.consumed_at,
                )
            )
        elif membership.role != provisioning.workspace_role:
            raise SetupRepositoryContractError(
                "The owner workspace membership has an unexpected role."
            )

        onboarding = self.session.get(
            UserOnboardingStateModel,
            provisioning.owner_user_id,
        )
        if onboarding is None:
            self.session.add(
                UserOnboardingStateModel(
                    user_id=provisioning.owner_user_id,
                    status="COMPLETED",
                    completed_at=provisioning.consumed_at,
                    created_at=owner.created_at,
                    updated_at=provisioning.consumed_at,
                )
            )
        else:
            onboarding.status = "COMPLETED"
            if onboarding.completed_at is None:
                onboarding.completed_at = provisioning.consumed_at
            onboarding.updated_at = provisioning.consumed_at

        self.session.flush()
        from app.application.libraries import grant_new_workspace_member_defaults

        grant_new_workspace_member_defaults(
            self.session, user_id=provisioning.owner_user_id,
            workspace_id=provisioning.workspace_id,
            granted_by_user_id=provisioning.owner_user_id, source="OWNER_SETUP",
        )
        token_update = self.session.execute(
            update(BootstrapTokenModel)
            .where(
                BootstrapTokenModel.singleton_key == _BOOTSTRAP_KEY,
                BootstrapTokenModel.token_digest == expected_token_digest,
                BootstrapTokenModel.consumed_at.is_(None),
            )
            .values(
                consumed_at=provisioning.consumed_at,
                request_digest=provisioning.request_digest,
                owner_user_id=provisioning.owner_user_id,
                workspace_id=provisioning.workspace_id,
                updated_at=provisioning.consumed_at,
            )
        )
        installation_update = self.session.execute(
            update(InstallationStateModel)
            .where(
                InstallationStateModel.singleton_key == _INSTALLATION_KEY,
                InstallationStateModel.state == expected_state.value,
                InstallationStateModel.state_version == expected_state_version,
                InstallationStateModel.installation_mode.is_(None),
            )
            .values(
                state=RuntimeSetupState.CONFIGURING.value,
                state_version=expected_state_version + 1,
                installation_mode=provisioning.installation_mode.value,
                edition=provisioning.edition.value,
                owner_user_id=provisioning.owner_user_id,
                updated_at=provisioning.consumed_at,
            )
        )
        if token_update.rowcount != 1 or installation_update.rowcount != 1:
            raise SetupStateVersionConflict("Setup metadata changed during owner claim.")
        return LocalOwnerSetupCommit(
            owner_user_id=provisioning.owner_user_id,
            workspace_id=provisioning.workspace_id,
            setup_state=RuntimeSetupState.CONFIGURING,
            state_version=expected_state_version + 1,
            installation_mode=provisioning.installation_mode,
        )
