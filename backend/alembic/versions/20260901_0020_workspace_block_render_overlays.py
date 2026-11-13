"""Add workspace-scoped derived render overlays.

Revision ID: 20260901_0020
Revises: 20260901_0019
Create Date: 2026-09-01

Existing canonical block payloads, including legacy AI repair provenance, remain untouched.  New
workspace-derived repairs use this table and are valid only while ``base_content_hash`` matches the
canonical block revision.  Downgrade is intentionally unsupported because deleting these rows can
discard user-visible derived work; restore a coordinated database/storage backup instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0020"
down_revision: str | None = "20260901_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_block_render_overlays",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("base_content_hash", sa.String(length=64), nullable=False),
        sa.Column("render_payload_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(base_content_hash) = 64",
            name="workspace_block_render_overlays_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["block_id"], ["content_blocks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "block_id",
            "base_content_hash",
            name="uq_workspace_block_render_overlays_base",
        ),
    )
    op.create_index(
        "ix_workspace_block_render_overlays_lookup",
        "workspace_block_render_overlays",
        ["workspace_id", "block_id", "base_content_hash"],
    )
    op.create_index(
        "ix_workspace_block_render_overlays_requester",
        "workspace_block_render_overlays",
        ["requested_by_user_id", "workspace_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "M5 workspace-render-overlay downgrade is unsupported; restore the coordinated "
        "database, storage, and secret-scope backup."
    )
