"""Add revocable authenticated Server browser sessions.

Revision ID: 20260903_0025
Revises: 20260903_0024
Create Date: 2026-09-03

Only a domain-separated digest of the browser token is stored. Every session is bound to its
authenticated user, active workspace membership, and the installation-state generation that
issued it. This table is deliberately separate from trusted-local launch sessions: Server
authentication must never accept a local launch proof or infer the installation owner.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0025"
down_revision: str | None = "20260903_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("active_workspace_id", sa.String(length=36), nullable=False),
        sa.Column("installation_state_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "installation_state_version >= 0",
            name="web_sessions_nonnegative_installation_version",
        ),
        sa.ForeignKeyConstraint(
            ["active_workspace_id", "user_id"],
            [
                "workspace_memberships.workspace_id",
                "workspace_memberships.user_id",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_web_sessions_digest"),
    )
    op.create_index("ix_web_sessions_expiry", "web_sessions", ["expires_at"])
    op.create_index("ix_web_sessions_active", "web_sessions", ["revoked_at", "expires_at"])
    op.create_index(
        "ix_web_sessions_user_workspace",
        "web_sessions",
        ["user_id", "active_workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_web_sessions_user_workspace", table_name="web_sessions")
    op.drop_index("ix_web_sessions_active", table_name="web_sessions")
    op.drop_index("ix_web_sessions_expiry", table_name="web_sessions")
    op.drop_table("web_sessions")
