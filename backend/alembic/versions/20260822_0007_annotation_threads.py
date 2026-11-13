"""Unify text annotations with comments and linked Q&A threads.

Revision ID: 20260822_0007
Revises: 20260821_0006
Create Date: 2026-08-22
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "20260822_0007"
down_revision: str | None = "20260821_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.add_column(sa.Column("anchor_annotation_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_annotations_anchor",
            "annotations",
            ["anchor_annotation_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index("ix_annotations_anchor", "annotations", ["anchor_annotation_id"])
    op.create_table(
        "annotation_comments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("annotation_id", sa.String(length=36), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["annotation_id"], ["annotations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_annotation_comments_active",
        "annotation_comments",
        ["annotation_id", "deleted_at", "created_at"],
    )
    connection = op.get_bind()
    legacy_notes = connection.execute(
        sa.text(
            "SELECT id, body, created_at, updated_at FROM annotations "
            "WHERE annotation_type = 'USER_NOTE' AND trim(body) <> ''"
        )
    ).mappings()
    for note in legacy_notes:
        connection.execute(
            sa.text(
                "INSERT INTO annotation_comments "
                "(id, annotation_id, body, revision, deleted_at, created_at, updated_at) "
                "VALUES (:id, :annotation_id, :body, 1, NULL, :created_at, :updated_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "annotation_id": note["id"],
                "body": note["body"],
                "created_at": note["created_at"] or datetime.now(UTC),
                "updated_at": note["updated_at"] or datetime.now(UTC),
            },
        )
    connection.execute(
        sa.text(
            "UPDATE annotations SET annotation_type = 'ANNOTATION', body = '' "
            "WHERE annotation_type IN ('HIGHLIGHT', 'USER_NOTE')"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    anchors = connection.execute(
        sa.text("SELECT id, is_highlighted FROM annotations WHERE annotation_type = 'ANNOTATION'")
    ).mappings()
    for anchor in anchors:
        body = connection.execute(
            sa.text(
                "SELECT body FROM annotation_comments "
                "WHERE annotation_id = :annotation_id AND deleted_at IS NULL "
                "ORDER BY created_at LIMIT 1"
            ),
            {"annotation_id": anchor["id"]},
        ).scalar_one_or_none()
        connection.execute(
            sa.text(
                "UPDATE annotations SET annotation_type = :annotation_type, body = :body "
                "WHERE id = :annotation_id"
            ),
            {
                "annotation_id": anchor["id"],
                "annotation_type": "USER_NOTE" if body else "HIGHLIGHT",
                "body": body or "",
            },
        )
    op.drop_index("ix_annotation_comments_active", table_name="annotation_comments")
    op.drop_table("annotation_comments")
    op.drop_index("ix_annotations_anchor", table_name="annotations")
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.drop_constraint("fk_annotations_anchor", type_="foreignkey")
        batch_op.drop_column("anchor_annotation_id")
