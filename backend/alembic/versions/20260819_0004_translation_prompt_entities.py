"""Add editable translation prompts and application settings.

Revision ID: 20260819_0004
Revises: 20260819_0003
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260819_0004"
down_revision: str | None = "20260819_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    with op.batch_alter_table("translations") as batch_op:
        batch_op.add_column(
            sa.Column("system_prompt", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("prompt_kind", sa.String(length=16), nullable=False, server_default="global")
        )


def downgrade() -> None:
    with op.batch_alter_table("translations") as batch_op:
        batch_op.drop_column("prompt_kind")
        batch_op.drop_column("system_prompt")
    op.drop_table("application_settings")
