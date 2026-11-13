"""Local metadata retention for the complete Server (no network, LLM or business deletion).

Default policy: retain event IDs for 30 days and at most 100,000 rows, evicting a monotonic
prefix with its cursor floor in one transaction. Expired API receipts lose response bodies and
headers, but their hashed tombstones are never removed or made executable again. Client families
are removed only after absolute expiry or 7 days after revocation; consumed refresh credentials
remain for the full lifetime of every retained family. Expired/revoked integration metadata has a
7-day grace period. Personal papers, library entries, annotations and queue tasks are untouched.

Each category uses a separate short SQLite write transaction. MaintenanceTick invokes this at
most once per hour per worker, including when the previous attempt failed, with no overlapping
runs. A failed step propagates without exposing metadata; already-committed retention steps are
safe and can be repeated at the next scheduled tick.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update

from app.infrastructure.db.api_contract_models import ApiIdempotencyModel
from app.infrastructure.db.client_models import ClientSessionModel
from app.infrastructure.db.sync_models import (
    IntegrationTokenModel,
    ResourceChangeModel,
    SyncStateModel,
)

EVENT_FLOOR_KEY = "events"


def _begin_write(session) -> None:
    if session.bind is None or session.bind.dialect.name != "sqlite":
        raise ValueError("Server maintenance requires the supported SQLite profile")
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    # Deleting a dead family must also remove its access/refresh records. Fail rather than
    # silently leave credential orphans if an incorrectly configured factory disables FKs.
    if session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
        raise ValueError("Server maintenance requires foreign-key enforcement")


def _event_retention(factory, now: datetime, *, days: int, maximum: int) -> tuple[int, int]:
    cutoff = now - timedelta(days=days)
    with factory() as session:
        _begin_write(session)
        state = session.get(SyncStateModel, EVENT_FLOOR_KEY)
        if state is None:
            state = SyncStateModel(singleton_key=EVENT_FLOOR_KEY, retained_after=0)
            session.add(state)
        aged = (
            session.scalar(
                select(func.max(ResourceChangeModel.id)).where(
                    ResourceChangeModel.created_at < cutoff,
                )
            )
            or 0
        )
        # This is the first evicted ID when retaining the newest `maximum` rows.
        excess = (
            session.scalar(
                select(ResourceChangeModel.id)
                .order_by(
                    ResourceChangeModel.id.desc(),
                )
                .offset(maximum)
                .limit(1)
            )
            or 0
        )
        floor = max(state.retained_after, aged, excess)
        removed = session.execute(
            delete(ResourceChangeModel).where(
                ResourceChangeModel.id <= floor,
            )
        ).rowcount
        # Prefix deletion is deliberate: even with a clock correction there can be no gap
        # hidden behind an apparently replayable cursor. Sequence is the replay authority.
        state.retained_after = floor
        session.commit()
        return removed, floor


def _receipt_retention(factory, now: datetime) -> int:
    with factory() as session:
        _begin_write(session)
        removed = session.execute(
            update(ApiIdempotencyModel)
            .where(
                ApiIdempotencyModel.expires_at <= now,
                or_(
                    ApiIdempotencyModel.state != "NON_REPLAYABLE",
                    ApiIdempotencyModel.response_json.is_not(None),
                    ApiIdempotencyModel.response_headers_json.is_not(None),
                    ApiIdempotencyModel.status_code.is_not(None),
                ),
            )
            .values(
                state="NON_REPLAYABLE",
                response_json=None,
                response_headers_json=None,
                status_code=None,
            ),
            execution_options={"synchronize_session": False},
        ).rowcount
        session.commit()
        return removed


def _family_retention(factory, now: datetime, *, grace_days: int) -> int:
    cutoff = now - timedelta(days=grace_days)
    with factory() as session:
        _begin_write(session)
        removed = session.execute(
            delete(ClientSessionModel).where(
                or_(
                    ClientSessionModel.expires_at <= now,
                    and_(
                        ClientSessionModel.revoked_at.is_not(None),
                        ClientSessionModel.revoked_at <= cutoff,
                    ),
                )
            ),
            execution_options={"synchronize_session": False},
        ).rowcount
        # ON DELETE CASCADE only touches access/refresh records belonging to those dead families.
        # Never prune consumed refresh rows individually; replay must remain detectable.
        session.commit()
        return removed


def _integration_retention(factory, now: datetime, *, grace_days: int) -> int:
    cutoff = now - timedelta(days=grace_days)
    with factory() as session:
        _begin_write(session)
        removed = session.execute(
            delete(IntegrationTokenModel).where(
                or_(
                    IntegrationTokenModel.expires_at <= cutoff,
                    and_(
                        IntegrationTokenModel.revoked_at.is_not(None),
                        IntegrationTokenModel.revoked_at <= cutoff,
                    ),
                )
            ),
            execution_options={"synchronize_session": False},
        ).rowcount
        session.commit()
        return removed


def run_server_maintenance(
    factory,
    *,
    now: datetime | None = None,
    event_retention_days: int = 30,
    event_max_rows: int = 100_000,
    metadata_grace_days: int = 7,
) -> dict[str, int]:
    """Return aggregate counters only. Every write affects retention metadata, never content.

    Optional policy overrides allow isolated validation; production callers use the defaults.
    A timezone-aware clock is required so a host-local timezone cannot shift retention cutoffs.
    """
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Maintenance requires a timezone-aware clock")
    for value in (event_retention_days, event_max_rows, metadata_grace_days):
        if type(value) is not int or value < 1:
            raise ValueError("Retention limits must be positive integers")
    current = current.astimezone(UTC)
    receipts = _receipt_retention(factory, current)
    families = _family_retention(factory, current, grace_days=metadata_grace_days)
    integration = _integration_retention(factory, current, grace_days=metadata_grace_days)
    # Integration deletion can itself emit a metadata tombstone. Bound the feed after all
    # maintenance-generated events, so this completed run respects the event cap as well.
    events, floor = _event_retention(
        factory,
        current,
        days=event_retention_days,
        maximum=event_max_rows,
    )
    return {
        "events_deleted": events,
        "event_floor": floor,
        "receipt_bodies_cleared": receipts,
        "client_families_deleted": families,
        "integration_tokens_deleted": integration,
    }


class MaintenanceTick:
    """Thread-safe, single-flight heartbeat callback with a monotonic attempt cooldown."""

    def __init__(
        self,
        factory,
        interval_seconds: float = 3600,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("Maintenance interval must be finite and positive")
        self.factory = factory
        self.interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._last_started: float | None = None
        self._lock = threading.Lock()

    def __call__(self) -> dict[str, int] | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            now = self._monotonic()
            if self._last_started is not None and now - self._last_started < self.interval_seconds:
                return None
            self._last_started = now
            return run_server_maintenance(self.factory)
        finally:
            self._lock.release()
