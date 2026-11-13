"""Persist the write-once first-run installation mode.

Revision ID: 20260903_0024
Revises: 20260902_0023
Create Date: 2026-09-03

The mode remains nullable only while first-run setup has not claimed an owner.  Existing Local
installations are conservatively classified as SINGLE_USER and moved to the unified SERVER
edition when they are READY or when their installation owner is already ACTIVE.  A NEW database
and a legacy database whose synthetic PENDING_CLAIM owner has not yet been claimed remain
unselected, so their first trusted setup ceremony may explicitly choose SINGLE_USER or MULTI_USER
without replacing any stable identity, workspace, paper, block, or storage identifier.

Mode selection is immutable application state and cannot be represented in the old schema.
Downgrade is intentionally unsupported; restore the coordinated pre-upgrade database, storage,
and secret-scope backup instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0024"
down_revision: str | None = "20260902_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("installation_state") as batch_op:
        batch_op.add_column(
            sa.Column("installation_mode", sa.String(length=32), nullable=True)
        )
        batch_op.create_check_constraint(
            "installation_state_valid_installation_mode",
            "installation_mode IS NULL OR "
            "installation_mode IN ('SINGLE_USER', 'MULTI_USER')",
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE installation_state
            SET installation_mode = 'SINGLE_USER',
                edition = 'SERVER'
            WHERE edition = 'LOCAL'
              AND (
                state = 'READY'
                OR EXISTS (
                  SELECT 1
                  FROM users
                  WHERE users.id = installation_state.owner_user_id
                    AND users.status = 'ACTIVE'
                )
              )
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "The write-once installation mode is part of the identity and authorization boundary; "
        "restore the coordinated pre-upgrade database, storage, and secret-scope backup."
    )
