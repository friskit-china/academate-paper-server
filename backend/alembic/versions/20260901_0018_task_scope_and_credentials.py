"""Add durable task visibility, ownership, credential reference and scoped idempotency.

Revision ID: 20260901_0018
Revises: 20260901_0017
Create Date: 2026-09-01

Legacy tasks are classified by an explicit conservative catalog.  Deterministic source/parse/
render work becomes deployment-global; default translations, maps and AI repair become workspace
derived; Q&A and custom/unknown work becomes user-private; known maintenance rows become system
tasks.  The migration refuses payload/result JSON, legacy task errors, or provider diagnostics
containing secret-shaped fields or obvious raw bearer/API-key values.  It never copies a
process-environment key into the database.

The old globally unique idempotency key is replaced by ``(scope_key, idempotency_key)``.  Collapsing
multiple tenant scopes back into a single global namespace is not lossless, so downgrade is
intentionally unsupported.  Restore the coordinated pre-upgrade backup instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0018"
down_revision: str | None = "20260901_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GLOBAL_DERIVED = "GLOBAL_DERIVED"
WORKSPACE_DERIVED = "WORKSPACE_DERIVED"
USER_PRIVATE = "USER_PRIVATE"
SYSTEM = "SYSTEM"

GLOBAL_TASK_TYPES = frozenset(
    {
        "FETCH_METADATA",
        "FETCH_SOURCE",
        "PARSE_SOURCE",
        "RENDER_TABLE",
        "RENDER_FIGURE",
        "RENDER_LATEX_BLOCK",
        "RENDER_LATEX_FORMULA",
    }
)
WORKSPACE_TASK_TYPES = frozenset(
    {
        "PLAN_PAPER_MAP",
        "ANSWER_MAP_QUESTION",
        "REPAIR_RENDER",
    }
)
USER_TASK_TYPES = frozenset({"ANSWER_QUESTION"})
SYSTEM_TASK_TYPES = frozenset({"SYSTEM_COORDINATION_LOCK"})

_SENSITIVE_KEY_NAMES = frozenset(
    {
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
        "servicetoken",
        "bootstraptoken",
    }
)
_BEARER_VALUE = re.compile(r"^bearer\s+\S+", re.IGNORECASE)
_API_KEY_VALUE = re.compile(r"^sk-[A-Za-z0-9_-]{8,}$")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|access[_-]?token|"
    r"refresh[_-]?token|service[_-]?token|bootstrap[_-]?token)\b"
    r"([\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s,;\"'}]+"
)
_BEARER_IN_TEXT = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_API_KEY_IN_TEXT = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _json_value(raw_value: object, *, task_id: str, column: str) -> Any:
    if raw_value is None or isinstance(raw_value, (dict, list, str, int, float, bool)):
        if isinstance(raw_value, str):
            try:
                return json.loads(raw_value)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Task {task_id} has invalid {column} JSON; restore the pre-upgrade backup "
                    "and repair the row before retrying."
                ) from exc
        return raw_value
    if isinstance(raw_value, bytes):
        try:
            return json.loads(raw_value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Task {task_id} has invalid {column} JSON; restore the pre-upgrade backup and "
                "repair the row before retrying."
            ) from exc
    raise RuntimeError(f"Task {task_id} has unsupported {column} JSON storage.")


def _normalized_key(key: object) -> str:
    return "".join(character for character in str(key).casefold() if character.isalnum())


def _secret_path(value: Any, path: str = "$") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if (
                normalized in _SENSITIVE_KEY_NAMES
                or normalized.endswith("apikey")
                or normalized.endswith("password")
                or normalized.endswith("secret")
                or (normalized.endswith("token") and not normalized.endswith("tokens"))
            ):
                return f"{path}.{key}"
            found = _secret_path(child, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            found = _secret_path(child, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if (
            _BEARER_VALUE.match(stripped)
            or _API_KEY_VALUE.match(stripped)
            or _SENSITIVE_ASSIGNMENT.search(value)
            or _BEARER_IN_TEXT.search(value)
            or _API_KEY_IN_TEXT.search(value)
        ):
            return path
    return None


def _validate_task_secrets(
    task_id: str,
    payload: Any,
    result: Any,
    error_code: Any,
    error_message: Any,
) -> None:
    for column, value in (
        ("payload_json", payload),
        ("result_json", result),
        ("error_code", error_code),
        ("error_message", error_message),
    ):
        secret_path = _secret_path(value)
        if secret_path is not None:
            raise RuntimeError(
                f"Task {task_id} contains a prohibited secret-shaped value in {column} at "
                f"{secret_path}; restore the pre-upgrade backup, remove the secret, rotate it, "
                "and retry."
            )


def _validate_provider_diagnostic_rows(
    table: str,
    rows: Sequence[Any],
    columns: Sequence[str],
) -> None:
    for row in rows:
        for column in columns:
            if _secret_path(row[column]) is not None:
                raise RuntimeError(
                    f"Legacy {table} row {row['id']} contains a prohibited secret-shaped "
                    f"provider diagnostic in {column}; restore the pre-upgrade backup, remove "
                    "the secret, rotate it, and retry."
                )


def _validate_legacy_provider_diagnostics(connection: sa.Connection) -> None:
    diagnostic_sets = (
        (
            "translations",
            connection.execute(
                sa.text(
                    "SELECT id, provider_request_id, error_type, error_code, error_message "
                    "FROM translations ORDER BY id"
                )
            ).mappings().all(),
            ("provider_request_id", "error_type", "error_code", "error_message"),
        ),
        (
            "annotations",
            connection.execute(
                sa.text(
                    "SELECT id, provider_request_id, qa_error_type, qa_error_code, "
                    "qa_error_message FROM annotations ORDER BY id"
                )
            ).mappings().all(),
            ("provider_request_id", "qa_error_type", "qa_error_code", "qa_error_message"),
        ),
        (
            "paper_maps",
            connection.execute(
                sa.text(
                    "SELECT id, provider_request_id, error_type, error_code, error_message "
                    "FROM paper_maps ORDER BY id"
                )
            ).mappings().all(),
            ("provider_request_id", "error_type", "error_code", "error_message"),
        ),
        (
            "paper_map_questions",
            connection.execute(
                sa.text(
                    "SELECT id, provider_request_id, error_type, error_code, error_message "
                    "FROM paper_map_questions ORDER BY id"
                )
            ).mappings().all(),
            ("provider_request_id", "error_type", "error_code", "error_message"),
        ),
    )
    for table, rows, columns in diagnostic_sets:
        _validate_provider_diagnostic_rows(table, rows, columns)


def _legacy_owner_scope(connection: sa.Connection) -> tuple[str, str] | None:
    rows = connection.execute(
        sa.text(
            "SELECT installation.owner_user_id, membership.workspace_id "
            "FROM installation_state AS installation "
            "JOIN workspace_memberships AS membership "
            "  ON membership.user_id = installation.owner_user_id "
            "WHERE installation.singleton_key = 'installation' "
            "  AND membership.role = 'OWNER' "
            "ORDER BY membership.workspace_id"
        )
    ).all()
    if len(rows) > 1:
        raise RuntimeError("Task scope backfill found multiple pending-owner workspaces.")
    return (rows[0][0], rows[0][1]) if rows else None


def _classify_task(
    task_type: str,
    payload: Any,
    translation_prompt_kinds: dict[str, str],
) -> str:
    if task_type in {"RENDER_LATEX_FORMULA", "REPAIR_RENDER"} and isinstance(
        payload, dict
    ):
        translation_id = payload.get("translation_id")
        translation_target = payload.get("formula_target") == "TRANSLATION"
        if translation_target or translation_id:
            if not isinstance(translation_id, str) or not translation_id:
                return USER_PRIVATE
            prompt_kind = translation_prompt_kinds.get(translation_id)
            return WORKSPACE_DERIVED if prompt_kind == "global" else USER_PRIVATE
    if task_type in GLOBAL_TASK_TYPES:
        return GLOBAL_DERIVED
    if task_type in WORKSPACE_TASK_TYPES:
        return WORKSPACE_DERIVED
    if task_type in USER_TASK_TYPES:
        return USER_PRIVATE
    if task_type in SYSTEM_TASK_TYPES or task_type.startswith(
        ("SYSTEM_", "MIGRATION_", "BACKFILL_", "MAINTENANCE_")
    ):
        return SYSTEM
    if task_type == "TRANSLATE_BLOCK":
        translation_id = payload.get("translation_id") if isinstance(payload, dict) else None
        if isinstance(translation_id, str):
            prompt_kind = translation_prompt_kinds.get(translation_id)
            if prompt_kind == "global":
                return WORKSPACE_DERIVED
        return USER_PRIVATE
    # Unknown legacy work must never become globally reusable by accident.
    return USER_PRIVATE


def _scope_values(
    visibility: str,
    owner_scope: tuple[str, str] | None,
) -> dict[str, str | None]:
    if visibility == GLOBAL_DERIVED:
        return {
            "workspace_id": None,
            "requested_by_user_id": None,
            "scope_key": "global",
        }
    if visibility == SYSTEM:
        return {
            "workspace_id": None,
            "requested_by_user_id": None,
            "scope_key": "system:installation",
        }
    if owner_scope is None:
        raise RuntimeError(
            "A workspace/user task exists without the pending local owner scope; restore the "
            "pre-upgrade backup and repair the identity migration before retrying."
        )
    owner_user_id, workspace_id = owner_scope
    scope_prefix = "workspace" if visibility == WORKSPACE_DERIVED else "user"
    scope_key = (
        f"workspace:{workspace_id}"
        if scope_prefix == "workspace"
        else f"user:{workspace_id}:{owner_user_id}"
    )
    return {
        "workspace_id": workspace_id,
        "requested_by_user_id": owner_user_id,
        "scope_key": scope_key,
    }


def upgrade() -> None:
    connection = op.get_bind()
    _validate_legacy_provider_diagnostics(connection)
    task_rows = connection.execute(
        sa.text(
            "SELECT id, task_type, payload_json, result_json, error_code, error_message "
            "FROM processing_tasks ORDER BY id"
        )
    ).mappings().all()
    decoded_rows: list[tuple[str, str, Any, Any]] = []
    for row in task_rows:
        payload = _json_value(row["payload_json"], task_id=row["id"], column="payload_json")
        result = _json_value(row["result_json"], task_id=row["id"], column="result_json")
        _validate_task_secrets(
            row["id"],
            payload,
            result,
            row["error_code"],
            row["error_message"],
        )
        decoded_rows.append((row["id"], row["task_type"], payload, result))

    translation_prompt_kinds = dict(
        connection.execute(sa.text("SELECT id, prompt_kind FROM translations")).all()
    )
    owner_scope = _legacy_owner_scope(connection)

    with op.batch_alter_table("processing_tasks") as batch_op:
        batch_op.add_column(sa.Column("visibility", sa.String(length=32)))
        batch_op.add_column(sa.Column("scope_key", sa.String(length=255)))
        batch_op.add_column(sa.Column("workspace_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("requested_by_user_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("credential_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_processing_tasks_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_processing_tasks_requested_by_user_id_users",
            "users",
            ["requested_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_processing_tasks_credential_id_user_llm_credentials",
            "user_llm_credentials",
            ["credential_id"],
            ["id"],
            ondelete="SET NULL",
        )

    for task_id, task_type, payload, _ in decoded_rows:
        visibility = _classify_task(task_type, payload, translation_prompt_kinds)
        values = _scope_values(visibility, owner_scope)
        connection.execute(
            sa.text(
                "UPDATE processing_tasks SET visibility = :visibility, scope_key = :scope_key, "
                "workspace_id = :workspace_id, requested_by_user_id = :requested_by_user_id "
                "WHERE id = :task_id"
            ),
            {"task_id": task_id, "visibility": visibility, **values},
        )

    with op.batch_alter_table("processing_tasks") as batch_op:
        batch_op.drop_constraint("uq_processing_tasks_idempotency_key", type_="unique")
        batch_op.alter_column(
            "visibility",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=SYSTEM,
        )
        batch_op.alter_column(
            "scope_key",
            existing_type=sa.String(length=255),
            nullable=False,
            server_default="system:compatibility",
        )
        batch_op.create_unique_constraint(
            "uq_processing_tasks_scope_idempotency",
            ["scope_key", "idempotency_key"],
        )
        batch_op.create_check_constraint(
            "processing_tasks_visibility_scope",
            "length(trim(scope_key)) > 0 AND ("
            "(visibility = 'GLOBAL_DERIVED' AND scope_key = 'global' "
            " AND workspace_id IS NULL AND requested_by_user_id IS NULL "
            " AND credential_id IS NULL) OR "
            "(visibility = 'WORKSPACE_DERIVED' "
            " AND scope_key = 'workspace:' || workspace_id "
            " AND workspace_id IS NOT NULL AND requested_by_user_id IS NOT NULL) OR "
            "(visibility = 'USER_PRIVATE' "
            " AND scope_key = 'user:' || workspace_id || ':' || requested_by_user_id "
            " AND workspace_id IS NOT NULL AND requested_by_user_id IS NOT NULL) OR "
            "(visibility = 'SYSTEM' AND scope_key LIKE 'system:%' "
            " AND workspace_id IS NULL AND requested_by_user_id IS NULL "
            " AND credential_id IS NULL))",
        )
    op.create_index(
        "ix_processing_tasks_visibility_scope",
        "processing_tasks",
        ["visibility", "scope_key", "status"],
    )
    op.create_index(
        "ix_processing_tasks_workspace",
        "processing_tasks",
        ["workspace_id", "status", "created_at"],
    )
    op.create_index(
        "ix_processing_tasks_requester",
        "processing_tasks",
        ["requested_by_user_id", "workspace_id"],
    )
    op.create_index(
        "ix_processing_tasks_credential",
        "processing_tasks",
        ["credential_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "M5 scoped-task downgrade is unsupported; restore the coordinated pre-upgrade database, "
        "storage, and secret-scope backup."
    )
