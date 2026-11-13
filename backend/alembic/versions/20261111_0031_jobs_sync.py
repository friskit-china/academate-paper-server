"""Durable v2 change feed, personal reading state and integration credentials.

Revision ID: 20261111_0031
Revises: 20261111_0030
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261111_0031"
down_revision: str | None = "20261111_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _trigger(
    table: str,
    resource: str,
    operation: str,
    *,
    workspace: str = "NULL",
    user: str = "NULL",
    paper: str = "NULL",
    version: str = "NULL",
    status: str = "NULL",
    condition: str = "1",
    identifier: str = "id",
) -> None:
    row = "OLD" if operation == "DELETE" else "NEW"
    values = [
        f"'{resource}'",
        f"{row}.{identifier}",
        f"'{operation.lower()}'",
        workspace,
        user,
        paper,
        version,
        status,
        "CURRENT_TIMESTAMP",
    ]
    sql = (
        f"CREATE TRIGGER sync_{table}_{operation.lower()} AFTER {operation} ON {table} "
        f"WHEN {condition} BEGIN INSERT INTO resource_changes "
        "(resource_type,resource_id,change_type,workspace_id,user_id,paper_id,"
        "revision,status,created_at) "
        f"VALUES ({','.join(values)}); END"
    )
    op.execute(sa.text(sql))


def upgrade() -> None:
    op.create_table(
        "resource_changes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("workspace_id", sa.String(36)),
        sa.Column("user_id", sa.String(36)),
        sa.Column("paper_id", sa.String(36)),
        sa.Column("revision", sa.Integer()),
        sa.Column("status", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_resource_changes_scope", "resource_changes", ["workspace_id", "user_id", "id"]
    )
    op.create_table(
        "sync_state",
        sa.Column("singleton_key", sa.String(32), primary_key=True),
        sa.Column("retained_after", sa.Integer(), nullable=False),
    )
    op.execute(sa.text("INSERT INTO sync_state (singleton_key,retained_after) VALUES ('events',0)"))
    op.create_table(
        "user_reading_states",
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "library_item_id",
            sa.String(36),
            sa.ForeignKey("library_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "block_id", sa.String(36), sa.ForeignKey("content_blocks.id", ondelete="SET NULL")
        ),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="reading_state_positive_revision"),
        sa.CheckConstraint("progress >= 0 AND progress <= 1", name="reading_state_progress_range"),
    )
    op.create_table(
        "job_cancellations",
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("processing_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "requested_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "integration_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("installation_state_version", sa.Integer(), nullable=False),
        sa.Column("auth_generation", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("digest", sa.String(128), nullable=False, unique=True),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_integration_tokens_user", "integration_tokens", ["user_id", "workspace_id"])
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError("The v2 durable change log currently requires SQLite")
    # These triggers are in the *same database transaction* as old API and worker writes.
    # Payloads deliberately contain only identifiers, versions and task status, never content.
    for operation in ("INSERT", "UPDATE", "DELETE"):
        row = "OLD" if operation == "DELETE" else "NEW"
        _trigger(
            "processing_tasks",
            "job",
            operation,
            workspace=f"{row}.workspace_id",
            user=f"CASE WHEN {row}.visibility='USER_PRIVATE' THEN "
            f"{row}.requested_by_user_id ELSE NULL END",
            paper=f"{row}.paper_id",
            status=f"{row}.status",
            condition=f"{row}.visibility <> 'SYSTEM' AND {row}.paper_id IS NOT NULL",
        )
        _trigger("papers", "paper", operation, paper=f"{row}.id")
        _trigger(
            "library_items",
            "library_item",
            operation,
            workspace=f"{row}.workspace_id",
            paper=f"{row}.paper_id",
        )
        _trigger(
            "annotations",
            "annotation",
            operation,
            workspace=f"{row}.workspace_id",
            user=f"{row}.creator_user_id",
            paper=f"{row}.paper_id",
            version=f"{row}.revision",
        )
        _trigger(
            "user_reading_states",
            "reading_state",
            operation,
            workspace=f"(SELECT workspace_id FROM library_items WHERE id={row}.library_item_id)",
            user=f"{row}.user_id",
            paper=f"(SELECT paper_id FROM library_items WHERE id={row}.library_item_id)",
            version=f"{row}.revision",
            identifier="library_item_id",
        )
        _trigger(
            "integration_tokens",
            "integration_token",
            operation,
            workspace=f"{row}.workspace_id",
            user=f"{row}.user_id",
        )

    _library_entry_triggers()


def _library_entry_triggers() -> None:
    op.execute(
        sa.text(
            "CREATE TRIGGER sync_library_item_audience_delete BEFORE DELETE ON library_items "
            "BEGIN INSERT INTO resource_changes "
            "(resource_type,resource_id,change_type,workspace_id,user_id,paper_id,created_at) "
            "SELECT DISTINCT 'library_item',OLD.id,'delete',OLD.workspace_id,"
            "g.user_id,OLD.paper_id,"
            "CURRENT_TIMESTAMP FROM library_entries e JOIN library_grants g "
            "ON g.library_id=e.library_id AND g.can_read=1 WHERE e.library_item_id=OLD.id; END"
        )
    )
    # Capture the entitled audience before a library link disappears. These contain only IDs;
    # replay instructs those existing readers to re-fetch and remove a now-inaccessible item.
    for operation in ("INSERT", "DELETE"):
        row = "OLD" if operation == "DELETE" else "NEW"
        timing = "BEFORE" if operation == "DELETE" else "AFTER"
        kind = "access_changed" if operation == "DELETE" else "insert"
        op.execute(
            sa.text(
                f"CREATE TRIGGER sync_library_entries_{operation.lower()} {timing} {operation} "
                "ON library_entries BEGIN INSERT INTO resource_changes "
                "(resource_type,resource_id,change_type,workspace_id,user_id,paper_id,created_at) "
                f"SELECT 'library_item', {row}.library_item_id, '{kind}', item.workspace_id, "
                "grant_row.user_id, item.paper_id, CURRENT_TIMESTAMP FROM library_items item "
                "JOIN library_grants grant_row ON grant_row.can_read=1 "
                f"AND grant_row.library_id={row}.library_id "
                f"WHERE item.id={row}.library_item_id; END"
            )
        )


def downgrade() -> None:
    raise RuntimeError(
        "Preserve the failed volume; restore a compatible full-volume snapshot instead"
    )
