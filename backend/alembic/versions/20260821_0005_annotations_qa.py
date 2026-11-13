"""Add annotations and paper-aware Q&A audit fields.

Revision ID: 20260821_0005
Revises: 20260819_0004
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260821_0005"
down_revision: str | None = "20260819_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=36), nullable=True),
        sa.Column("parent_annotation_id", sa.String(length=36), nullable=True),
        sa.Column("annotation_type", sa.String(length=24), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("creator_type", sa.String(length=8), nullable=False),
        sa.Column("selector_json", sa.JSON(), nullable=True),
        sa.Column("target_translation_id", sa.String(length=36), nullable=True),
        sa.Column("target_translation_input_hash", sa.String(length=64), nullable=True),
        sa.Column("resolution_status", sa.String(length=32), nullable=False),
        sa.Column("resolution_reason", sa.String(length=96), nullable=True),
        sa.Column("resolved_start_offset", sa.Integer(), nullable=True),
        sa.Column("resolved_end_offset", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("qa_status", sa.String(length=16), nullable=True),
        sa.Column("qa_generation", sa.Integer(), nullable=False),
        sa.Column("qa_error_type", sa.String(length=64), nullable=True),
        sa.Column("qa_error_code", sa.String(length=128), nullable=True),
        sa.Column("qa_error_message", sa.Text(), nullable=True),
        sa.Column("citations_json", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["block_id"], ["content_blocks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_annotation_id"], ["annotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["target_translation_id"], ["translations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_annotation_id", "qa_generation"),
    )
    op.create_index(
        "ix_annotations_paper_active",
        "annotations",
        ["paper_id", "deleted_at", "created_at"],
    )
    op.create_index("ix_annotations_block", "annotations", ["block_id"])
    op.create_index("ix_annotations_parent", "annotations", ["parent_annotation_id"])


def downgrade() -> None:
    op.drop_index("ix_annotations_parent", table_name="annotations")
    op.drop_index("ix_annotations_block", table_name="annotations")
    op.drop_index("ix_annotations_paper_active", table_name="annotations")
    op.drop_table("annotations")
