"""Add library collections and explicit grants without changing existing content IDs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa

from alembic import op

revision = "20261111_0030"
down_revision = "20261111_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "libraries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "key"),
        sa.CheckConstraint("revision >= 1", name="libraries_positive_revision"),
    )
    op.create_index("ix_libraries_workspace", "libraries", ["workspace_id", "id"])
    op.create_table(
        "library_grants",
        sa.Column(
            "library_id",
            sa.String(36),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("grant_source", sa.String(32), nullable=False),
        sa.Column(
            "granted_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        *[
            sa.Column(f"can_{name}", sa.Boolean(), nullable=False)
            for name in ("read", "import", "manage", "remove")
        ],
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "library_entries",
        sa.Column(
            "library_id",
            sa.String(36),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "library_item_id",
            sa.String(36),
            sa.ForeignKey("library_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_library_entries_item", "library_entries", ["library_item_id"])
    op.create_table(
        "library_import_keys",
        sa.Column(
            "library_id",
            sa.String(36),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "library_item_id",
            sa.String(36),
            sa.ForeignKey("library_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    connection = op.get_bind()
    now = datetime.now(UTC).isoformat()
    for workspace_id, name in connection.execute(sa.text("SELECT id,name FROM workspaces")):
        members = connection.execute(
            sa.text(
                "SELECT wm.user_id,wm.role FROM workspace_memberships wm "
                "JOIN users u ON u.id=wm.user_id WHERE wm.workspace_id=:workspace "
                "ORDER BY CASE WHEN wm.role='OWNER' THEN 0 ELSE 1 END,wm.user_id",
            ),
            {"workspace": workspace_id},
        ).all()
        if not members:
            # A workspace with no principal is not made visible to an unrelated administrator.
            continue
        library_id = str(uuid5(NAMESPACE_URL, "academatepaper:default-library:" + workspace_id))
        connection.execute(
            sa.text(
                "INSERT INTO libraries(id,workspace_id,owner_user_id,key,name,revision,"
                "created_at,updated_at) "
                "VALUES(:id,:workspace,:owner,'default',:name,1,:now,:now)",
            ),
            {
                "id": library_id,
                "workspace": workspace_id,
                "owner": members[0][0],
                "name": name,
                "now": now,
            },
        )
        for user_id, role in members:
            connection.execute(
                sa.text(
                    "INSERT INTO library_grants(library_id,user_id,can_read,can_import,"
                    "can_manage,can_remove,grant_source,created_at,updated_at) "
                    "VALUES(:library,:user,1,1,:manage,1,'MIGRATION_COMPATIBILITY',:now,:now)",
                ),
                {"library": library_id, "user": user_id, "manage": role == "OWNER", "now": now},
            )
        connection.execute(
            sa.text(
                "INSERT INTO library_entries(library_id,library_item_id,created_at) "
                "SELECT :library,id,:now FROM library_items WHERE workspace_id=:workspace",
            ),
            {"library": library_id, "workspace": workspace_id, "now": now},
        )


def downgrade() -> None:
    raise RuntimeError(
        "Library grants protect content; restore a coordinated snapshot, never downgrade."
    )
