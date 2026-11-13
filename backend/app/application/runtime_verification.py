from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.application.installation_identity import InstallationIdentityInvariant
from app.application.runtime import RuntimeSetupState
from app.application.setup import SetupStateSnapshot, SetupStateStore, SetupTransitionError


@dataclass(frozen=True, slots=True)
class RuntimeVerificationOutcome:
    ok: bool
    capability_states: tuple[tuple[str, str], ...] = ()
    failure_code: str | None = None


class RuntimeVerifier(Protocol):
    def verify(self) -> RuntimeVerificationOutcome: ...


class SchemaRevisionInvariant(Protocol):
    def verify(self) -> None: ...


class SchemaRevisionInvariantError(RuntimeError):
    """Raised when the application database is not at the exact supported revision."""


class RuntimeVerificationFailedError(RuntimeError):
    """Safe orchestration error that never includes command output or host paths."""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(f"Runtime verification failed ({failure_code}).")


@dataclass(frozen=True, slots=True)
class SetupRuntimeVerificationResult:
    setup_state: RuntimeSetupState
    state_version: int
    capability_states: tuple[tuple[str, str], ...]
    replayed: bool = False


@dataclass(slots=True)
class SetupRuntimeVerificationService:
    """Restricted launcher/Supervisor service that closes first-run setup after real smoke tests."""

    state_store: SetupStateStore
    verifier: RuntimeVerifier
    identity_invariant: InstallationIdentityInvariant
    schema_invariant: SchemaRevisionInvariant

    def run(
        self,
        *,
        now: datetime | None = None,
        reverify_ready: bool = False,
    ) -> SetupRuntimeVerificationResult:
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            raise ValueError("Runtime verification time must be timezone-aware.")

        try:
            self.schema_invariant.verify()
        except Exception as error:
            # Do not touch setup state through a schema this application version does not own.
            raise RuntimeVerificationFailedError("SCHEMA_REVISION_INVALID") from error

        snapshot = self.state_store.read_snapshot()
        reverifying_ready = (
            snapshot.state is RuntimeSetupState.READY and reverify_ready
        )
        if snapshot.state is RuntimeSetupState.READY and not reverifying_ready:
            self._verify_identity()
            return SetupRuntimeVerificationResult(
                setup_state=snapshot.state,
                state_version=snapshot.state_version,
                capability_states=(),
                replayed=True,
            )
        if reverifying_ready:
            pass
        elif snapshot.state in {
            RuntimeSetupState.CONFIGURING,
            RuntimeSetupState.RECOVERABLE_FAILURE,
        }:
            snapshot = self.state_store.transition(
                expected=snapshot,
                target=RuntimeSetupState.VERIFYING_RUNTIME,
                now=current_time,
            )
        elif snapshot.state is not RuntimeSetupState.VERIFYING_RUNTIME:
            raise SetupTransitionError(
                "Runtime verification requires a provisioned local owner."
            )

        try:
            self.identity_invariant.verify()
        except Exception as error:
            self._record_recoverable_failure(snapshot, current_time)
            raise RuntimeVerificationFailedError(
                "INSTALLATION_IDENTITY_INVALID"
            ) from error

        try:
            outcome = self.verifier.verify()
        except Exception as error:
            self._record_recoverable_failure(snapshot, current_time)
            raise RuntimeVerificationFailedError("RUNTIME_PROBE_EXCEPTION") from error
        if not outcome.ok:
            self._record_recoverable_failure(snapshot, current_time)
            raise RuntimeVerificationFailedError(
                outcome.failure_code or "RUNTIME_PROBE_FAILED"
            )

        if reverifying_ready:
            return SetupRuntimeVerificationResult(
                setup_state=snapshot.state,
                state_version=snapshot.state_version,
                capability_states=outcome.capability_states,
                replayed=True,
            )

        ready = self.state_store.transition(
            expected=snapshot,
            target=RuntimeSetupState.READY,
            now=current_time,
        )
        return SetupRuntimeVerificationResult(
            setup_state=ready.state,
            state_version=ready.state_version,
            capability_states=outcome.capability_states,
        )

    def _verify_identity(self) -> None:
        try:
            self.identity_invariant.verify()
        except Exception as error:
            raise RuntimeVerificationFailedError(
                "INSTALLATION_IDENTITY_INVALID"
            ) from error

    def _record_recoverable_failure(
        self,
        snapshot: SetupStateSnapshot,
        now: datetime,
    ) -> None:
        self.state_store.transition(
            expected=snapshot,
            target=RuntimeSetupState.RECOVERABLE_FAILURE,
            now=now,
        )
