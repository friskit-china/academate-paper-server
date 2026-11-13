"""Create foundation tables.

Revision ID: 20260817_0001
Revises:
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260817_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "papers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("arxiv_id", sa.String(length=32), nullable=False),
        sa.Column("arxiv_version", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "processing_stage", sa.String(length=32), nullable=False, server_default="REGISTERED"
        ),
        sa.Column("processing_health", sa.String(length=16), nullable=False, server_default="OK"),
        sa.Column("reading_status", sa.String(length=16), nullable=False, server_default="UNREAD"),
        sa.Column("last_read_block_id", sa.String(length=36), nullable=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("arxiv_id", "arxiv_version"),
    )
    op.create_table(
        "content_blocks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("block_type", sa.String(length=32), nullable=False),
        sa.Column("ordinal_path", sa.String(length=255), nullable=False),
        sa.Column("heading_level", sa.Integer(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("source_payload_json", sa.JSON(), nullable=False),
        sa.Column("render_payload_json", sa.JSON(), nullable=False),
        sa.Column("source_locator_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["content_blocks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_content_blocks_paper_ordinal", "content_blocks", ["paper_id", "ordinal_path"]
    )
    op.create_index("ix_content_blocks_parent", "content_blocks", ["parent_id"])
    op.create_index("ix_content_blocks_type", "content_blocks", ["block_type"])
    op.create_table(
        "processing_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=True),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="QUEUED"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_processing_tasks_claim", "processing_tasks", ["status", "priority", "available_at"]
    )
    op.create_index("ix_processing_tasks_paper", "processing_tasks", ["paper_id"])


def downgrade() -> None:
    op.drop_index("ix_processing_tasks_paper", table_name="processing_tasks")
    op.drop_index("ix_processing_tasks_claim", table_name="processing_tasks")
    op.drop_table("processing_tasks")
    op.drop_index("ix_content_blocks_type", table_name="content_blocks")
    op.drop_index("ix_content_blocks_parent", table_name="content_blocks")
    op.drop_index("ix_content_blocks_paper_ordinal", table_name="content_blocks")
    op.drop_table("content_blocks")
    op.drop_table("papers")
