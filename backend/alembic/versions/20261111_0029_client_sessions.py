"""Add independent client session families without replacing v1 sessions."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261111_0029"
down_revision: str | None = "20260903_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("auth_generation", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "web_sessions",
        sa.Column("auth_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "server_identity",
        sa.Column("singleton_key", sa.String(32), primary_key=True),
        sa.Column("server_id", sa.String(36), nullable=False, unique=True),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO server_identity (singleton_key,server_id) VALUES ('server',:id)",
        ),
        {"id": str(uuid.uuid4())},
    )
    op.create_table(
        "client_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("active_workspace_id", sa.String(36), nullable=False),
        sa.Column("client_kind", sa.String(16), nullable=False),
        sa.Column("client_name", sa.String(128), nullable=False),
        sa.Column("device_name", sa.String(128), nullable=False),
        sa.Column("installation_state_version", sa.Integer(), nullable=False),
        sa.Column("auth_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revocation_reason", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["active_workspace_id", "user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("client_kind IN ('native','web')", name="client_sessions_kind"),
        sa.CheckConstraint("auth_generation >= 0", name="client_sessions_generation"),
    )
    op.create_index("ix_client_sessions_user_id", "client_sessions", ["user_id"])
    for table in ("client_access_credentials", "client_refresh_credentials"):
        extra = (
            [sa.Column("auth_generation", sa.Integer(), nullable=False)]
            if table == "client_access_credentials"
            else [
                sa.Column("sequence", sa.Integer(), nullable=False),
                sa.Column("consumed_at", sa.DateTime(timezone=True)),
                sa.Column("replacement_id", sa.String(36)),
                sa.UniqueConstraint("session_id", "sequence"),
            ]
        )
        op.create_table(
            table,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("session_id", sa.String(36), nullable=False),
            sa.Column("token_digest", sa.String(128), nullable=False, unique=True),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["session_id"], ["client_sessions.id"], ondelete="CASCADE"),
            *extra,
        )
        op.create_index(f"ix_{table}_session_id", table, ["session_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Client authentication is a security boundary; restore a coordinated snapshot."
    )
