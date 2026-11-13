"""Add one-time browser launch proofs and revocable local sessions.

Revision ID: 20260901_0021
Revises: 20260901_0020
Create Date: 2026-09-01

Raw launch and session tokens never enter SQLite.  Launch proofs are single-use and short-lived;
browser sessions are bound to the installation-state generation that issued them and can be
expired or revoked without changing user data.  Downgrade is intentionally unsupported because
removing this boundary would re-enable the pre-M5 trusted-BFF identity bypass.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0021"
down_revision: str | None = "20260901_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_launch_tokens",
        sa.Column("singleton_key", sa.String(length=32), nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "singleton_key = 'browser-launch'",
            name="local_launch_tokens_singleton_key",
        ),
        sa.PrimaryKeyConstraint("singleton_key"),
        sa.UniqueConstraint("token_digest", name="uq_local_launch_tokens_digest"),
    )
    op.create_index(
        "ix_local_launch_tokens_expiry",
        "local_launch_tokens",
        ["expires_at"],
    )

    op.create_table(
        "local_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("installation_state_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "installation_state_version >= 0",
            name="local_sessions_nonnegative_installation_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_local_sessions_digest"),
    )
    op.create_index("ix_local_sessions_expiry", "local_sessions", ["expires_at"])
    op.create_index(
        "ix_local_sessions_active",
        "local_sessions",
        ["revoked_at", "expires_at"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "M5 local-session downgrade is unsupported; restore the coordinated pre-upgrade "
        "database, storage, and secret-scope backup."
    )
