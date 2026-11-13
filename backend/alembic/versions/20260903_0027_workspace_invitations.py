"""Add workspace-bound, single-use invitation records.

Revision ID: 20260903_0027
Revises: 20260903_0026
Create Date: 2026-09-03

Invitation secrets are deliberately absent from this schema.  Only a domain-separated digest
and a non-secret masked hint may be persisted.  Browser administrators and the Docker operator
remain distinguishable issuers so an operator action is never attributed to the installation
owner.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0027"
down_revision: str | None = "20260903_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("masked_hint", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36)),
        sa.Column("issuer_channel", sa.String(length=32), nullable=False),
        sa.Column("installation_state_version", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True)),
        sa.Column("redeemed_by_user_id", sa.String(length=36)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "issuer_channel IN ('WEB_ADMIN', 'OPERATOR_CLI')",
            name="workspace_invitations_valid_issuer_channel",
        ),
        sa.CheckConstraint(
            "(issuer_channel = 'WEB_ADMIN' AND created_by_user_id IS NOT NULL) OR "
            "(issuer_channel = 'OPERATOR_CLI' AND created_by_user_id IS NULL)",
            name="workspace_invitations_issuer_identity",
        ),
        sa.CheckConstraint(
            "installation_state_version >= 0",
            name="workspace_invitations_nonnegative_installation_version",
        ),
        sa.CheckConstraint(
            "length(trim(masked_hint)) BETWEEN 1 AND 32",
            name="workspace_invitations_nonempty_masked_hint",
        ),
        sa.CheckConstraint(
            "(redeemed_at IS NULL AND redeemed_by_user_id IS NULL) OR "
            "(redeemed_at IS NOT NULL AND redeemed_by_user_id IS NOT NULL)",
            name="workspace_invitations_complete_redemption",
        ),
        sa.CheckConstraint(
            "redeemed_at IS NULL OR revoked_at IS NULL",
            name="workspace_invitations_not_redeemed_and_revoked",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_workspace_invitations_token_digest",
        ),
    )
    op.create_index(
        "ix_workspace_invitations_workspace_created",
        "workspace_invitations",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_workspace_invitations_workspace_lifecycle",
        "workspace_invitations",
        ["workspace_id", "redeemed_at", "revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_workspace_invitations_created_by",
        "workspace_invitations",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_workspace_invitations_redeemed_by",
        "workspace_invitations",
        ["redeemed_by_user_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "Workspace invitations contain security and audit state; restore the coordinated "
        "pre-upgrade data-volume snapshot instead of downgrading destructively."
    )
