"""Separate annotation highlight and comment presentation state.

Revision ID: 20260821_0006
Revises: 20260821_0005
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260821_0006"
down_revision: str | None = "20260821_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_highlighted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute(
        "UPDATE annotations SET is_highlighted = 1 "
        "WHERE annotation_type IN ('HIGHLIGHT', 'USER_NOTE')"
    )


def downgrade() -> None:
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.drop_column("is_highlighted")
