"""Persist the exact provider-bound Q&A user context snapshot.

Revision ID: 20260822_0008
Revises: 20260822_0007
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260822_0008"
down_revision: str | None = "20260822_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.add_column(sa.Column("qa_context_json", sa.JSON()))


def downgrade() -> None:
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.drop_column("qa_context_json")
