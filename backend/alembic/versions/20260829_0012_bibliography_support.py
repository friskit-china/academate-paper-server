"""Add structured bibliographies and citation occurrences.

Revision ID: 20260829_0012
Revises: 20260823_0011
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260829_0012"
down_revision: str | None = "20260823_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_bibliographies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_style", sa.Text()),
        sa.Column("resolved_style", sa.Text()),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("processor_version", sa.String(length=64), nullable=False),
        sa.Column("source_files_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paper_id"),
    )
    op.create_table(
        "bibliography_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bibliography_id", sa.String(length=36), nullable=False),
        sa.Column("citation_key", sa.Text(), nullable=False),
        sa.Column("citation_key_normalized", sa.Text(), nullable=False),
        sa.Column("entry_type", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text()),
        sa.Column("raw_bibtex", sa.Text()),
        sa.Column("fields_json", sa.JSON(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("container_title", sa.Text()),
        sa.Column("publication_year", sa.String(length=32)),
        sa.Column("doi", sa.Text()),
        sa.Column("url", sa.Text()),
        sa.Column("eprint", sa.Text()),
        sa.Column("archive_prefix", sa.String(length=64)),
        sa.Column("isbn", sa.Text()),
        sa.Column("issn", sa.Text()),
        sa.Column("ordinal", sa.Integer()),
        sa.Column("display_label", sa.Text()),
        sa.Column("citation_author", sa.Text()),
        sa.Column("citation_year", sa.String(length=32)),
        sa.Column("formatted_latex", sa.Text()),
        sa.Column("formatted_text", sa.Text()),
        sa.Column("render_payload_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["bibliography_id"], ["paper_bibliographies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bibliography_id", "citation_key_normalized"),
    )
    op.create_index(
        "ix_bibliography_entries_order",
        "bibliography_entries",
        ["bibliography_id", "ordinal"],
    )
    op.create_index("ix_bibliography_entries_doi", "bibliography_entries", ["doi"])
    op.create_index(
        "ix_bibliography_entries_eprint",
        "bibliography_entries",
        ["archive_prefix", "eprint"],
    )
    op.create_table(
        "bibliography_persons",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("given_names_json", sa.JSON(), nullable=False),
        sa.Column("family_names_json", sa.JSON(), nullable=False),
        sa.Column("prefix_names_json", sa.JSON(), nullable=False),
        sa.Column("suffix_names_json", sa.JSON(), nullable=False),
        sa.Column("initials_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["entry_id"], ["bibliography_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "role", "position"),
    )
    op.create_index(
        "ix_bibliography_persons_name", "bibliography_persons", ["display_name"]
    )
    op.create_table(
        "citation_occurrences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=36), nullable=False),
        sa.Column("bibliography_entry_id", sa.String(length=36)),
        sa.Column("cluster_path", sa.String(length=512), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("citation_key", sa.Text(), nullable=False),
        sa.Column("citation_key_normalized", sa.Text(), nullable=False),
        sa.Column("command", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.Text()),
        sa.Column("suffix", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["bibliography_entry_id"], ["bibliography_entries.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["block_id"], ["content_blocks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("block_id", "cluster_path", "item_index"),
    )
    op.create_index(
        "ix_citation_occurrences_paper",
        "citation_occurrences",
        ["paper_id", "block_id"],
    )
    op.create_index(
        "ix_citation_occurrences_entry",
        "citation_occurrences",
        ["bibliography_entry_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_citation_occurrences_entry", table_name="citation_occurrences")
    op.drop_index("ix_citation_occurrences_paper", table_name="citation_occurrences")
    op.drop_table("citation_occurrences")
    op.drop_index("ix_bibliography_persons_name", table_name="bibliography_persons")
    op.drop_table("bibliography_persons")
    op.drop_index("ix_bibliography_entries_eprint", table_name="bibliography_entries")
    op.drop_index("ix_bibliography_entries_doi", table_name="bibliography_entries")
    op.drop_index("ix_bibliography_entries_order", table_name="bibliography_entries")
    op.drop_table("bibliography_entries")
    op.drop_table("paper_bibliographies")
