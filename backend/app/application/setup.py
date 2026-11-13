from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.application.runtime import RuntimeSetupState
from app.domain.setup import InstallationMode


class SetupTransitionError(ValueError):
    pass


class SetupStateVersionConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SetupStateSnapshot:
    state: RuntimeSetupState
    state_version: int


@dataclass(frozen=True, slots=True)
class SetupStatusSnapshot:
    """Public, secret-free first-run configuration status."""

    state: RuntimeSetupState
    state_version: int
    installation_mode: InstallationMode | None

    @property
    def allowed_installation_modes(self) -> tuple[InstallationMode, ...]:
        if self.installation_mode is not None:
            return ()
        if self.state not in {
            RuntimeSetupState.NEW,
            RuntimeSetupState.MIGRATING,
            RuntimeSetupState.AWAITING_OWNER,
            RuntimeSetupState.RECOVERABLE_FAILURE,
        }:
            return ()
        return (InstallationMode.SINGLE_USER,)


class SetupStateStore(Protocol):
    """Durable compare-and-swap port used by migration/runtime orchestration."""

    def read_snapshot(self) -> SetupStateSnapshot: ...

    def read_status(self) -> SetupStatusSnapshot: ...

    def transition(
        self,
        *,
        expected: SetupStateSnapshot,
        target: RuntimeSetupState,
        now: datetime,
    ) -> SetupStateSnapshot: ...


_ALLOWED_TRANSITIONS: dict[RuntimeSetupState, frozenset[RuntimeSetupState]] = {
    RuntimeSetupState.NEW: frozenset(
        {
            RuntimeSetupState.MIGRATING,
            RuntimeSetupState.AWAITING_OWNER,
            RuntimeSetupState.BLOCKED,
        }
    ),
    RuntimeSetupState.MIGRATING: frozenset(
        {
            RuntimeSetupState.AWAITING_OWNER,
            RuntimeSetupState.RECOVERABLE_FAILURE,
            RuntimeSetupState.BLOCKED,
        }
    ),
    RuntimeSetupState.AWAITING_OWNER: frozenset(
        {
            RuntimeSetupState.CONFIGURING,
            RuntimeSetupState.RECOVERABLE_FAILURE,
            RuntimeSetupState.BLOCKED,
        }
    ),
    RuntimeSetupState.CONFIGURING: frozenset(
        {
            RuntimeSetupState.VERIFYING_RUNTIME,
            RuntimeSetupState.RECOVERABLE_FAILURE,
            RuntimeSetupState.BLOCKED,
        }
    ),
    RuntimeSetupState.VERIFYING_RUNTIME: frozenset(
        {
            RuntimeSetupState.READY,
            RuntimeSetupState.RECOVERABLE_FAILURE,
            RuntimeSetupState.BLOCKED,
        }
    ),
    RuntimeSetupState.RECOVERABLE_FAILURE: frozenset(
        {
            RuntimeSetupState.MIGRATING,
            RuntimeSetupState.AWAITING_OWNER,
            RuntimeSetupState.CONFIGURING,
            RuntimeSetupState.VERIFYING_RUNTIME,
            RuntimeSetupState.BLOCKED,
        }
    ),
    # READY closes every bootstrap/configuration mutation, but a trusted runtime verifier may
    # still fail the installation closed when an external prerequisite disappears or changes.
    RuntimeSetupState.READY: frozenset({RuntimeSetupState.RECOVERABLE_FAILURE}),
    RuntimeSetupState.BLOCKED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class SetupTransitionPolicy:
    """Pure installation-state policy used by every persistence adapter.

    Persistence implementations still have to compare-and-swap a state version in one database
    transaction.  Keeping the graph here prevents a UI, Supervisor, or repository adapter from
    inventing a shortcut such as NEW -> READY.
    """

    def require_transition(
        self,
        current: RuntimeSetupState,
        target: RuntimeSetupState,
    ) -> None:
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise SetupTransitionError(
                f"Installation setup cannot transition from {current.value} to {target.value}."
            )

    def require_bootstrap_mutable(self, state: RuntimeSetupState) -> None:
        if state in {RuntimeSetupState.READY, RuntimeSetupState.BLOCKED}:
            raise SetupTransitionError(
                f"Bootstrap mutation is closed while installation state is {state.value}."
            )
