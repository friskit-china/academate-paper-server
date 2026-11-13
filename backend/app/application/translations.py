from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker, with_loader_criteria

from app.application.render_repairs import inline_latex_render_payload
from app.application.settings import compose_translation_system_prompt
from app.domain.enums import ProcessingStage, TaskStatus, TranslationStatus
from app.domain.identity import stable_content_id
from app.domain.tasks import TaskScope, TaskVisibility
from app.domain.translation import (
    TRANSLATION_PROMPT_VERSION,
    TranslationRequest,
    TranslationUnit,
    context_hash,
    extract_translation_units,
    translation_input_hash,
)
from app.infrastructure.db.models import (
    ContentBlockModel,
    LibraryItemModel,
    PaperModel,
    ProcessingTaskModel,
    TranslationModel,
    UserModel,
    WorkspaceMembershipModel,
    now_utc,
)
from app.infrastructure.llm import DEFAULT_TRANSLATION_SYSTEM_PROMPT

TRANSLATE_BLOCK = "TRANSLATE_BLOCK"
TRANSLATION_PRIORITY = 50


def translation_task_scope_for_base_prompt(
    *,
    workspace_id: str,
    requested_by_user_id: str,
    base_system_prompt: str,
    credential_id: str | None = None,
) -> TaskScope:
    """Keep user-edited prompt text and its derived translation in user-private scope."""

    scope_factory = (
        TaskScope.workspace_derived
        if base_system_prompt.strip() == DEFAULT_TRANSLATION_SYSTEM_PROMPT.strip()
        else TaskScope.user_private
    )
    return scope_factory(
        workspace_id=workspace_id,
        requested_by_user_id=requested_by_user_id,
        credential_id=credential_id,
    )


def _scope_for_system_prompt(task_scope: TaskScope, system_prompt: str) -> TaskScope:
    _require_translation_scope(task_scope)
    default_prompts = {
        DEFAULT_TRANSLATION_SYSTEM_PROMPT.strip(),
        compose_translation_system_prompt(DEFAULT_TRANSLATION_SYSTEM_PROMPT).strip(),
    }
    if (
        task_scope.visibility is TaskVisibility.WORKSPACE_DERIVED
        and system_prompt.strip() not in default_prompts
    ):
        return TaskScope.user_private(
            workspace_id=str(task_scope.workspace_id),
            requested_by_user_id=str(task_scope.requested_by_user_id),
            credential_id=task_scope.credential_id,
        )
    return task_scope


def _require_translation_scope(task_scope: TaskScope) -> None:
    if (
        task_scope.visibility not in {TaskVisibility.WORKSPACE_DERIVED, TaskVisibility.USER_PRIVATE}
        or task_scope.workspace_id is None
        or task_scope.requested_by_user_id is None
    ):
        raise ValueError("Translations require an explicit workspace and requester scope.")


def _task_scope_fields(task_scope: TaskScope) -> dict[str, str | None]:
    _require_translation_scope(task_scope)
    return {
        "visibility": task_scope.visibility,
        "scope_key": task_scope.scope_key,
        "workspace_id": task_scope.workspace_id,
        "requested_by_user_id": task_scope.requested_by_user_id,
        "credential_id": task_scope.credential_id,
    }


def _translation_visibility_predicate(task_scope: TaskScope):
    _require_translation_scope(task_scope)
    return and_(
        TranslationModel.workspace_id == task_scope.workspace_id,
        or_(
            TranslationModel.prompt_kind == "global",
            and_(
                TranslationModel.prompt_kind.in_(["custom", "paper"]),
                TranslationModel.requested_by_user_id == task_scope.requested_by_user_id,
            ),
        ),
    )


def translation_scope_has_library_access(
    session: Session,
    *,
    paper_id: str,
    task_scope: TaskScope,
) -> bool:
    """Verify that a task requester is active and owns the paper through its workspace."""

    _require_translation_scope(task_scope)
    return (
        session.scalar(
            select(LibraryItemModel.id)
            .join(
                WorkspaceMembershipModel,
                WorkspaceMembershipModel.workspace_id == LibraryItemModel.workspace_id,
            )
            .join(UserModel, UserModel.id == WorkspaceMembershipModel.user_id)
            .where(
                LibraryItemModel.paper_id == paper_id,
                LibraryItemModel.workspace_id == task_scope.workspace_id,
                WorkspaceMembershipModel.user_id == task_scope.requested_by_user_id,
                UserModel.status == "ACTIVE",
            )
        )
        is not None
    )


def _translation_scope_has_active_membership(
    session: Session,
    *,
    task_scope: TaskScope,
) -> bool:
    _require_translation_scope(task_scope)
    return (
        session.scalar(
            select(WorkspaceMembershipModel.user_id)
            .join(UserModel, UserModel.id == WorkspaceMembershipModel.user_id)
            .where(
                WorkspaceMembershipModel.workspace_id == task_scope.workspace_id,
                WorkspaceMembershipModel.user_id == task_scope.requested_by_user_id,
                UserModel.status == "ACTIVE",
            )
        )
        is not None
    )


@dataclass(frozen=True, slots=True)
class TranslationSpec:
    block: ContentBlockModel
    units: tuple[TranslationUnit, ...]
    request: TranslationRequest
    context_hash: str
    input_hash: str
    prompt_version: str
    prompt_hash: str
    system_prompt: str


@dataclass(frozen=True, slots=True)
class PaperRetranslationResult:
    batch_id: str
    eligible: int
    queued: int
    superseded_tasks: int


@dataclass(frozen=True, slots=True)
class RemainingTranslationsResult:
    eligible: int
    queued: int
    created: int
    retried: int
    cached: int
    preserved: int
    in_flight: int


def _flat_source(units: tuple[TranslationUnit, ...]) -> str | None:
    value = " ".join(unit.text for unit in units).strip()
    return value[:2000] or None


def _section_title(block: ContentBlockModel, by_id: dict[str, ContentBlockModel]) -> str | None:
    if block.block_type == "ABSTRACT":
        return "Abstract"
    current: ContentBlockModel | None = block
    seen: set[str] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.block_type == "SECTION":
            candidate = current.render_payload_json.get("text") or current.source_text
            return str(candidate)[:500] if candidate else None
        current = by_id.get(current.parent_id) if current.parent_id else None
    return None


def build_translation_specs(
    *,
    paper: PaperModel,
    blocks: list[ContentBlockModel],
    language: str,
    provider: str,
    model: str,
    system_prompt: str = DEFAULT_TRANSLATION_SYSTEM_PROMPT,
    prompt_version: str = TRANSLATION_PROMPT_VERSION,
) -> list[TranslationSpec]:
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()
    by_id = {block.id: block for block in blocks}
    extracted = [
        extract_translation_units(
            block_type=block.block_type,
            source_text=block.source_text,
            render_payload=block.render_payload_json,
        )
        for block in blocks
    ]
    specs: list[TranslationSpec] = []
    for index, (block, units) in enumerate(zip(blocks, extracted, strict=True)):
        if not units:
            continue
        if block.block_type == "ABSTRACT":
            # Abstract translation can be scheduled as soon as metadata arrives, before body
            # parsing. Its cache/input identity must therefore not depend on mutable body blocks.
            previous_context = None
            next_context = None
        else:
            previous_context = next(
                (
                    _flat_source(extracted[position])
                    for position in range(index - 1, -1, -1)
                    if extracted[position]
                ),
                None,
            )
            next_context = next(
                (
                    _flat_source(extracted[position])
                    for position in range(index + 1, len(blocks))
                    if extracted[position]
                ),
                None,
            )
        section_title = _section_title(block, by_id)
        context_hash_value = context_hash(
            paper_title=paper.title,
            section_title=section_title,
            previous_context=previous_context,
            next_context=next_context,
        )
        input_hash = translation_input_hash(
            language=language,
            source_hash=block.content_hash,
            unit_hash=hashlib.sha256(
                json.dumps(
                    [{"key": unit.key, "text": unit.text, "math": unit.math} for unit in units],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            context_hash_value=context_hash_value,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            provider=provider,
            model=model,
        )
        specs.append(
            TranslationSpec(
                block=block,
                units=units,
                request=TranslationRequest(
                    target_language=language,
                    paper_title=paper.title,
                    section_title=section_title,
                    previous_context=previous_context,
                    next_context=next_context,
                    units=units,
                ),
                context_hash=context_hash_value,
                input_hash=input_hash,
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                system_prompt=system_prompt,
            )
        )
    return specs


def translation_spec_for_block(
    session: Session,
    *,
    block_id: str,
    language: str,
    provider: str,
    model: str,
    system_prompt: str = DEFAULT_TRANSLATION_SYSTEM_PROMPT,
    prompt_version: str = TRANSLATION_PROMPT_VERSION,
) -> TranslationSpec | None:
    block = session.get(ContentBlockModel, block_id)
    if block is None:
        return None
    paper = session.get(PaperModel, block.paper_id)
    if paper is None:
        return None
    blocks = list(
        session.scalars(
            select(ContentBlockModel)
            .where(ContentBlockModel.paper_id == paper.id)
            .order_by(ContentBlockModel.ordinal_path)
        ).all()
    )
    return next(
        (
            spec
            for spec in build_translation_specs(
                paper=paper,
                blocks=blocks,
                language=language,
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                prompt_version=prompt_version,
            )
            if spec.block.id == block_id
        ),
        None,
    )


def _create_scheduled_translation_for_spec(
    session: Session,
    *,
    paper_id: str,
    spec: TranslationSpec,
    language: str,
    provider: str,
    model: str,
    max_attempts: int,
    task_scope: TaskScope,
) -> str:
    _require_translation_scope(task_scope)
    prompt_kind = (
        "global"
        if task_scope.visibility is TaskVisibility.WORKSPACE_DERIVED
        else "custom"
    )
    ownership_predicates = [
        TranslationModel.workspace_id == task_scope.workspace_id,
        TranslationModel.prompt_kind == prompt_kind,
    ]
    if prompt_kind != "global":
        ownership_predicates.append(
            TranslationModel.requested_by_user_id == task_scope.requested_by_user_id
        )
    existing = session.scalar(
        select(TranslationModel).where(
            *ownership_predicates,
            TranslationModel.block_id == spec.block.id,
            TranslationModel.language == language,
            TranslationModel.input_hash == spec.input_hash,
        )
    )
    if existing is not None:
        return "existing"
    cached_source = session.scalar(
        select(TranslationModel)
        .where(
            *ownership_predicates,
            TranslationModel.input_hash == spec.input_hash,
            TranslationModel.status == TranslationStatus.SUCCEEDED,
        )
        .order_by(TranslationModel.completed_at)
        .limit(1)
    )
    translation = TranslationModel(
        workspace_id=task_scope.workspace_id,
        requested_by_user_id=task_scope.requested_by_user_id,
        block_id=spec.block.id,
        language=language,
        source_hash=spec.block.content_hash,
        context_hash=spec.context_hash,
        input_hash=spec.input_hash,
        provider=provider,
        model=model,
        prompt_version=TRANSLATION_PROMPT_VERSION,
        prompt_hash=spec.prompt_hash,
        system_prompt=spec.system_prompt,
        prompt_kind=prompt_kind,
    )
    session.add(translation)
    session.flush()
    if cached_source is not None:
        translation.status = TranslationStatus.SUCCEEDED
        translation.translated_text = cached_source.translated_text
        translation.translated_payload_json = cached_source.translated_payload_json
        translation.cache_hit = True
        translation.cache_source_translation_id = cached_source.id
        translation.input_tokens = 0
        translation.output_tokens = 0
        translation.latency_ms = 0
        translation.completed_at = now_utc()
        return "cached"
    session.add(
        ProcessingTaskModel(
            **_task_scope_fields(task_scope),
            paper_id=paper_id,
            task_type=TRANSLATE_BLOCK,
            priority=TRANSLATION_PRIORITY,
            idempotency_key=f"translation:{translation.id}:{spec.input_hash}",
            payload_json={
                "translation_id": translation.id,
                "block_id": spec.block.id,
                "language": language,
                "source_hash": spec.block.content_hash,
                "input_hash": spec.input_hash,
                "provider": provider,
                "model": model,
                "prompt_version": TRANSLATION_PROMPT_VERSION,
                "prompt_kind": prompt_kind,
            },
            max_attempts=max_attempts,
        )
    )
    return "created"


def schedule_paper_translations(
    session_factory: sessionmaker[Session],
    *,
    paper_id: str,
    language: str,
    provider: str,
    model: str,
    max_attempts: int,
    task_scope: TaskScope,
    system_prompt: str = DEFAULT_TRANSLATION_SYSTEM_PROMPT,
) -> dict[str, int]:
    _require_translation_scope(task_scope)
    task_scope = _scope_for_system_prompt(task_scope, system_prompt)
    created = 0
    cached = 0
    eligible = 0
    with session_factory() as session:
        paper = (
            session.get(PaperModel, paper_id)
            if translation_scope_has_library_access(
                session,
                paper_id=paper_id,
                task_scope=task_scope,
            )
            else None
        )
        if paper is None:
            return {"eligible": 0, "created": 0, "cached": 0}
        ensure_abstract_block(session, paper)
        session.flush()
        blocks = list(
            session.scalars(
                select(ContentBlockModel)
                .where(ContentBlockModel.paper_id == paper.id)
                .order_by(ContentBlockModel.ordinal_path)
            ).all()
        )
        specs = build_translation_specs(
            paper=paper,
            blocks=blocks,
            language=language,
            provider=provider,
            model=model,
            system_prompt=system_prompt,
        )
        eligible = len(specs)
        for spec in specs:
            current_query = select(TranslationModel.id).where(
                TranslationModel.workspace_id == task_scope.workspace_id,
                TranslationModel.block_id == spec.block.id,
                TranslationModel.language == language,
                TranslationModel.source_hash == spec.block.content_hash,
                TranslationModel.provider == provider,
                TranslationModel.model == model,
                or_(
                    TranslationModel.prompt_kind == "global",
                    and_(
                        TranslationModel.prompt_kind.in_(["custom", "paper"]),
                        TranslationModel.requested_by_user_id
                        == task_scope.requested_by_user_id,
                    ),
                ),
            )
            current = session.scalar(current_query.limit(1))
            if current is not None:
                # Prompt contract/settings changes apply to future source versions. Existing
                # blocks are only retranslated through the explicit per-block action.
                continue
            outcome = _create_scheduled_translation_for_spec(
                session,
                paper_id=paper.id,
                spec=spec,
                language=language,
                provider=provider,
                model=model,
                max_attempts=max_attempts,
                task_scope=task_scope,
            )
            if outcome == "cached":
                cached += 1
                continue
            if outcome == "created":
                created += 1
        session.commit()
    return {"eligible": eligible, "created": created, "cached": cached}


def _visible_translation(
    block: ContentBlockModel,
    *,
    language: str,
    provider: str,
    model: str,
    task_scope: TaskScope,
) -> TranslationModel | None:
    candidates = [
        item
        for item in block.translations
        if item.source_hash == block.content_hash
        and item.language == language
        and item.workspace_id == task_scope.workspace_id
        and (
            item.prompt_kind == "global"
            or (
                item.prompt_kind in {"custom", "paper"}
                and item.requested_by_user_id == task_scope.requested_by_user_id
            )
        )
    ]
    configured = [item for item in candidates if item.provider == provider and item.model == model]
    if configured:
        candidates = configured
    return max(candidates, key=lambda item: item.created_at, default=None)


def _reset_failed_translation(
    session: Session,
    *,
    paper_id: str,
    translation: TranslationModel,
    max_attempts: int,
    task_scope: TaskScope,
) -> ProcessingTaskModel:
    if translation.prompt_kind == "global":
        reset_scope = TaskScope.workspace_derived(
            workspace_id=str(task_scope.workspace_id),
            requested_by_user_id=str(task_scope.requested_by_user_id),
            credential_id=task_scope.credential_id,
        )
    elif translation.prompt_kind in {"custom", "paper"}:
        reset_scope = TaskScope.user_private(
            workspace_id=str(task_scope.workspace_id),
            requested_by_user_id=str(task_scope.requested_by_user_id),
            credential_id=task_scope.credential_id,
        )
    else:
        raise ValueError("Unknown translation prompt kind cannot be retried.")
    task = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.task_type == TRANSLATE_BLOCK,
            ProcessingTaskModel.scope_key == reset_scope.scope_key,
            ProcessingTaskModel.payload_json["translation_id"].as_string() == translation.id,
        )
    )
    if task is None:
        task = ProcessingTaskModel(
            **_task_scope_fields(reset_scope),
            paper_id=paper_id,
            task_type=TRANSLATE_BLOCK,
            priority=TRANSLATION_PRIORITY,
            idempotency_key=f"translation:{translation.id}:{translation.input_hash}",
            payload_json={
                "translation_id": translation.id,
                "block_id": translation.block_id,
                "language": translation.language,
                "source_hash": translation.source_hash,
                "input_hash": translation.input_hash,
                "provider": translation.provider,
                "model": translation.model,
                "prompt_version": translation.prompt_version,
                "prompt_kind": translation.prompt_kind,
            },
            max_attempts=max_attempts,
        )
        session.add(task)
    translation.status = TranslationStatus.PENDING
    translation.error_type = None
    translation.error_code = None
    translation.error_message = None
    task.visibility = reset_scope.visibility
    task.scope_key = reset_scope.scope_key
    task.workspace_id = reset_scope.workspace_id
    task.requested_by_user_id = reset_scope.requested_by_user_id
    task.credential_id = reset_scope.credential_id
    task.status = TaskStatus.QUEUED
    task.attempt_count = 0
    task.max_attempts = max_attempts
    task.available_at = now_utc()
    task.started_at = None
    task.finished_at = None
    task.lease_expires_at = None
    task.worker_id = None
    task.error_code = None
    task.error_message = None
    task.result_json = None
    return task


def schedule_remaining_paper_translations(
    session: Session,
    *,
    paper_id: str,
    language: str,
    provider: str,
    model: str,
    max_attempts: int,
    task_scope: TaskScope,
    system_prompt: str = DEFAULT_TRANSLATION_SYSTEM_PROMPT,
) -> RemainingTranslationsResult | None:
    """Queue only missing translations and retry the currently visible failures.

    Successful translations and already queued/running work are immutable in this operation.
    This makes repeated clicks idempotent while preserving explicit custom or paper-wide revisions.
    """

    _require_translation_scope(task_scope)
    task_scope = _scope_for_system_prompt(task_scope, system_prompt)
    paper = (
        session.get(PaperModel, paper_id)
        if translation_scope_has_library_access(
            session,
            paper_id=paper_id,
            task_scope=task_scope,
        )
        else None
    )
    if paper is None:
        return None
    ensure_abstract_block(session, paper)
    session.flush()
    blocks = list(
        session.scalars(
            select(ContentBlockModel)
            .options(
                selectinload(ContentBlockModel.translations),
                with_loader_criteria(
                    TranslationModel,
                    _translation_visibility_predicate(task_scope),
                    include_aliases=True,
                ),
            )
            .where(ContentBlockModel.paper_id == paper_id)
            .order_by(ContentBlockModel.ordinal_path)
        ).all()
    )
    specs = build_translation_specs(
        paper=paper,
        blocks=blocks,
        language=language,
        provider=provider,
        model=model,
        system_prompt=system_prompt,
    )
    created = 0
    retried = 0
    cached = 0
    preserved = 0
    in_flight = 0
    for spec in specs:
        current = _visible_translation(
            spec.block,
            language=language,
            provider=provider,
            model=model,
            task_scope=task_scope,
        )
        if current is not None and current.status == TranslationStatus.SUCCEEDED:
            preserved += 1
            continue
        if current is not None and current.status in {
            TranslationStatus.PENDING,
            TranslationStatus.RUNNING,
        }:
            in_flight += 1
            continue
        if current is not None and current.status == TranslationStatus.FAILED:
            _reset_failed_translation(
                session,
                paper_id=paper_id,
                translation=current,
                max_attempts=max_attempts,
                task_scope=task_scope,
            )
            retried += 1
            continue
        outcome = _create_scheduled_translation_for_spec(
            session,
            paper_id=paper_id,
            spec=spec,
            language=language,
            provider=provider,
            model=model,
            max_attempts=max_attempts,
            task_scope=task_scope,
        )
        if outcome == "created":
            created += 1
        elif outcome == "cached":
            cached += 1
        else:
            in_flight += 1
    return RemainingTranslationsResult(
        eligible=len(specs),
        queued=created + retried,
        created=created,
        retried=retried,
        cached=cached,
        preserved=preserved,
        in_flight=in_flight,
    )


def backfill_translation_tasks(
    session_factory: sessionmaker[Session],
    *,
    language: str,
    provider: str,
    model: str,
    max_attempts: int,
    task_scope: TaskScope,
    system_prompt: str = DEFAULT_TRANSLATION_SYSTEM_PROMPT,
) -> dict[str, int]:
    _require_translation_scope(task_scope)
    task_scope = _scope_for_system_prompt(task_scope, system_prompt)
    with session_factory() as session:
        if not _translation_scope_has_active_membership(
            session,
            task_scope=task_scope,
        ):
            return {"eligible": 0, "created": 0, "cached": 0}
        paper_ids = list(
            session.scalars(
                select(PaperModel.id)
                .join(LibraryItemModel, LibraryItemModel.paper_id == PaperModel.id)
                .where(
                    LibraryItemModel.workspace_id == task_scope.workspace_id,
                    PaperModel.processing_stage.in_(
                        [
                            ProcessingStage.READING_READY,
                            ProcessingStage.SEMANTIC_READY,
                            ProcessingStage.KNOWLEDGE_READY,
                        ]
                    ),
                )
            ).all()
        )
    totals = {"eligible": 0, "created": 0, "cached": 0}
    for paper_id in paper_ids:
        result = schedule_paper_translations(
            session_factory,
            paper_id=paper_id,
            language=language,
            provider=provider,
            model=model,
            max_attempts=max_attempts,
            task_scope=task_scope,
            system_prompt=system_prompt,
        )
        for key in totals:
            totals[key] += result[key]
    return totals


def abstract_block_id(paper_id: str) -> str:
    return stable_content_id("abstract", paper_id)


def ensure_abstract_block(
    session: Session,
    paper: PaperModel,
    *,
    renderer_preamble: str | None = None,
) -> ContentBlockModel | None:
    block_id = abstract_block_id(paper.id)
    existing = session.get(ContentBlockModel, block_id)
    abstract = (paper.abstract or "").strip()
    if not abstract:
        if existing is not None:
            session.delete(existing)
        return None
    source_hash = hashlib.sha256(abstract.encode()).hexdigest()
    canonical_render_payload = inline_latex_render_payload(
        abstract,
        renderer_preamble=renderer_preamble,
    )
    if existing is None:
        existing = ContentBlockModel(
            id=block_id,
            paper_id=paper.id,
            block_type="ABSTRACT",
            ordinal_path="!abstract",
            source_text=abstract,
            source_payload_json={"metadata_field": "abstract"},
            render_payload_json=canonical_render_payload,
            source_locator_json={"metadata_field": "abstract"},
            content_hash=source_hash,
        )
        session.add(existing)
    else:
        existing.source_text = abstract
        existing_macro_context = existing.render_payload_json.get("macro_context_hash")
        requested_macro_context = canonical_render_payload.get("macro_context_hash")
        if existing.content_hash != source_hash or (
            renderer_preamble is not None and existing_macro_context != requested_macro_context
        ):
            existing.render_payload_json = canonical_render_payload
        elif not isinstance(existing.render_payload_json.get("segments"), list):
            existing.render_payload_json = {
                **existing.render_payload_json,
                **canonical_render_payload,
            }
        existing.content_hash = source_hash
    return existing


def create_custom_retranslation(
    session: Session,
    *,
    paper_id: str,
    block_id: str,
    language: str,
    provider: str,
    model: str,
    max_attempts: int,
    system_prompt: str,
    task_scope: TaskScope,
) -> ProcessingTaskModel | None:
    _require_translation_scope(task_scope)
    if task_scope.visibility is not TaskVisibility.USER_PRIVATE:
        raise ValueError("Custom translation prompts require USER_PRIVATE task scope.")
    if not translation_scope_has_library_access(
        session,
        paper_id=paper_id,
        task_scope=task_scope,
    ):
        return None
    prompt_version = f"custom-{uuid.uuid4().hex}"
    spec = translation_spec_for_block(
        session,
        block_id=block_id,
        language=language,
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        prompt_version=prompt_version,
    )
    if spec is None or spec.block.paper_id != paper_id:
        return None
    translation = TranslationModel(
        workspace_id=task_scope.workspace_id,
        requested_by_user_id=task_scope.requested_by_user_id,
        block_id=block_id,
        language=language,
        source_hash=spec.block.content_hash,
        context_hash=spec.context_hash,
        input_hash=spec.input_hash,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        prompt_hash=spec.prompt_hash,
        system_prompt=system_prompt,
        prompt_kind="custom",
    )
    session.add(translation)
    session.flush()
    task = ProcessingTaskModel(
        **_task_scope_fields(task_scope),
        paper_id=paper_id,
        task_type=TRANSLATE_BLOCK,
        priority=TRANSLATION_PRIORITY - 5,
        idempotency_key=f"translation:{translation.id}:{spec.input_hash}",
        payload_json={
            "translation_id": translation.id,
            "block_id": block_id,
            "language": language,
            "source_hash": spec.block.content_hash,
            "input_hash": spec.input_hash,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "prompt_kind": "custom",
        },
        max_attempts=max_attempts,
    )
    session.add(task)
    return task


def create_paper_retranslation(
    session: Session,
    *,
    paper_id: str,
    language: str,
    provider: str,
    model: str,
    max_attempts: int,
    system_prompt: str,
    task_scope: TaskScope,
) -> PaperRetranslationResult | None:
    """Atomically supersede the visible translation set and queue a fresh paper-wide batch.

    Previous translation rows remain as revision history so translation-target annotations keep
    their exact revision anchor. Fresh pending rows immediately become the reader's current
    Chinese blocks, which clears the old derived text without deleting user-authored data.
    """
    _require_translation_scope(task_scope)
    if task_scope.visibility is not TaskVisibility.USER_PRIVATE:
        raise ValueError("Paper-wide prompt translation requires USER_PRIVATE task scope.")
    paper = (
        session.get(PaperModel, paper_id)
        if translation_scope_has_library_access(
            session,
            paper_id=paper_id,
            task_scope=task_scope,
        )
        else None
    )
    if paper is None:
        return None
    ensure_abstract_block(session, paper)
    session.flush()
    blocks = list(
        session.scalars(
            select(ContentBlockModel)
            .where(ContentBlockModel.paper_id == paper_id)
            .order_by(ContentBlockModel.ordinal_path)
        ).all()
    )
    batch_id = uuid.uuid4().hex
    prompt_version = f"paper-{batch_id}"
    specs = build_translation_specs(
        paper=paper,
        blocks=blocks,
        language=language,
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        prompt_version=prompt_version,
    )

    active_tasks = list(
        session.scalars(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.paper_id == paper_id,
                ProcessingTaskModel.task_type == TRANSLATE_BLOCK,
                ProcessingTaskModel.scope_key == task_scope.scope_key,
                ProcessingTaskModel.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            )
        ).all()
    )
    for active_task in active_tasks:
        active_task.status = TaskStatus.CANCELLED
        active_task.lease_expires_at = None
        active_task.worker_id = None
        active_task.finished_at = now_utc()
        active_task.result_json = {
            "superseded": True,
            "superseded_by_retranslation_batch_id": batch_id,
        }

    for spec in specs:
        translation = TranslationModel(
            workspace_id=task_scope.workspace_id,
            requested_by_user_id=task_scope.requested_by_user_id,
            block_id=spec.block.id,
            language=language,
            source_hash=spec.block.content_hash,
            context_hash=spec.context_hash,
            input_hash=spec.input_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            prompt_hash=spec.prompt_hash,
            system_prompt=system_prompt,
            prompt_kind="paper",
        )
        session.add(translation)
        session.flush()
        session.add(
            ProcessingTaskModel(
                **_task_scope_fields(task_scope),
                paper_id=paper_id,
                task_type=TRANSLATE_BLOCK,
                priority=TRANSLATION_PRIORITY - 5,
                idempotency_key=f"translation:{translation.id}:{spec.input_hash}",
                payload_json={
                    "translation_id": translation.id,
                    "block_id": spec.block.id,
                    "language": language,
                    "source_hash": spec.block.content_hash,
                    "input_hash": spec.input_hash,
                    "provider": provider,
                    "model": model,
                    "prompt_version": prompt_version,
                    "prompt_kind": "paper",
                    "retranslation_batch_id": batch_id,
                },
                max_attempts=max_attempts,
            )
        )

    return PaperRetranslationResult(
        batch_id=batch_id,
        eligible=len(specs),
        queued=len(specs),
        superseded_tasks=len(active_tasks),
    )


def retry_translation(
    session: Session,
    *,
    paper_id: str,
    block_id: str,
    workspace_id: str,
    requested_by_user_id: str,
    credential_id: str | None = None,
) -> ProcessingTaskModel | None:
    task_scope = TaskScope.workspace_derived(
        workspace_id=workspace_id,
        requested_by_user_id=requested_by_user_id,
    )
    if not translation_scope_has_library_access(
        session,
        paper_id=paper_id,
        task_scope=task_scope,
    ):
        return None
    translation = session.scalar(
        select(TranslationModel)
        .join(ContentBlockModel, ContentBlockModel.id == TranslationModel.block_id)
        .where(
            ContentBlockModel.paper_id == paper_id,
            TranslationModel.workspace_id == workspace_id,
            or_(
                TranslationModel.prompt_kind == "global",
                and_(
                    TranslationModel.prompt_kind.in_(["custom", "paper"]),
                    TranslationModel.requested_by_user_id == requested_by_user_id,
                ),
            ),
            TranslationModel.block_id == block_id,
            TranslationModel.status == TranslationStatus.FAILED,
        )
        .order_by(TranslationModel.updated_at.desc())
        .limit(1)
    )
    if translation is None:
        return None
    task = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.task_type == TRANSLATE_BLOCK,
            or_(
                ProcessingTaskModel.scope_key == f"workspace:{workspace_id}",
                ProcessingTaskModel.scope_key == f"user:{workspace_id}:{requested_by_user_id}",
            ),
            ProcessingTaskModel.payload_json["translation_id"].as_string() == translation.id,
        )
    )
    if task is None:
        return None
    retry_scope = (
        TaskScope.workspace_derived
        if translation.prompt_kind == "global"
        else TaskScope.user_private
    )(
        workspace_id=workspace_id,
        requested_by_user_id=requested_by_user_id,
        credential_id=credential_id,
    )
    translation.status = TranslationStatus.PENDING
    translation.error_type = None
    translation.error_code = None
    translation.error_message = None
    task.visibility = retry_scope.visibility
    task.scope_key = retry_scope.scope_key
    task.workspace_id = retry_scope.workspace_id
    task.requested_by_user_id = retry_scope.requested_by_user_id
    task.credential_id = retry_scope.credential_id
    task.status = TaskStatus.QUEUED
    task.attempt_count = 0
    task.available_at = now_utc()
    task.started_at = None
    task.finished_at = None
    task.lease_expires_at = None
    task.worker_id = None
    task.error_code = None
    task.error_message = None
    task.result_json = None
    return task
