"""Persist compare-and-swap setup state and one-time bootstrap metadata.

Revision ID: 20260901_0019
Revises: 20260901_0018
Create Date: 2026-09-01

Only a one-way digest of the launcher-issued bootstrap token is stored.  The token consumption
result is retained in the same singleton row so an identical retry can return the original owner
and workspace without provisioning a second identity.  ``state_version`` provides the durable
compare-and-swap generation used by every setup mutation.

Setup metadata and claimed identity cannot be collapsed losslessly into the pre-M5 model.
Downgrade is intentionally unsupported; restore the coordinated pre-upgrade database, storage,
and secret-scope backup instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0019"
down_revision: str | None = "20260901_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("installation_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "state_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint(
            "installation_state_nonnegative_version",
            "state_version >= 0",
        )

    op.create_table(
        "bootstrap_tokens",
        sa.Column("singleton_key", sa.String(length=32), nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("request_digest", sa.String(length=128)),
        sa.Column("owner_user_id", sa.String(length=36)),
        sa.Column("workspace_id", sa.String(length=36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "singleton_key = 'owner-bootstrap'",
            name="bootstrap_tokens_singleton_key",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND request_digest IS NULL "
            " AND owner_user_id IS NULL AND workspace_id IS NULL) OR "
            "(consumed_at IS NOT NULL AND request_digest IS NOT NULL "
            " AND owner_user_id IS NOT NULL AND workspace_id IS NOT NULL)",
            name="bootstrap_tokens_complete_consumption",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("singleton_key"),
        sa.UniqueConstraint("token_digest", name="uq_bootstrap_tokens_digest"),
    )
    op.create_index("ix_bootstrap_tokens_expiry", "bootstrap_tokens", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError(
        "M5 setup-metadata downgrade is unsupported; restore the coordinated pre-upgrade "
        "database, storage, and secret-scope backup."
    )
