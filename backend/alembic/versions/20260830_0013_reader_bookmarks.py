"""Add persistent paper preferences and reader block bookmarks.

Revision ID: 20260830_0013
Revises: 20260829_0012
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0013"
down_revision: str | None = "20260829_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "papers",
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("papers", sa.Column("pinned_at", sa.DateTime(timezone=True)))
    op.create_index("ix_papers_favorite", "papers", ["is_favorite"])
    op.create_index("ix_papers_pinned", "papers", ["pinned_at"])
    op.create_table(
        "block_bookmarks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["block_id"], ["content_blocks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paper_id", "block_id"),
    )
    op.create_index(
        "ix_block_bookmarks_paper_created",
        "block_bookmarks",
        ["paper_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_block_bookmarks_paper_created", table_name="block_bookmarks")
    op.drop_table("block_bookmarks")
    op.drop_index("ix_papers_pinned", table_name="papers")
    op.drop_index("ix_papers_favorite", table_name="papers")
    op.drop_column("papers", "pinned_at")
    op.drop_column("papers", "is_favorite")
