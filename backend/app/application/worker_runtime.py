from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock

from app.domain.os_adapters import ToolchainCapability
from app.domain.worker_runtime import (
    Clock,
    StopRequest,
    WorkerHeartbeat,
    WorkerHeartbeatStore,
    WorkerReadinessProbe,
    WorkerRuntimeState,
)


@dataclass(frozen=True, slots=True)
class WorkerDrainResult[TaskT, ResultT]:
    completed: tuple[tuple[Future[ResultT], TaskT], ...]
    abandoned: tuple[tuple[Future[ResultT], TaskT], ...]


class ActiveTaskClaims:
    """Thread-safe claim registry shared by the worker loop and lease heartbeat."""

    def __init__(self) -> None:
        self._claims: set[tuple[str, int]] = set()
        self._lock = RLock()

    def add(self, task_id: str, lease_generation: int) -> None:
        if not task_id or lease_generation < 1:
            raise ValueError("An active task claim requires a task and positive generation.")
        with self._lock:
            self._claims.add((task_id, lease_generation))

    def discard(self, task_id: str, lease_generation: int) -> None:
        with self._lock:
            self._claims.discard((task_id, lease_generation))

    def snapshot(self) -> tuple[tuple[str, int], ...]:
        with self._lock:
            return tuple(sorted(self._claims))


class WorkerLifecycle:
    """OS-neutral worker lifecycle, heartbeat cadence, and claim gate."""

    def __init__(
        self,
        *,
        instance_id: str,
        stop_request: StopRequest,
        clock: Clock,
        heartbeat_store: WorkerHeartbeatStore,
        heartbeat_interval_seconds: float,
        task_capacity: int,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if task_capacity < 1:
            raise ValueError("task capacity must be positive")
        self.instance_id = instance_id
        self._stop_request = stop_request
        self._clock = clock
        self._heartbeat_store = heartbeat_store
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._task_capacity = task_capacity
        self._lock = RLock()
        self._state = WorkerRuntimeState.STARTING
        self._runtime_capabilities: tuple[ToolchainCapability, ...] = ()
        self._active_tasks = 0
        self._last_heartbeat_at: float | None = None

    @property
    def stop_requested(self) -> bool:
        return self._stop_request.requested

    @property
    def accepting_tasks(self) -> bool:
        with self._lock:
            return self._state is WorkerRuntimeState.READY and not self.stop_requested

    def wait_for_stop(self, timeout_seconds: float) -> bool:
        return self._stop_request.wait(timeout_seconds)

    def transition(
        self,
        state: WorkerRuntimeState,
        *,
        active_tasks: int = 0,
        runtime_capabilities: tuple[ToolchainCapability, ...] | None = None,
    ) -> None:
        with self._lock:
            self._state = state
            self._active_tasks = active_tasks
            if runtime_capabilities is not None:
                self._runtime_capabilities = tuple(sorted(set(runtime_capabilities), key=str))
            self.publish(force=True)

    def heartbeat(self, *, active_tasks: int) -> None:
        with self._lock:
            self._active_tasks = active_tasks
            self.publish(force=False)

    def publish(self, *, force: bool) -> None:
        with self._lock:
            monotonic_now = self._clock.monotonic()
            if (
                not force
                and self._last_heartbeat_at is not None
                and monotonic_now - self._last_heartbeat_at < self._heartbeat_interval_seconds
            ):
                return
            heartbeat = WorkerHeartbeat(
                instance_id=self.instance_id,
                state=self._state,
                observed_at=self._clock.now(),
                accepting_tasks=self.accepting_tasks,
                active_tasks=self._active_tasks,
                task_capacity=self._task_capacity,
                runtime_capabilities=self._runtime_capabilities,
            )
            self._heartbeat_store.publish(heartbeat)
            self._last_heartbeat_at = monotonic_now


def wait_for_worker_readiness(
    readiness: WorkerReadinessProbe,
    stop_request: StopRequest,
    *,
    poll_interval_seconds: float,
) -> bool:
    """Wait without touching product storage, toolchains, backfills, or the task queue."""

    if poll_interval_seconds <= 0:
        raise ValueError("readiness poll interval must be positive")
    while not stop_request.requested:
        if readiness.is_ready():
            return True
        if stop_request.wait(poll_interval_seconds):
            return False
    return False


def drain_provider_futures[TaskT, ResultT](
    futures: Mapping[Future[ResultT], TaskT],
    *,
    clock: Clock,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.05,
    on_progress: Callable[[int], None] | None = None,
) -> WorkerDrainResult[TaskT, ResultT]:
    """Bound waiting without publishing incomplete futures after the deadline.

    The caller owns result/error publication for ``completed`` entries.  ``abandoned`` entries
    deliberately remain RUNNING under their existing database lease; a replacement worker may
    reclaim them after expiry.  This service never converts an unfinished provider call into a
    synthetic success or failure.
    """

    if timeout_seconds < 0:
        raise ValueError("drain timeout must not be negative")
    if poll_interval_seconds <= 0:
        raise ValueError("drain poll interval must be positive")
    pending = dict(futures)
    completed: list[tuple[Future[ResultT], TaskT]] = []
    deadline = clock.monotonic() + timeout_seconds
    while pending:
        done = [future for future in pending if future.done()]
        for future in done:
            completed.append((future, pending.pop(future)))
        if on_progress is not None:
            on_progress(len(pending))
        remaining = deadline - clock.monotonic()
        if not pending or remaining <= 0:
            break
        clock.sleep(min(poll_interval_seconds, remaining))

    # Capture a completion that raced exactly with the deadline, but never block past it.
    for future in [future for future in pending if future.done()]:
        completed.append((future, pending.pop(future)))
    return WorkerDrainResult(
        completed=tuple(completed),
        abandoned=tuple(pending.items()),
    )
