from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.db.models import (
    ApplicationSettingModel,
    InstallationStateModel,
    LibraryItemModel,
    UserModel,
    UserSettingModel,
    WorkspaceMembershipModel,
)
from app.infrastructure.llm import DEFAULT_TRANSLATION_SYSTEM_PROMPT

TRANSLATION_SYSTEM_PROMPT_KEY = "translation.system_prompt"
AUTOMATIC_AI_REPAIR_ENABLED_KEY = "render_repair.automatic_ai_enabled"
AUTOMATIC_TRANSLATION_ENABLED_KEY = "translation.automatic_enabled"
TRANSLATION_SYSTEM_PROMPT_MIN_LENGTH = 80
TRANSLATION_SYSTEM_PROMPT_MAX_LENGTH = 8000
ENTITY_ALIGNMENT_SYSTEM_PROMPT = (
    "Required entity alignment policy: For every entity, source_term must be the exact "
    "case-sensitive contiguous English phrase in source_text that directly and exclusively "
    "produced translated_term. Never infer, broaden, combine non-contiguous words, or select a "
    "nearby but different phrase. Return each unique translated_term/source_term mapping at most "
    "once per unit, even if the term occurs repeatedly. Omit the entity whenever the "
    "correspondence is uncertain."
)

_MISSING = object()


@dataclass(frozen=True, slots=True)
class UserWorkspacePreference:
    user_id: str
    workspace_id: str


def _require_user_id(user_id: str) -> str:
    normalized = user_id.strip()
    if not normalized:
        raise ValueError("A user-scoped setting requires a non-empty user id.")
    return normalized


def _legacy_owner_setting(
    session: Session,
    *,
    user_id: str,
    key: str,
    decode: Callable[[str], Any],
) -> Any:
    """Read the frozen pre-M5 table only for the installation owner.

    Migration 0017 copies every known preference into ``user_settings``. This narrow fallback is
    retained only for the rolling migration window where application code may start before that
    copy is visible. It is deliberately owner-bound: a second user must never inherit a legacy
    deployment-global preference merely because their scoped row is absent.
    """

    installation = session.get(InstallationStateModel, "installation")
    if installation is None or installation.owner_user_id != user_id:
        return _MISSING
    legacy = session.get(ApplicationSettingModel, key)
    if legacy is None:
        return _MISSING
    try:
        return decode(legacy.value_text)
    except (TypeError, ValueError):
        return _MISSING


def get_user_setting_value(
    session: Session,
    *,
    user_id: str,
    key: str,
    default: Any,
    legacy_decode: Callable[[str], Any] | None = None,
) -> Any:
    """Load one explicitly user-owned setting, with a read-only owner legacy fallback."""

    scoped_user_id = _require_user_id(user_id)
    setting = session.get(UserSettingModel, (scoped_user_id, key))
    if setting is not None:
        return setting.value_json
    if legacy_decode is not None:
        legacy_value = _legacy_owner_setting(
            session,
            user_id=scoped_user_id,
            key=key,
            decode=legacy_decode,
        )
        if legacy_value is not _MISSING:
            return legacy_value
    return default


def save_user_setting_value(
    session: Session,
    *,
    user_id: str,
    key: str,
    value: Any,
) -> None:
    """Upsert a scoped preference without ever mutating ``application_settings``."""

    scoped_user_id = _require_user_id(user_id)
    setting = session.get(UserSettingModel, (scoped_user_id, key))
    if setting is None:
        session.add(UserSettingModel(user_id=scoped_user_id, key=key, value_json=value))
    else:
        setting.value_json = value


def validate_translation_system_prompt(value: str) -> str:
    prompt = value.strip()
    if not (
        TRANSLATION_SYSTEM_PROMPT_MIN_LENGTH <= len(prompt) <= TRANSLATION_SYSTEM_PROMPT_MAX_LENGTH
    ):
        raise ValueError(
            "Translation system prompt must contain between "
            f"{TRANSLATION_SYSTEM_PROMPT_MIN_LENGTH} and "
            f"{TRANSLATION_SYSTEM_PROMPT_MAX_LENGTH} characters."
        )
    return prompt


def get_translation_system_prompt(session: Session, *, user_id: str) -> str:
    value = get_user_setting_value(
        session,
        user_id=user_id,
        key=TRANSLATION_SYSTEM_PROMPT_KEY,
        default=DEFAULT_TRANSLATION_SYSTEM_PROMPT,
        legacy_decode=str,
    )
    return value if isinstance(value, str) else DEFAULT_TRANSLATION_SYSTEM_PROMPT


def load_translation_system_prompt(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
) -> str:
    with session_factory() as session:
        return get_translation_system_prompt(session, user_id=user_id)


def compose_translation_system_prompt(base_prompt: str) -> str:
    return f"{base_prompt.rstrip()}\n\n{ENTITY_ALIGNMENT_SYSTEM_PROMPT}"


def get_effective_translation_system_prompt(session: Session, *, user_id: str) -> str:
    return compose_translation_system_prompt(
        get_translation_system_prompt(session, user_id=user_id)
    )


def load_effective_translation_system_prompt(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
) -> str:
    with session_factory() as session:
        return get_effective_translation_system_prompt(session, user_id=user_id)


def save_translation_system_prompt(session: Session, value: str, *, user_id: str) -> str:
    prompt = validate_translation_system_prompt(value)
    save_user_setting_value(
        session,
        user_id=user_id,
        key=TRANSLATION_SYSTEM_PROMPT_KEY,
        value=prompt,
    )
    return prompt


def reset_translation_system_prompt(session: Session, *, user_id: str) -> str:
    # Persist the default instead of deleting the scoped row. During the migration compatibility
    # window, deletion would otherwise reveal the frozen legacy value again.
    save_user_setting_value(
        session,
        user_id=user_id,
        key=TRANSLATION_SYSTEM_PROMPT_KEY,
        value=DEFAULT_TRANSLATION_SYSTEM_PROMPT,
    )
    return DEFAULT_TRANSLATION_SYSTEM_PROMPT


def _decode_legacy_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("Legacy boolean setting is invalid.")


def get_automatic_ai_repair_enabled(session: Session, *, user_id: str) -> bool:
    value = get_user_setting_value(
        session,
        user_id=user_id,
        key=AUTOMATIC_AI_REPAIR_ENABLED_KEY,
        default=False,
        legacy_decode=_decode_legacy_bool,
    )
    return value is True


def load_automatic_ai_repair_enabled(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
) -> bool:
    with session_factory() as session:
        return get_automatic_ai_repair_enabled(session, user_id=user_id)


def save_automatic_ai_repair_enabled(
    session: Session,
    enabled: bool,
    *,
    user_id: str,
) -> bool:
    save_user_setting_value(
        session,
        user_id=user_id,
        key=AUTOMATIC_AI_REPAIR_ENABLED_KEY,
        value=enabled,
    )
    return enabled


def get_automatic_translation_enabled(session: Session, *, user_id: str) -> bool:
    value = get_user_setting_value(
        session,
        user_id=user_id,
        key=AUTOMATIC_TRANSLATION_ENABLED_KEY,
        default=False,
        legacy_decode=_decode_legacy_bool,
    )
    return value is True


def load_automatic_translation_enabled(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
) -> bool:
    with session_factory() as session:
        return get_automatic_translation_enabled(session, user_id=user_id)


def save_automatic_translation_enabled(
    session: Session,
    enabled: bool,
    *,
    user_id: str,
) -> bool:
    save_user_setting_value(
        session,
        user_id=user_id,
        key=AUTOMATIC_TRANSLATION_ENABLED_KEY,
        value=enabled,
    )
    return enabled


def enabled_user_workspace_preferences(
    session: Session,
    *,
    key: str,
    paper_id: str | None = None,
) -> tuple[UserWorkspacePreference, ...]:
    """Resolve explicit opt-ins to active membership scopes.

    A canonical deterministic task has no requester by design. Its completion may be used only as
    an event: callers fan out through durable library membership and typed user preferences, then
    create one correctly scoped downstream LLM task per returned principal. Browser-provided user
    or workspace identifiers never participate in this lookup.
    """

    if key not in {
        AUTOMATIC_TRANSLATION_ENABLED_KEY,
        AUTOMATIC_AI_REPAIR_ENABLED_KEY,
    }:
        raise ValueError(f"Unsupported boolean user preference key: {key}")

    statement = (
        select(
            WorkspaceMembershipModel.user_id,
            WorkspaceMembershipModel.workspace_id,
            UserSettingModel.value_json,
        )
        .join(UserModel, UserModel.id == WorkspaceMembershipModel.user_id)
        .join(
            UserSettingModel,
            (UserSettingModel.user_id == WorkspaceMembershipModel.user_id)
            & (UserSettingModel.key == key),
        )
        .where(UserModel.status == "ACTIVE")
    )
    if paper_id is not None:
        statement = statement.join(
            LibraryItemModel,
            (LibraryItemModel.workspace_id == WorkspaceMembershipModel.workspace_id)
            & (LibraryItemModel.paper_id == paper_id),
        )
    rows = session.execute(
        statement.distinct().order_by(
            WorkspaceMembershipModel.workspace_id,
            WorkspaceMembershipModel.user_id,
        )
    ).all()
    return tuple(
        UserWorkspacePreference(user_id=user_id, workspace_id=workspace_id)
        for user_id, workspace_id, value in rows
        if value is True
    )
