"""Add versioned single-paper visual insight maps.

Revision ID: 20260823_0009
Revises: 20260822_0008
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260823_0009"
down_revision: str | None = "20260822_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_maps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "paper_id",
            sa.String(36),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("question_library_version", sa.String(64), nullable=False),
        sa.Column("primary_type", sa.String(32)),
        sa.Column("secondary_types_json", sa.JSON(), nullable=False),
        sa.Column("type_confidence", sa.Float()),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("plan_prompt_version", sa.String(64), nullable=False),
        sa.Column("plan_prompt_hash", sa.String(64), nullable=False),
        sa.Column("plan_context_json", sa.JSON()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("error_type", sa.String(64)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("paper_id", "generation"),
    )
    op.create_index("ix_paper_maps_current", "paper_maps", ["paper_id", "generation", "status"])
    op.create_table(
        "paper_map_questions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "map_id",
            sa.String(36),
            sa.ForeignKey("paper_maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question_key", sa.String(64), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_block_ids_json", sa.JSON(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("answer_generation", sa.Integer(), nullable=False),
        sa.Column("answer_status", sa.String(32)),
        sa.Column("answer_text", sa.Text()),
        sa.Column("citations_json", sa.JSON(), nullable=False),
        sa.Column("answer_prompt_version", sa.String(64)),
        sa.Column("answer_prompt_hash", sa.String(64)),
        sa.Column("answer_context_json", sa.JSON()),
        sa.Column("provider", sa.String(64)),
        sa.Column("model", sa.String(128)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("error_type", sa.String(64)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("map_id", "question_key"),
    )
    op.create_index(
        "ix_paper_map_questions_map_status",
        "paper_map_questions",
        ["map_id", "status", "ordinal"],
    )
    op.create_table(
        "paper_map_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "map_id",
            sa.String(36),
            sa.ForeignKey("paper_maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_id",
            sa.String(36),
            sa.ForeignKey("paper_map_questions.id", ondelete="CASCADE"),
        ),
        sa.Column("node_key", sa.String(128), nullable=False),
        sa.Column("node_type", sa.String(24), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32)),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("map_id", "node_key"),
    )
    op.create_index("ix_paper_map_nodes_map_type", "paper_map_nodes", ["map_id", "node_type"])
    op.create_index("ix_paper_map_nodes_question", "paper_map_nodes", ["question_id"])
    op.create_table(
        "paper_map_edges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "map_id",
            sa.String(36),
            sa.ForeignKey("paper_maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_node_id",
            sa.String(36),
            sa.ForeignKey("paper_map_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            sa.String(36),
            sa.ForeignKey("paper_map_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("edge_type", sa.String(32), nullable=False),
        sa.Column("is_tree", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("map_id", "source_node_id", "target_node_id", "edge_type"),
    )
    op.create_index("ix_paper_map_edges_map", "paper_map_edges", ["map_id"])


def downgrade() -> None:
    op.drop_index("ix_paper_map_edges_map", table_name="paper_map_edges")
    op.drop_table("paper_map_edges")
    op.drop_index("ix_paper_map_nodes_question", table_name="paper_map_nodes")
    op.drop_index("ix_paper_map_nodes_map_type", table_name="paper_map_nodes")
    op.drop_table("paper_map_nodes")
    op.drop_index("ix_paper_map_questions_map_status", table_name="paper_map_questions")
    op.drop_table("paper_map_questions")
    op.drop_index("ix_paper_maps_current", table_name="paper_maps")
    op.drop_table("paper_maps")
