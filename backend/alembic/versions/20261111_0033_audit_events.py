"""Structured, secret-free API mutation audit metadata."""

import sqlalchemy as sa

from alembic import op

revision = "20261111_0033"
down_revision = "20261111_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="SET NULL")
        ),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("resource_path", sa.String(512), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audit_workspace_created", "audit_events", ["workspace_id", "created_at", "id"]
    )


def downgrade() -> None:
    raise RuntimeError("Restore a complete snapshot; destructive downgrade is disabled.")
