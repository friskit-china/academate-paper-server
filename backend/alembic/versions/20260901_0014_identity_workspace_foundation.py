"""Add the M5 identity, workspace, permission, setting and credential foundation.

Revision ID: 20260901_0014
Revises: 20260830_0013
Create Date: 2026-09-01

This upgrade is deliberately data-driven and never reads an owner password or Edition from the
process environment.  It records the M5 Local installation invariant, seeds only stable system
catalog rows on an empty database, and creates a non-authenticatable pending owner only when the
pre-M5 database already contains user-scoped data.

Operators must retain the database/storage backup made before this migration.  Once setup claims
the pending owner, or M5 settings/credentials are written, the new ownership data cannot be mapped
back to the pre-M5 global model without loss.  Downgrade is therefore intentionally unsupported;
restore the coordinated pre-upgrade backup instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0014"
down_revision: str | None = "20260830_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INSTALLATION_KEY = "installation"
INSTALLATION_SCHEMA_VERSION = 1
PENDING_CLAIM_PASSWORD_SENTINEL = "!m5-pending-claim-not-authenticatable!"

PERMISSION_CATALOG: tuple[tuple[str, str], ...] = (
    ("library.read", "Read papers in a workspace library."),
    ("library.write", "Add, update, and remove workspace library items."),
    ("annotation.read", "Read workspace annotations and comments."),
    ("annotation.write", "Create and update workspace annotations and comments."),
    ("paper_map.read", "Read workspace paper maps."),
    ("paper_map.write", "Create and update workspace paper maps."),
    ("settings.read", "Read settings visible to the principal."),
    ("settings.write", "Update settings owned by the principal."),
    ("task.read", "Read tasks visible to the principal."),
    ("task.write", "Create and control tasks in the active workspace."),
    ("credential.read", "Read masked credential metadata owned by the principal."),
    ("credential.write", "Create, rotate, and remove credentials owned by the principal."),
    ("system_status.read", "Read the safe installation and runtime status summary."),
)

# Each of these tables represented implicit local-user state before M5.  Some rows imply a paper
# row through a foreign key, but keeping the list explicit protects partially recovered databases
# and makes the owner-creation decision independent of application settings or environment values.
LEGACY_USER_SCOPED_TABLES: tuple[str, ...] = (
    "papers",
    "translations",
    "annotations",
    "annotation_comments",
    "paper_maps",
    "block_bookmarks",
    "application_settings",
    "processing_tasks",
)


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _has_legacy_user_data(connection: sa.Connection) -> bool:
    for table_name in LEGACY_USER_SCOPED_TABLES:
        has_row = connection.execute(
            sa.select(sa.literal(1)).select_from(sa.table(table_name)).limit(1)
        ).scalar_one_or_none()
        if has_row is not None:
            return True
    return False


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_status", "users", ["status"])

    op.create_table(
        "groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "permissions",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "group_memberships",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "group_id"),
    )
    op.create_index("ix_group_memberships_group", "group_memberships", ["group_id"])

    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )
    op.create_index(
        "ix_workspace_memberships_user",
        "workspace_memberships",
        ["user_id", "workspace_id"],
    )

    op.create_table(
        "group_permissions",
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("permission_key", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_key"],
            ["permissions.key"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "permission_key"),
    )
    op.create_index(
        "ix_group_permissions_permission",
        "group_permissions",
        ["permission_key"],
    )

    op.create_table(
        "workspace_role_permissions",
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("permission_key", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_key"],
            ["permissions.key"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role", "permission_key"),
    )
    op.create_index(
        "ix_workspace_role_permissions_permission",
        "workspace_role_permissions",
        ["permission_key"],
    )

    op.create_table(
        "installation_state",
        sa.Column("singleton_key", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("edition", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "singleton_key = 'installation'",
            name="installation_state_singleton_key",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("singleton_key"),
    )

    op.create_table(
        "site_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "key"),
    )

    op.create_table(
        "user_llm_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("api_base", sa.Text()),
        sa.Column("model", sa.String(length=128)),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("encryption_algorithm", sa.String(length=64), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_llm_credentials_user_provider",
        "user_llm_credentials",
        ["user_id", "provider"],
    )

    connection = op.get_bind()
    now = _now()
    administrators_group_id = _new_id()
    groups = sa.table(
        "groups",
        sa.column("id"),
        sa.column("key"),
        sa.column("display_name"),
        sa.column("is_system"),
        sa.column("created_at"),
    )
    permissions = sa.table(
        "permissions",
        sa.column("key"),
        sa.column("description"),
    )
    group_permissions = sa.table(
        "group_permissions",
        sa.column("group_id"),
        sa.column("permission_key"),
    )
    workspace_role_permissions = sa.table(
        "workspace_role_permissions",
        sa.column("role"),
        sa.column("permission_key"),
    )
    connection.execute(
        groups.insert().values(
            id=administrators_group_id,
            key="administrators",
            display_name="Administrators",
            is_system=True,
            created_at=now,
        )
    )
    connection.execute(
        permissions.insert(),
        [
            {"key": permission_key, "description": description}
            for permission_key, description in PERMISSION_CATALOG
        ],
    )
    connection.execute(
        group_permissions.insert(),
        [
            {"group_id": administrators_group_id, "permission_key": permission_key}
            for permission_key, _ in PERMISSION_CATALOG
        ],
    )
    connection.execute(
        workspace_role_permissions.insert(),
        [
            {"role": "OWNER", "permission_key": permission_key}
            for permission_key, _ in PERMISSION_CATALOG
        ],
    )

    owner_user_id: str | None = None
    installation_state = "NEW"
    if _has_legacy_user_data(connection):
        owner_user_id = _new_id()
        workspace_id = _new_id()
        users = sa.table(
            "users",
            sa.column("id"),
            sa.column("username"),
            sa.column("display_name"),
            sa.column("password_hash"),
            sa.column("status"),
            sa.column("created_at"),
            sa.column("updated_at"),
        )
        workspaces = sa.table(
            "workspaces",
            sa.column("id"),
            sa.column("kind"),
            sa.column("name"),
            sa.column("created_at"),
            sa.column("updated_at"),
        )
        group_memberships = sa.table(
            "group_memberships",
            sa.column("user_id"),
            sa.column("group_id"),
            sa.column("created_at"),
        )
        workspace_memberships = sa.table(
            "workspace_memberships",
            sa.column("workspace_id"),
            sa.column("user_id"),
            sa.column("role"),
            sa.column("created_at"),
        )
        connection.execute(
            users.insert().values(
                id=owner_user_id,
                username="pending-local-owner",
                display_name="Local owner (pending claim)",
                password_hash=PENDING_CLAIM_PASSWORD_SENTINEL,
                status="PENDING_CLAIM",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            workspaces.insert().values(
                id=workspace_id,
                kind="PRIVATE",
                name="Local Library",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            group_memberships.insert().values(
                user_id=owner_user_id,
                group_id=administrators_group_id,
                created_at=now,
            )
        )
        connection.execute(
            workspace_memberships.insert().values(
                workspace_id=workspace_id,
                user_id=owner_user_id,
                role="OWNER",
                created_at=now,
            )
        )
        installation_state = "AWAITING_OWNER"

    installations = sa.table(
        "installation_state",
        sa.column("singleton_key"),
        sa.column("state"),
        sa.column("edition"),
        sa.column("schema_version"),
        sa.column("owner_user_id"),
        sa.column("completed_at"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    connection.execute(
        installations.insert().values(
            singleton_key=INSTALLATION_KEY,
            state=installation_state,
            edition="LOCAL",
            schema_version=INSTALLATION_SCHEMA_VERSION,
            owner_user_id=owner_user_id,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "M5 identity/workspace downgrade is unsupported; restore the coordinated pre-upgrade "
        "database, storage, and secret-scope backup."
    )
