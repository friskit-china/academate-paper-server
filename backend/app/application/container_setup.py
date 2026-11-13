from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.application.runtime import RuntimeSetupState
from app.application.runtime_verification import (
    RuntimeVerificationFailedError,
    SetupRuntimeVerificationResult,
)
from app.application.setup import SetupStateStore
from app.domain.worker_runtime import StopRequest


class ContainerSetupExitState(StrEnum):
    READY = "READY"
    CANCELLED = "CANCELLED"


class ContainerSetupProcessError(RuntimeError):
    """Fail-closed setup-process error containing only a bounded diagnostic code."""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


class SetupRuntimeVerificationRunner(Protocol):
    def run(self) -> SetupRuntimeVerificationResult: ...


_WAITING_STATES = frozenset(
    {
        RuntimeSetupState.NEW,
        RuntimeSetupState.MIGRATING,
        RuntimeSetupState.AWAITING_OWNER,
        RuntimeSetupState.CONFIGURING,
    }
)
_PUBLIC_RUNTIME_FAILURE_CODES = frozenset(
    {
        "INSTALLATION_IDENTITY_INVALID",
        "RUNTIME_PROBE_EXCEPTION",
        "RUNTIME_PROBE_FAILED",
        "SCHEMA_REVISION_INVALID",
        "TOOLCHAIN_FINGERPRINT_CHANGED",
        "TOOLCHAIN_RESOLUTION_FAILED",
        "WORKER_RUNTIME_SMOKE_FAILED",
    }
)


def _public_runtime_failure_code(value: str) -> str:
    if value in _PUBLIC_RUNTIME_FAILURE_CODES:
        return value
    return "RUNTIME_VERIFICATION_FAILED"


@dataclass(slots=True)
class ContainerSetupVerificationLoop:
    """Wait for browser setup, then close it through the restricted runtime gate.

    The browser/API owns the configuration ceremony and must explicitly transition the durable
    state to ``VERIFYING_RUNTIME``.  This process never promotes ``CONFIGURING`` itself and never
    touches Paper, task, or storage state.  A durable recoverable failure gets at most one retry
    during one process lifetime; a Compose restart creates a fresh, observable retry budget.
    """

    state_store: SetupStateStore
    verification: SetupRuntimeVerificationRunner
    stop_request: StopRequest
    poll_interval_seconds: float
    retry_delay_seconds: float

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("container setup poll interval must be positive")
        if self.retry_delay_seconds < 0:
            raise ValueError("container setup retry delay must not be negative")

    def run(self) -> ContainerSetupExitState:
        recoverable_retry_used = False
        last_failure_code = "RUNTIME_VERIFICATION_FAILED"

        while not self.stop_request.requested:
            try:
                snapshot = self.state_store.read_snapshot()
            except Exception as error:
                raise ContainerSetupProcessError("SETUP_STATE_UNAVAILABLE") from error

            if snapshot.state is RuntimeSetupState.READY:
                return ContainerSetupExitState.READY
            if snapshot.state is RuntimeSetupState.BLOCKED:
                raise ContainerSetupProcessError("SETUP_BLOCKED")

            if snapshot.state in _WAITING_STATES:
                if self.stop_request.wait(self.poll_interval_seconds):
                    return ContainerSetupExitState.CANCELLED
                continue

            if snapshot.state is RuntimeSetupState.RECOVERABLE_FAILURE:
                if recoverable_retry_used:
                    raise ContainerSetupProcessError(last_failure_code)
                recoverable_retry_used = True
                if self.stop_request.wait(self.retry_delay_seconds):
                    return ContainerSetupExitState.CANCELLED
            elif snapshot.state is not RuntimeSetupState.VERIFYING_RUNTIME:
                raise ContainerSetupProcessError("SETUP_STATE_INVALID")

            try:
                result = self.verification.run()
            except RuntimeVerificationFailedError as error:
                last_failure_code = _public_runtime_failure_code(error.failure_code)
                # The verification service has atomically recorded RECOVERABLE_FAILURE. Re-read
                # that durable state before deciding whether the one process-local retry remains.
                continue
            except Exception as error:
                raise ContainerSetupProcessError(
                    "RUNTIME_VERIFICATION_EXCEPTION"
                ) from error

            if result.setup_state is RuntimeSetupState.READY:
                return ContainerSetupExitState.READY
            raise ContainerSetupProcessError("RUNTIME_VERIFICATION_STATE_INVALID")

        return ContainerSetupExitState.CANCELLED
