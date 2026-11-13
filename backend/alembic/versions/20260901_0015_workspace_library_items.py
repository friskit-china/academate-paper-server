"""Split workspace library state from canonical papers without changing paper/block IDs.

Revision ID: 20260901_0015
Revises: 20260901_0014
Create Date: 2026-09-01

The compatibility-era columns on ``papers`` remain in place.  For a legacy database this revision
copies every paper's reading status, favorite flag, pin, and last-read position into the unique
pending-owner workspace created by revision 0014.  It never rewrites a Paper or Content Block ID.

Operators must retain the coordinated pre-upgrade database/storage backup.  Once more workspaces or
new library-only state exist, there is no lossless way to collapse them into the legacy global paper
columns.  Downgrade is therefore intentionally unsupported; restore that backup instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0015"
down_revision: str | None = "20260901_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _new_id() -> str:
    return str(uuid.uuid4())


def _legacy_owner_workspace_id(connection: sa.Connection) -> str | None:
    rows = connection.execute(
        sa.text(
            "SELECT wm.workspace_id "
            "FROM workspace_memberships AS wm "
            "JOIN installation_state AS installation "
            "  ON installation.owner_user_id = wm.user_id "
            "WHERE installation.singleton_key = :singleton_key "
            "  AND wm.role = 'OWNER' "
            "ORDER BY wm.workspace_id"
        ),
        {"singleton_key": "installation"},
    ).scalars().all()
    if len(rows) > 1:
        raise RuntimeError(
            "Legacy M5 upgrade found multiple owner workspaces; restore the pre-upgrade backup "
            "and repair the ownership records before retrying."
        )
    return rows[0] if rows else None


def _validate_last_read_block(
    connection: sa.Connection,
    *,
    paper_id: str,
    block_id: str,
) -> None:
    matching_block = connection.execute(
        sa.text(
            "SELECT 1 FROM content_blocks "
            "WHERE id = :block_id AND paper_id = :paper_id LIMIT 1"
        ),
        {"block_id": block_id, "paper_id": paper_id},
    ).scalar_one_or_none()
    if matching_block is None:
        raise RuntimeError(
            "A legacy paper has a last_read_block_id that is not one of its content blocks; "
            "restore the pre-upgrade backup and repair the stale reader position before retrying."
        )


def upgrade() -> None:
    op.create_table(
        "library_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column(
            "reading_status",
            sa.String(length=16),
            nullable=False,
            server_default="UNREAD",
        ),
        sa.Column(
            "is_favorite",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("pinned_at", sa.DateTime(timezone=True)),
        sa.Column("last_read_block_id", sa.String(length=36)),
        sa.Column("last_read_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["last_read_block_id"],
            ["content_blocks.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "paper_id"),
    )
    op.create_index(
        "ix_library_items_workspace_status",
        "library_items",
        ["workspace_id", "reading_status", "created_at"],
    )
    op.create_index("ix_library_items_paper", "library_items", ["paper_id"])
    op.create_index(
        "ix_library_items_workspace_favorite",
        "library_items",
        ["workspace_id", "is_favorite"],
    )
    op.create_index(
        "ix_library_items_workspace_pinned",
        "library_items",
        ["workspace_id", "pinned_at"],
    )

    connection = op.get_bind()
    papers = connection.execute(
        sa.text(
            "SELECT id, reading_status, is_favorite, pinned_at, last_read_block_id, "
            "last_read_at, created_at FROM papers ORDER BY id"
        )
    ).mappings().all()
    if not papers:
        return

    workspace_id = _legacy_owner_workspace_id(connection)
    if workspace_id is None:
        raise RuntimeError(
            "Legacy papers exist without the pending owner's private workspace; restore the "
            "pre-upgrade backup and rerun the complete linear migration chain."
        )

    library_items = sa.table(
        "library_items",
        sa.column("id"),
        sa.column("workspace_id"),
        sa.column("paper_id"),
        sa.column("reading_status"),
        sa.column("is_favorite"),
        sa.column("pinned_at"),
        sa.column("last_read_block_id"),
        sa.column("last_read_at"),
        sa.column("created_at"),
    )
    values: list[dict[str, object]] = []
    for paper in papers:
        paper_id = paper["id"]
        last_read_block_id = paper["last_read_block_id"]
        if last_read_block_id is not None:
            _validate_last_read_block(
                connection,
                paper_id=paper_id,
                block_id=last_read_block_id,
            )
        values.append(
            {
                "id": _new_id(),
                "workspace_id": workspace_id,
                "paper_id": paper_id,
                "reading_status": paper["reading_status"] or "UNREAD",
                "is_favorite": bool(paper["is_favorite"]),
                "pinned_at": paper["pinned_at"],
                "last_read_block_id": last_read_block_id,
                "last_read_at": paper["last_read_at"],
                "created_at": paper["created_at"],
            }
        )
    connection.execute(library_items.insert(), values)


def downgrade() -> None:
    raise RuntimeError(
        "M5 workspace-library downgrade is unsupported; restore the coordinated pre-upgrade "
        "database, storage, and secret-scope backup."
    )
