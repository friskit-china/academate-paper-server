"""Add a monotonic generation to durable task claims.

Revision ID: 20260902_0023
Revises: 20260901_0022
Create Date: 2026-09-02

Worker ids identify processes, not individual claims.  A task reclaimed after lease expiry may be
assigned to the same long-lived worker id while an old future is still completing.  The generation
distinguishes those claims so an old future cannot renew, release, commit, succeed, or fail the new
claim.  Existing rows start at generation zero and receive generation one on their next claim;
task ids, statuses, retry counts, and payloads are not rewritten.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0023"
down_revision: str | None = "20260901_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processing_tasks",
        sa.Column(
            "lease_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Task claim generations are part of the worker publication safety boundary; restore the "
        "coordinated pre-upgrade database, storage, and secret-scope backup."
    )
