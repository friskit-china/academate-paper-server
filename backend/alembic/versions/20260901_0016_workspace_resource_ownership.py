"""Add workspace and creator/requester ownership to user-visible resources.

Revision ID: 20260901_0016
Revises: 20260901_0015
Create Date: 2026-09-01

The revision first adds nullable foreign keys, backfills every legacy row to the one pending owner
and private workspace created by 0014, and only then makes workspace ownership (and user comment /
bookmark creators) non-null.  Existing row IDs, Paper IDs, Block IDs and payloads are unchanged.

The old global uniqueness rules are replaced with workspace-scoped rules.  A downgrade cannot
losslessly collapse data from multiple workspaces back into those global constraints.  Keep the
coordinated pre-upgrade database/storage backup; downgrade is intentionally unsupported and that
backup is the recovery path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0016"
down_revision: str | None = "20260901_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _legacy_owner_scope(connection: sa.Connection) -> tuple[str, str] | None:
    rows = connection.execute(
        sa.text(
            "SELECT installation.owner_user_id, membership.workspace_id "
            "FROM installation_state AS installation "
            "JOIN workspace_memberships AS membership "
            "  ON membership.user_id = installation.owner_user_id "
            "WHERE installation.singleton_key = 'installation' "
            "  AND membership.role = 'OWNER' "
            "ORDER BY membership.workspace_id"
        )
    ).all()
    if len(rows) > 1:
        raise RuntimeError(
            "M5 ownership backfill found multiple owner workspaces; restore the pre-upgrade "
            "backup and repair the identity records before retrying."
        )
    if not rows:
        return None
    owner_user_id, workspace_id = rows[0]
    if owner_user_id is None:
        raise RuntimeError("The legacy installation owner reference is unexpectedly null.")
    return owner_user_id, workspace_id


def _scoped_row_count(connection: sa.Connection) -> int:
    return sum(
        connection.execute(
            sa.select(sa.func.count()).select_from(sa.table(table_name))
        ).scalar_one()
        for table_name in (
            "translations",
            "annotations",
            "annotation_comments",
            "block_bookmarks",
            "paper_maps",
        )
    )


def _add_ownership_columns() -> None:
    with op.batch_alter_table("translations") as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("requested_by_user_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_translations_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_translations_requested_by_user_id_users",
            "users",
            ["requested_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("annotations") as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("creator_user_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_annotations_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_annotations_creator_user_id_users",
            "users",
            ["creator_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("annotation_comments") as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("creator_user_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_annotation_comments_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_annotation_comments_creator_user_id_users",
            "users",
            ["creator_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("block_bookmarks") as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("creator_user_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_block_bookmarks_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_block_bookmarks_creator_user_id_users",
            "users",
            ["creator_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("paper_maps") as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("requested_by_user_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_paper_maps_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_paper_maps_requested_by_user_id_users",
            "users",
            ["requested_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def _backfill_legacy_scope(connection: sa.Connection, owner_id: str, workspace_id: str) -> None:
    for table_name in ("translations", "paper_maps"):
        connection.execute(
            sa.text(
                f"UPDATE {table_name} "  # noqa: S608 - fixed migration-owned table names
                "SET workspace_id = :workspace_id, requested_by_user_id = :owner_id"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        )
    # Pre-M5 global rows may contain the editable installation prompt. There was no persisted
    # provenance proving that plaintext safe to share with a future workspace member, so preserve
    # the row and IDs but conservatively classify its prompt/result as owner-private.
    connection.execute(
        sa.text(
            "UPDATE translations SET prompt_kind = 'custom' "
            "WHERE prompt_kind = 'global'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE annotations SET workspace_id = :workspace_id, "
            "creator_user_id = :owner_id"
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    )
    for table_name in ("annotation_comments", "block_bookmarks"):
        connection.execute(
            sa.text(
                f"UPDATE {table_name} "  # noqa: S608 - fixed migration-owned table names
                "SET workspace_id = :workspace_id, creator_user_id = :owner_id"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        )


def _replace_global_constraints() -> None:
    op.drop_index("ix_translations_cache", table_name="translations")
    op.drop_index("ix_translations_block_language_status", table_name="translations")
    with op.batch_alter_table("translations") as batch_op:
        batch_op.drop_constraint("uq_translations_block_id", type_="unique")
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.alter_column(
            "requested_by_user_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_translations_workspace_block_language_input",
            ["workspace_id", "block_id", "language", "input_hash"],
        )
    op.create_index(
        "ix_translations_workspace_block_language_status",
        "translations",
        ["workspace_id", "block_id", "language", "status"],
    )
    op.create_index(
        "ix_translations_workspace_cache",
        "translations",
        ["workspace_id", "input_hash", "status"],
    )
    op.create_index(
        "ix_translations_requester",
        "translations",
        ["requested_by_user_id", "workspace_id"],
    )

    for index_name in (
        "ix_annotations_paper_active",
        "ix_annotations_block",
        "ix_annotations_parent",
        "ix_annotations_anchor",
    ):
        op.drop_index(index_name, table_name="annotations")
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.drop_constraint("uq_annotations_parent_annotation_id", type_="unique")
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "annotations_user_creator",
            "creator_type <> 'USER' OR creator_user_id IS NOT NULL",
        )
        batch_op.create_unique_constraint(
            "uq_annotations_workspace_parent_generation",
            ["workspace_id", "parent_annotation_id", "qa_generation"],
        )
    op.create_index(
        "ix_annotations_workspace_paper_active",
        "annotations",
        ["workspace_id", "paper_id", "deleted_at", "created_at"],
    )
    op.create_index(
        "ix_annotations_workspace_block",
        "annotations",
        ["workspace_id", "block_id"],
    )
    op.create_index(
        "ix_annotations_workspace_parent",
        "annotations",
        ["workspace_id", "parent_annotation_id"],
    )
    op.create_index(
        "ix_annotations_workspace_anchor",
        "annotations",
        ["workspace_id", "anchor_annotation_id"],
    )
    op.create_index(
        "ix_annotations_creator",
        "annotations",
        ["creator_user_id", "workspace_id"],
    )

    op.drop_index("ix_annotation_comments_active", table_name="annotation_comments")
    with op.batch_alter_table("annotation_comments") as batch_op:
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.alter_column(
            "creator_user_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
    op.create_index(
        "ix_annotation_comments_workspace_active",
        "annotation_comments",
        ["workspace_id", "annotation_id", "deleted_at", "created_at"],
    )
    op.create_index(
        "ix_annotation_comments_creator",
        "annotation_comments",
        ["creator_user_id", "workspace_id"],
    )

    op.drop_index("ix_block_bookmarks_paper_created", table_name="block_bookmarks")
    with op.batch_alter_table("block_bookmarks") as batch_op:
        batch_op.drop_constraint("uq_block_bookmarks_paper_id", type_="unique")
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.alter_column(
            "creator_user_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_block_bookmarks_workspace_paper_block",
            ["workspace_id", "paper_id", "block_id"],
        )
    op.create_index(
        "ix_block_bookmarks_workspace_paper_created",
        "block_bookmarks",
        ["workspace_id", "paper_id", "created_at"],
    )
    op.create_index(
        "ix_block_bookmarks_creator",
        "block_bookmarks",
        ["creator_user_id", "workspace_id"],
    )

    op.drop_index("ix_paper_maps_current", table_name="paper_maps")
    with op.batch_alter_table("paper_maps") as batch_op:
        batch_op.drop_constraint("uq_paper_maps_paper_id", type_="unique")
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.alter_column(
            "requested_by_user_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_paper_maps_workspace_paper_generation",
            ["workspace_id", "paper_id", "generation"],
        )
    op.create_index(
        "ix_paper_maps_workspace_current",
        "paper_maps",
        ["workspace_id", "paper_id", "generation", "status"],
    )
    op.create_index(
        "ix_paper_maps_requester",
        "paper_maps",
        ["requested_by_user_id", "workspace_id"],
    )


def upgrade() -> None:
    connection = op.get_bind()
    existing_count = _scoped_row_count(connection)
    owner_scope = _legacy_owner_scope(connection)
    if existing_count and owner_scope is None:
        raise RuntimeError(
            "Legacy user-scoped rows exist without exactly one pending owner workspace; restore "
            "the pre-upgrade backup and repair the M5 identity migration before retrying."
        )

    _add_ownership_columns()
    if owner_scope is not None:
        _backfill_legacy_scope(connection, *owner_scope)
    _replace_global_constraints()


def downgrade() -> None:
    raise RuntimeError(
        "M5 workspace ownership downgrade is unsupported; restore the coordinated pre-upgrade "
        "database, storage, and secret-scope backup."
    )
