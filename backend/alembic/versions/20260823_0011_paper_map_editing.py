"""Add editable labels to paper-map edges.

Revision ID: 20260823_0011
Revises: 20260823_0010
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260823_0011"
down_revision: str | None = "20260823_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    edge_columns = {column["name"] for column in inspector.get_columns("paper_map_edges")}
    if "label" not in edge_columns:
        op.add_column("paper_map_edges", sa.Column("label", sa.Text()))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    edge_columns = {column["name"] for column in inspector.get_columns("paper_map_edges")}
    if "label" in edge_columns:
        with op.batch_alter_table("paper_map_edges") as batch_op:
            batch_op.drop_column("label")
