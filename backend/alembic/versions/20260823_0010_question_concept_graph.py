"""Merge answers into questions and model typed concepts.

Revision ID: 20260823_0010
Revises: 20260823_0009
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260823_0010"
down_revision: str | None = "20260823_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    question_columns = {
        column["name"] for column in inspector.get_columns("paper_map_questions")
    }
    question_foreign_keys = {
        foreign_key.get("name")
        for foreign_key in inspector.get_foreign_keys("paper_map_questions")
    }
    with op.batch_alter_table("paper_map_questions") as batch_op:
        if "anchor_node_id" not in question_columns:
            batch_op.add_column(sa.Column("anchor_node_id", sa.String(36)))
        if "is_initial" not in question_columns:
            batch_op.add_column(
                sa.Column(
                    "is_initial",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )
        if "fk_paper_map_questions_anchor_node" not in question_foreign_keys:
            batch_op.create_foreign_key(
                "fk_paper_map_questions_anchor_node",
                "paper_map_nodes",
                ["anchor_node_id"],
                ["id"],
                ondelete="SET NULL",
            )
    inspector = sa.inspect(connection)
    question_indexes = {
        index["name"] for index in inspector.get_indexes("paper_map_questions")
    }
    if "ix_paper_map_questions_anchor" not in question_indexes:
        op.create_index(
            "ix_paper_map_questions_anchor",
            "paper_map_questions",
            ["anchor_node_id"],
        )
    node_columns = {column["name"] for column in inspector.get_columns("paper_map_nodes")}
    if "concept_type" not in node_columns:
        op.add_column("paper_map_nodes", sa.Column("concept_type", sa.String(24)))
    connection.execute(
        sa.text(
            "UPDATE paper_map_nodes "
            "SET concept_type = node_type, node_type = 'CONCEPT' "
            "WHERE node_type NOT IN ('PAPER', 'QUESTION', 'ANSWER')"
        )
    )
    answer_rows = connection.execute(
        sa.text(
            "SELECT id, question_id FROM paper_map_nodes "
            "WHERE node_type = 'ANSWER' AND question_id IS NOT NULL"
        )
    ).all()
    for answer_id, question_id in answer_rows:
        question_node_id = connection.execute(
            sa.text(
                "SELECT id FROM paper_map_nodes "
                "WHERE question_id = :question_id AND node_type = 'QUESTION' LIMIT 1"
            ),
            {"question_id": question_id},
        ).scalar()
        if question_node_id is None:
            continue
        connection.execute(
            sa.text(
                "UPDATE paper_map_nodes SET "
                "body = COALESCE((SELECT answer_text FROM paper_map_questions "
                "WHERE id = :qid), body), "
                "status = COALESCE((SELECT answer_status FROM paper_map_questions "
                "WHERE id = :qid), status), "
                "evidence_json = COALESCE((SELECT citations_json FROM paper_map_questions "
                "WHERE id = :qid), evidence_json) "
                "WHERE id = :node_id"
            ),
            {"qid": question_id, "node_id": question_node_id},
        )
        outgoing = connection.execute(
            sa.text(
                "SELECT id, target_node_id, edge_type FROM paper_map_edges "
                "WHERE source_node_id = :answer_id"
            ),
            {"answer_id": answer_id},
        ).all()
        for edge_id, target_node_id, edge_type in outgoing:
            next_type = "HAS_CONCEPT" if edge_type == "HAS_DETAIL" else edge_type
            duplicate = connection.execute(
                sa.text(
                    "SELECT id FROM paper_map_edges WHERE source_node_id = :source "
                    "AND target_node_id = :target AND edge_type = :edge_type LIMIT 1"
                ),
                {
                    "source": question_node_id,
                    "target": target_node_id,
                    "edge_type": next_type,
                },
            ).scalar()
            if duplicate is not None:
                connection.execute(
                    sa.text("DELETE FROM paper_map_edges WHERE id = :edge_id"),
                    {"edge_id": edge_id},
                )
            else:
                connection.execute(
                    sa.text(
                        "UPDATE paper_map_edges SET source_node_id = :source, "
                        "edge_type = :edge_type WHERE id = :edge_id"
                    ),
                    {"source": question_node_id, "edge_type": next_type, "edge_id": edge_id},
                )
        connection.execute(
            sa.text(
                "DELETE FROM paper_map_edges "
                "WHERE target_node_id = :answer_id OR source_node_id = :answer_id"
            ),
            {"answer_id": answer_id},
        )
        connection.execute(
            sa.text("DELETE FROM paper_map_nodes WHERE id = :answer_id"),
            {"answer_id": answer_id},
        )
    connection.execute(
        sa.text(
            "UPDATE paper_map_edges SET edge_type = 'HAS_CONCEPT' "
            "WHERE edge_type = 'HAS_DETAIL'"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE paper_map_nodes SET node_type = concept_type "
            "WHERE node_type = 'CONCEPT' AND concept_type IS NOT NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE paper_map_edges SET edge_type = 'HAS_DETAIL' "
            "WHERE edge_type = 'HAS_CONCEPT'"
        )
    )
    with op.batch_alter_table("paper_map_nodes") as batch_op:
        batch_op.drop_column("concept_type")
    op.drop_index("ix_paper_map_questions_anchor", table_name="paper_map_questions")
    with op.batch_alter_table("paper_map_questions") as batch_op:
        batch_op.drop_constraint(
            "fk_paper_map_questions_anchor_node",
            type_="foreignkey",
        )
        batch_op.drop_column("is_initial")
        batch_op.drop_column("anchor_node_id")
