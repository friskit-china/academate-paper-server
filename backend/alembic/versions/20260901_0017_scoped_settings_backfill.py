"""Backfill legacy global application settings into typed owner/site settings.

Revision ID: 20260901_0017
Revises: 20260901_0016
Create Date: 2026-09-01

Known user preferences move to the pending local owner's ``user_settings`` rows as typed JSON.
Unknown keys are preserved byte-for-byte as JSON strings in ``site_settings`` rather than guessed
into a user scope.  The legacy ``application_settings`` table remains unchanged as a read-only
compatibility source during M5.

New scoped settings may diverge from the compatibility table after upgrade, so downgrade cannot
choose a lossless winner.  Keep and restore the coordinated pre-upgrade backup; downgrade is
intentionally unsupported.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "20260901_0017"
down_revision: str | None = "20260901_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRANSLATION_SYSTEM_PROMPT_KEY = "translation.system_prompt"
AUTOMATIC_TRANSLATION_ENABLED_KEY = "translation.automatic_enabled"
AUTOMATIC_AI_REPAIR_ENABLED_KEY = "render_repair.automatic_ai_enabled"
PAPER_MAP_INITIAL_QUESTIONS_KEY = "paper_map.initial_questions.v1"
USER_SETTING_KEYS = frozenset(
    {
        TRANSLATION_SYSTEM_PROMPT_KEY,
        AUTOMATIC_TRANSLATION_ENABLED_KEY,
        AUTOMATIC_AI_REPAIR_ENABLED_KEY,
        PAPER_MAP_INITIAL_QUESTIONS_KEY,
    }
)


def _typed_user_value(key: str, value_text: str) -> Any:
    if key == TRANSLATION_SYSTEM_PROMPT_KEY:
        return value_text
    if key in {AUTOMATIC_TRANSLATION_ENABLED_KEY, AUTOMATIC_AI_REPAIR_ENABLED_KEY}:
        if value_text == "true":
            return True
        if value_text == "false":
            return False
        raise RuntimeError(
            f"Legacy boolean setting {key!r} is neither 'true' nor 'false'; restore the "
            "pre-upgrade backup and repair the setting before retrying."
        )
    if key == PAPER_MAP_INITIAL_QUESTIONS_KEY:
        try:
            return json.loads(value_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Legacy paper-map question settings are not valid JSON; restore the pre-upgrade "
                "backup and repair the setting before retrying."
            ) from exc
    raise AssertionError(f"Unhandled user setting key: {key}")


def upgrade() -> None:
    connection = op.get_bind()
    application_settings = sa.table(
        "application_settings",
        sa.column("key", sa.String(length=128)),
        sa.column("value_text", sa.Text()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    legacy_rows = connection.execute(
        sa.select(
            application_settings.c.key,
            application_settings.c.value_text,
            application_settings.c.updated_at,
        ).order_by(application_settings.c.key)
    ).mappings().all()
    if not legacy_rows:
        return

    owner_user_id = connection.execute(
        sa.text(
            "SELECT owner_user_id FROM installation_state "
            "WHERE singleton_key = 'installation'"
        )
    ).scalar_one_or_none()
    if owner_user_id is None:
        raise RuntimeError(
            "Legacy application settings exist without the pending local owner; restore the "
            "pre-upgrade backup and repair the M5 identity migration before retrying."
        )

    user_settings = sa.table(
        "user_settings",
        sa.column("user_id", sa.String(length=36)),
        sa.column("key", sa.String(length=128)),
        sa.column("value_json", sa.JSON()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    site_settings = sa.table(
        "site_settings",
        sa.column("key", sa.String(length=128)),
        sa.column("value_json", sa.JSON()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    user_values: list[dict[str, object]] = []
    site_values: list[dict[str, object]] = []
    for row in legacy_rows:
        key = row["key"]
        value_text = row["value_text"]
        if key in USER_SETTING_KEYS:
            user_values.append(
                {
                    "user_id": owner_user_id,
                    "key": key,
                    "value_json": _typed_user_value(key, value_text),
                    "updated_at": row["updated_at"],
                }
            )
        else:
            site_values.append(
                {
                    "key": key,
                    "value_json": value_text,
                    "updated_at": row["updated_at"],
                }
            )
    if user_values:
        connection.execute(user_settings.insert(), user_values)
    if site_values:
        connection.execute(site_settings.insert(), site_values)


def downgrade() -> None:
    raise RuntimeError(
        "M5 scoped-settings downgrade is unsupported; restore the coordinated pre-upgrade "
        "database, storage, and secret-scope backup."
    )
