"""Persist per-user onboarding completion state.

Revision ID: 20260903_0028
Revises: 20260903_0027
Create Date: 2026-09-03

Every user that predates this feature is backfilled as COMPLETED so a schema upgrade cannot lock
an existing owner or member out of their library.  New account creation paths are responsible for
writing an explicit PENDING or COMPLETED row in the same transaction as the user.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0028"
down_revision: str | None = "20260903_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_onboarding_states",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED')",
            name="user_onboarding_states_valid_status",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND completed_at IS NULL) OR "
            "(status = 'COMPLETED' AND completed_at IS NOT NULL)",
            name="user_onboarding_states_completion_metadata",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_user_onboarding_states_status",
        "user_onboarding_states",
        ["status"],
    )

    connection = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    onboarding_states = sa.table(
        "user_onboarding_states",
        sa.column("user_id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("completed_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    upgraded_at = datetime.now(UTC)
    existing_users = connection.execute(
        sa.select(users.c.id, users.c.created_at).order_by(users.c.id)
    ).all()
    if existing_users:
        connection.execute(
            onboarding_states.insert(),
            [
                {
                    "user_id": user_id,
                    "status": "COMPLETED",
                    "completed_at": upgraded_at,
                    "created_at": created_at,
                    "updated_at": upgraded_at,
                }
                for user_id, created_at in existing_users
            ],
        )


def downgrade() -> None:
    raise RuntimeError(
        "User onboarding state is an authorization boundary; restore the coordinated "
        "pre-upgrade data-volume snapshot instead of downgrading destructively."
    )
