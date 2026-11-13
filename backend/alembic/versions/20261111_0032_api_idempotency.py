"""Persist bounded, subject-scoped v2 command idempotency receipts."""

import sqlalchemy as sa

from alembic import op

revision = "20261111_0032"
down_revision = "20261111_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_idempotency",
        sa.Column("key_digest", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("authorization_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("response_headers_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING','COMPLETE','NON_REPLAYABLE')", name="api_receipt_state"
        ),
        sa.CheckConstraint(
            "(state = 'COMPLETE' AND response_json IS NOT NULL AND status_code >= 200 "
            "AND status_code < 300) OR (state <> 'COMPLETE' AND response_json IS NULL "
            "AND status_code IS NULL)",
            name="api_receipt_complete_response",
        ),
    )
    op.create_index("ix_api_receipts_expiry", "api_idempotency", ["expires_at"])
    op.create_index(
        "ix_api_receipts_subject", "api_idempotency", ["user_id", "workspace_id", "created_at"]
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a complete stopped-writer snapshot; destructive downgrade is disabled."
    )
