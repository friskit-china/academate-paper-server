"""Make user-private translation and bookmark uniqueness requester-aware.

Revision ID: 20260901_0022
Revises: 20260901_0021
Create Date: 2026-09-01

Workspace-derived global-prompt translations retain one shared row per workspace/input.  Private
prompt translations and bookmarks instead include their requesting user in the uniqueness key, so
one workspace member cannot reserve another member's private resource identity.  The previous
constraints were stricter, so this migration never merges, deletes, or rewrites existing rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0022"
down_revision: str | None = "20260901_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("block_bookmarks") as batch_op:
        batch_op.drop_constraint(
            "uq_block_bookmarks_workspace_paper_block",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_block_bookmarks_workspace_creator_paper_block",
            ["workspace_id", "creator_user_id", "paper_id", "block_id"],
        )

    with op.batch_alter_table("translations") as batch_op:
        batch_op.drop_constraint(
            "uq_translations_workspace_block_language_input",
            type_="unique",
        )
    op.create_index(
        "uq_translations_workspace_shared_input",
        "translations",
        ["workspace_id", "block_id", "language", "input_hash"],
        unique=True,
        sqlite_where=sa.text("prompt_kind = 'global'"),
        postgresql_where=sa.text("prompt_kind = 'global'"),
    )
    op.create_index(
        "uq_translations_workspace_private_input",
        "translations",
        [
            "workspace_id",
            "requested_by_user_id",
            "block_id",
            "language",
            "input_hash",
        ],
        unique=True,
        sqlite_where=sa.text("prompt_kind != 'global'"),
        postgresql_where=sa.text("prompt_kind != 'global'"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "M5 requester-aware private-resource uniqueness cannot be collapsed safely; restore the "
        "coordinated pre-upgrade database, storage, and secret-scope backup."
    )
