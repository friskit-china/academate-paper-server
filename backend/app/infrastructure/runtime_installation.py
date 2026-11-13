from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.application.installation_identity import InstallationIdentityInvariantError
from app.application.runtime import RuntimeSetupState
from app.domain.runtime import RuntimeEdition, RuntimeProfile
from app.domain.setup import InstallationMode
from app.infrastructure.db.models import InstallationStateModel, UserModel
from app.infrastructure.installation_identity import (
    DatabaseInstallationOwnerIdentityInvariant,
)


class RuntimeInstallationInvariantError(RuntimeError):
    """Raised before a runtime component can touch application work."""


@dataclass(frozen=True, slots=True)
class RuntimeInstallationSnapshot:
    setup_state: RuntimeSetupState
    edition: RuntimeEdition
    installation_mode: InstallationMode | None
    owner_user_id: str | None
    active_user_count: int
    owner_is_active: bool


@dataclass(frozen=True, slots=True)
class DatabaseRuntimeInstallationModeProvider:
    """Read the write-once mode from the installation singleton.

    The capability API uses this provider rather than request values or process configuration.
    ``None`` is valid only while the first-run ceremony has not committed its final selection.
    """

    session_factory: Any

    def get_installation_mode(self) -> InstallationMode | None:
        with self.session_factory() as session:
            installation = session.get(InstallationStateModel, "installation")
            if installation is None:
                raise RuntimeInstallationInvariantError(
                    "The installation-state singleton is missing; run migrations first."
                )
            if installation.installation_mode is None:
                return None
            try:
                return InstallationMode(installation.installation_mode)
            except ValueError as error:
                raise RuntimeInstallationInvariantError(
                    "The installation state contains an unsupported installation mode."
                ) from error


class DatabaseWorkerReadinessProbe:
    """Database-backed READY gate for the single-node Docker Server runtime."""

    def __init__(self, session_factory: Any, runtime_profile: RuntimeProfile) -> None:
        self._session_factory = session_factory
        self._runtime_profile = runtime_profile

    def inspect(self) -> RuntimeInstallationSnapshot:
        with self._session_factory() as session:
            installation = session.get(InstallationStateModel, "installation")
            if installation is None:
                raise RuntimeInstallationInvariantError(
                    "The installation-state singleton is missing; run migrations before the worker."
                )
            try:
                setup_state = RuntimeSetupState(installation.state)
                installed_edition = RuntimeEdition(installation.edition)
                installation_mode = (
                    InstallationMode(installation.installation_mode)
                    if installation.installation_mode is not None
                    else None
                )
            except ValueError as error:
                raise RuntimeInstallationInvariantError(
                    "The installation state contains an unsupported runtime value."
                ) from error
            active_user_count = int(
                session.scalar(
                    select(func.count()).select_from(UserModel).where(UserModel.status == "ACTIVE")
                )
                or 0
            )
            active_owner_id = session.scalar(
                select(UserModel.id).where(
                    UserModel.status == "ACTIVE",
                    UserModel.id == installation.owner_user_id,
                )
            )

        snapshot = RuntimeInstallationSnapshot(
            setup_state=setup_state,
            edition=installed_edition,
            installation_mode=installation_mode,
            owner_user_id=installation.owner_user_id,
            active_user_count=active_user_count,
            owner_is_active=active_owner_id == installation.owner_user_id
            and installation.owner_user_id is not None,
        )
        self._validate_hard_invariants(snapshot)
        return snapshot

    def is_ready(self) -> bool:
        snapshot = self.inspect()
        if snapshot.setup_state is not RuntimeSetupState.READY:
            return False
        self._verify_ready_identity(snapshot)
        return True

    def _validate_hard_invariants(self, snapshot: RuntimeInstallationSnapshot) -> None:
        # A brand-new or legacy pending-claim database deliberately retains LOCAL + NULL until
        # setup atomically selects the mode, activates the owner, and records SERVER.  The worker
        # waits without touching product state.  Once a mode exists (and always at READY), every
        # mismatch is corruption or an unsafe runtime configuration and fails closed.
        if (
            snapshot.setup_state is not RuntimeSetupState.READY
            and snapshot.installation_mode is None
        ):
            return
        if self._runtime_profile.edition is not RuntimeEdition.SERVER:
            raise RuntimeInstallationInvariantError(
                "The Docker worker requires the SERVER runtime edition."
            )
        if snapshot.edition is not self._runtime_profile.edition:
            raise RuntimeInstallationInvariantError(
                "The configured runtime edition does not match the installed database edition."
            )
        if snapshot.installation_mode is None:
            raise RuntimeInstallationInvariantError(
                "A ready Server installation must have a persisted installation mode."
            )
        if not snapshot.owner_is_active:
            raise RuntimeInstallationInvariantError(
                "The Server installation owner must be active before workers start."
            )
        if (
            snapshot.installation_mode is InstallationMode.SINGLE_USER
            and snapshot.active_user_count != 1
        ):
            raise RuntimeInstallationInvariantError(
                "Single-user mode requires exactly one active installation owner "
                "before workers start."
            )
        if (
            snapshot.installation_mode is InstallationMode.MULTI_USER
            and snapshot.active_user_count < 1
        ):
            raise RuntimeInstallationInvariantError(
                "Multi-user mode requires an active installation owner before workers start."
            )

    def _verify_ready_identity(self, snapshot: RuntimeInstallationSnapshot) -> None:
        if snapshot.installation_mode is None:
            raise RuntimeInstallationInvariantError(
                "A ready Server installation must have a persisted installation mode."
            )
        try:
            DatabaseInstallationOwnerIdentityInvariant(
                self._session_factory,
                expected_edition=RuntimeEdition.SERVER,
                expected_installation_mode=snapshot.installation_mode,
            ).verify()
        except InstallationIdentityInvariantError as error:
            raise RuntimeInstallationInvariantError(
                "The Server installation owner identity and permission graph is incomplete."
            ) from error


def require_ready_worker_installation(
    session_factory: Any,
    runtime_profile: RuntimeProfile,
) -> RuntimeInstallationSnapshot:
    """Fail closed unless the durable installation is safe for worker claims.

    Migration and setup use separate, restricted entry points.  The ordinary worker must not run
    startup backfills, initialize product storage, resolve toolchains, or claim tasks while those
    entry points still own the installation state.
    """

    readiness = DatabaseWorkerReadinessProbe(session_factory, runtime_profile)
    snapshot = readiness.inspect()
    if snapshot.setup_state is not RuntimeSetupState.READY:
        raise RuntimeInstallationInvariantError(
            f"The worker cannot start while installation setup is {snapshot.setup_state.value}."
        )
    readiness._verify_ready_identity(snapshot)
    return snapshot
