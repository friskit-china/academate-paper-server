from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.domain.os_adapters import ToolchainCapability

WORKER_HEARTBEAT_SCHEMA_VERSION = 1


class WorkerRuntimeState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    DRAINING = "DRAINING"
    DRAIN_TIMEOUT = "DRAIN_TIMEOUT"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    """A deliberately small, safe-to-display worker runtime summary.

    All variable capability names come from ``ToolchainCapability``.  The contract contains no
    arbitrary strings, paths, ports, hostnames, command lines, environment values, or provider
    diagnostics, so the persistence adapter cannot accidentally turn a heartbeat into a secret or
    local-system disclosure channel.
    """

    instance_id: str
    state: WorkerRuntimeState
    observed_at: datetime
    accepting_tasks: bool
    active_tasks: int
    task_capacity: int
    runtime_capabilities: tuple[ToolchainCapability, ...] = ()
    schema_version: int = WORKER_HEARTBEAT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        try:
            UUID(self.instance_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("worker heartbeat instance_id must be a UUID") from error
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("worker heartbeat observed_at must be timezone-aware")
        if self.active_tasks < 0:
            raise ValueError("worker heartbeat active_tasks must not be negative")
        if self.task_capacity < 1:
            raise ValueError("worker heartbeat task_capacity must be positive")
        if self.active_tasks > self.task_capacity:
            raise ValueError("worker heartbeat active_tasks cannot exceed task_capacity")
        if self.accepting_tasks and self.state is not WorkerRuntimeState.READY:
            raise ValueError("only a READY worker may accept tasks")
        if any(
            not isinstance(capability, ToolchainCapability)
            for capability in self.runtime_capabilities
        ):
            raise ValueError("worker runtime capabilities must use the fixed capability enum")
        if self.schema_version != WORKER_HEARTBEAT_SCHEMA_VERSION:
            raise ValueError("unsupported worker heartbeat schema version")

    def safe_summary(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "instance_id": self.instance_id,
            "state": self.state.value,
            "observed_at": self.observed_at.isoformat(),
            "accepting_tasks": self.accepting_tasks,
            "active_tasks": self.active_tasks,
            "task_capacity": self.task_capacity,
            "runtime_capabilities": sorted(
                capability.value for capability in self.runtime_capabilities
            ),
        }


class StopRequest(Protocol):
    @property
    def requested(self) -> bool: ...

    def wait(self, timeout_seconds: float) -> bool:
        """Wait for a stop request, returning true when stop was requested."""

        ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class WorkerReadinessProbe(Protocol):
    def is_ready(self) -> bool:
        """Return false for a valid but incomplete setup; raise on hard invariants."""

        ...


class WorkerHeartbeatStore(Protocol):
    def publish(self, heartbeat: WorkerHeartbeat) -> None: ...

    def latest(self) -> WorkerHeartbeat | None: ...
