"""Seed least-privilege MEMBER permissions and administrator user management.

Revision ID: 20260903_0026
Revises: 20260903_0025
Create Date: 2026-09-03

The first multi-user profile creates new accounts as MEMBERs of the administrator's active
workspace.  Their workspace capabilities must be an explicit catalog mapping rather than an
implicit fallback.  Account management remains a separate permission granted only through the
system administrators group; neither OWNER nor MEMBER workspace roles receive it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0026"
down_revision: str | None = "20260903_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_MANAGE_PERMISSION = "user.manage"
MEMBER_PERMISSION_KEYS = (
    "library.read",
    "library.write",
    "annotation.read",
    "annotation.write",
    "paper_map.read",
    "paper_map.write",
    "settings.read",
    "settings.write",
    "task.read",
    "task.write",
    "credential.read",
    "credential.write",
    "system_status.read",
)


def upgrade() -> None:
    connection = op.get_bind()
    permissions = sa.table(
        "permissions",
        sa.column("key", sa.String()),
        sa.column("description", sa.Text()),
    )
    groups = sa.table(
        "groups",
        sa.column("id", sa.String()),
        sa.column("key", sa.String()),
    )
    group_permissions = sa.table(
        "group_permissions",
        sa.column("group_id", sa.String()),
        sa.column("permission_key", sa.String()),
    )
    workspace_role_permissions = sa.table(
        "workspace_role_permissions",
        sa.column("role", sa.String()),
        sa.column("permission_key", sa.String()),
    )

    permission_exists = connection.execute(
        sa.select(permissions.c.key).where(permissions.c.key == USER_MANAGE_PERMISSION)
    ).scalar_one_or_none()
    if permission_exists is None:
        connection.execute(
            permissions.insert().values(
                key=USER_MANAGE_PERMISSION,
                description="Manage non-owner users in an authorized multi-user installation.",
            )
        )

    administrator_group_id = connection.execute(
        sa.select(groups.c.id).where(groups.c.key == "administrators")
    ).scalar_one_or_none()
    if administrator_group_id is None:
        raise RuntimeError(
            "The administrators group is missing; restore or repair the identity catalog."
        )
    administrator_mapping_exists = connection.execute(
        sa.select(group_permissions.c.group_id).where(
            group_permissions.c.group_id == administrator_group_id,
            group_permissions.c.permission_key == USER_MANAGE_PERMISSION,
        )
    ).scalar_one_or_none()
    if administrator_mapping_exists is None:
        connection.execute(
            group_permissions.insert().values(
                group_id=administrator_group_id,
                permission_key=USER_MANAGE_PERMISSION,
            )
        )

    existing_member_permissions = set(
        connection.execute(
            sa.select(workspace_role_permissions.c.permission_key).where(
                workspace_role_permissions.c.role == "MEMBER"
            )
        ).scalars()
    )
    missing_member_permissions = [
        {"role": "MEMBER", "permission_key": permission_key}
        for permission_key in MEMBER_PERMISSION_KEYS
        if permission_key not in existing_member_permissions
    ]
    if missing_member_permissions:
        connection.execute(
            workspace_role_permissions.insert(),
            missing_member_permissions,
        )


def downgrade() -> None:
    raise RuntimeError(
        "The multi-user permission boundary cannot be removed safely; restore the coordinated "
        "pre-upgrade data-volume snapshot."
    )
