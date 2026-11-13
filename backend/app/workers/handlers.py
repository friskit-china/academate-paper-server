from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.credentials import LlmTaskCredential
from app.application.paper_maps import ANSWER_MAP_QUESTION, PLAN_PAPER_MAP
from app.application.qa import (
    ANSWER_QUESTION,
    QA_PROMPT_VERSION,
    QA_SYSTEM_PROMPT,
    build_qa_request,
    qa_prompt_hash,
)
from app.application.render_overlays import (
    effective_block_render,
    save_workspace_render_overlay,
    workspace_render_asset_key,
)
from app.application.render_repairs import (
    FormulaRepairValidationError,
    FormulaSelectorError,
    apply_formula_latex_render,
    apply_formula_repair,
    equation_matches_deterministic_normalization,
    formula_matches_deterministic_normalization,
    formula_selector_key,
    normalize_katex_latex,
    propagate_formula_latex_render,
    selected_formula,
    selected_formula_node,
    validate_katex_repair,
)
from app.application.settings import (
    AUTOMATIC_AI_REPAIR_ENABLED_KEY,
    UserWorkspacePreference,
    enabled_user_workspace_preferences,
)
from app.application.translations import (
    TRANSLATE_BLOCK,
    ensure_abstract_block,
    translation_scope_has_library_access,
    translation_spec_for_block,
)
from app.domain.arxiv import ArxivIdentifier
from app.domain.citations import resolve_canonical_quote
from app.domain.enums import (
    AnnotationTarget,
    AnnotationType,
    ProcessingHealth,
    ProcessingStage,
    QAStatus,
    TaskStatus,
    TranslationStatus,
)
from app.domain.identity import stable_content_id
from app.domain.os_adapters import ToolchainCommands, toolchain_cache_identity
from app.domain.paper_map import PaperMapGenerator
from app.domain.ports import QueuedTask, Storage
from app.domain.qa import QACitation, QuestionAnswerer, qa_user_payload
from app.domain.tasks import TaskScope, TaskVisibility, redact_task_error_message
from app.domain.translation import Translator, translated_payload
from app.infrastructure.arxiv import (
    ArchiveIntegrityError,
    ArchiveSafetyError,
    ArxivClient,
    ArxivClientError,
    ArxivResumeError,
    SourceDownloadProgress,
    extract_source_archive,
    load_validated_source_manifest,
)
from app.infrastructure.db.models import (
    AnnotationModel,
    BibliographyEntryModel,
    BibliographyPersonModel,
    CitationOccurrenceModel,
    ContentBlockModel,
    LibraryItemModel,
    PaperAuthorModel,
    PaperBibliographyModel,
    PaperModel,
    PaperSourceModel,
    ProcessingTaskModel,
    TranslationModel,
    UserModel,
    WorkspaceMembershipModel,
    now_utc,
)
from app.infrastructure.latex import (
    ARXIV_PDF_ASSET_PATH,
    FIGURE_RENDER_MAX_ASSET_BYTES,
    FIGURE_RENDER_MAX_ASSET_COUNT,
    FIGURE_RENDERER_VERSION,
    INCOMPATIBLE_TABLE_RENDERER_VERSIONS,
    KATEX_NORMALIZER_VERSION,
    LATEX_BLOCK_RENDERER_VERSION,
    PARSER_VERSION,
    PDF_ONLY_READER_VERSION,
    TABLE_RENDER_MAX_ASSET_BYTES,
    TABLE_RENDER_MAX_ASSET_COUNT,
    TABLE_RENDERER_VERSION,
    BibliographyProcessingError,
    BibliographyResult,
    FigureRenderError,
    LatexFigureRenderer,
    LatexTableRenderer,
    PDFDocumentError,
    TableRenderError,
    figure_requires_latex_render,
    inspect_pdf_document,
    materialize_citation_segment,
    normalize_figure_latex_for_render,
    normalize_render_payload_with_preamble,
    normalize_table_latex_for_render,
    parse_latex_project,
    prepare_includegraphics_assets,
    process_bibliography,
    render_latex_prose,
    table_complexity_reasons,
    table_png_has_visible_geometry,
    table_svg_has_visible_geometry,
)
from app.infrastructure.llm import (
    DEFAULT_TRANSLATION_SYSTEM_PROMPT,
    RENDER_REPAIR_MAX_LATEX_CHARS,
    RenderRepairClient,
    TranslationProviderError,
)
from app.infrastructure.llm.qa import QAProviderError
from app.workers.paper_maps import answer_map_question, plan_paper_map

FETCH_METADATA = "FETCH_METADATA"
FETCH_SOURCE = "FETCH_SOURCE"
PARSE_SOURCE = "PARSE_SOURCE"
RENDER_FIGURE = "RENDER_FIGURE"
RENDER_TABLE = "RENDER_TABLE"
RENDER_LATEX_BLOCK = "RENDER_LATEX_BLOCK"
RENDER_LATEX_FORMULA = "RENDER_LATEX_FORMULA"
REPAIR_RENDER = "REPAIR_RENDER"
LATEX_FORMULA_RENDERER_VERSION = "texlive-formula-svg-v3"
_GLOBAL_TASK_SCOPE = TaskScope.global_derived()
_PARSER_PREAMBLE_FILE_PATTERN = re.compile(r"preamble-[0-9a-f]{64}\.tex")
_TASK_VISIBILITY_MATRIX: dict[str, frozenset[TaskVisibility]] = {
    FETCH_METADATA: frozenset({TaskVisibility.GLOBAL_DERIVED}),
    FETCH_SOURCE: frozenset({TaskVisibility.GLOBAL_DERIVED}),
    PARSE_SOURCE: frozenset({TaskVisibility.GLOBAL_DERIVED}),
    RENDER_LATEX_BLOCK: frozenset({TaskVisibility.GLOBAL_DERIVED}),
    RENDER_TABLE: frozenset(
        {TaskVisibility.GLOBAL_DERIVED, TaskVisibility.WORKSPACE_DERIVED}
    ),
    RENDER_FIGURE: frozenset(
        {TaskVisibility.GLOBAL_DERIVED, TaskVisibility.WORKSPACE_DERIVED}
    ),
    RENDER_LATEX_FORMULA: frozenset(
        {
            TaskVisibility.GLOBAL_DERIVED,
            TaskVisibility.WORKSPACE_DERIVED,
            TaskVisibility.USER_PRIVATE,
        }
    ),
    REPAIR_RENDER: frozenset(
        {TaskVisibility.WORKSPACE_DERIVED, TaskVisibility.USER_PRIVATE}
    ),
    TRANSLATE_BLOCK: frozenset(
        {TaskVisibility.WORKSPACE_DERIVED, TaskVisibility.USER_PRIVATE}
    ),
    ANSWER_QUESTION: frozenset({TaskVisibility.USER_PRIVATE}),
    PLAN_PAPER_MAP: frozenset({TaskVisibility.WORKSPACE_DERIVED}),
    ANSWER_MAP_QUESTION: frozenset({TaskVisibility.WORKSPACE_DERIVED}),
}


class TaskHandlerError(RuntimeError):
    pass


class TaskAuthorizationError(TaskHandlerError):
    """Raised before a queued task can cross its persisted ownership boundary."""


def _toolchain_provenance(fingerprint: str | None) -> dict[str, str]:
    return {"toolchain_fingerprint": fingerprint} if fingerprint is not None else {}


def _toolchain_identity_matches(
    payload: Mapping[str, Any],
    *,
    renderer_version: str,
    fingerprint: str | None,
) -> bool:
    return (
        payload.get("renderer_version") == renderer_version
        and payload.get("toolchain_fingerprint") == fingerprint
    )


def _require_toolchain_task_identity(
    task: QueuedTask,
    *,
    renderer_version: str,
    fingerprint: str | None,
) -> None:
    # Compose remains the legacy regression authority and does not publish an external runtime
    # fingerprint.  Its historical queue rows may predate renderer_version entirely.  Native
    # external workers always carry a non-null launch fingerprint and therefore fail closed.
    if fingerprint is None:
        return
    if not _toolchain_identity_matches(
        task.payload,
        renderer_version=renderer_version,
        fingerprint=fingerprint,
    ):
        raise TaskHandlerError("The render task toolchain identity is no longer current.")


def authorize_task_scope(task: QueuedTask) -> TaskScope:
    """Validate the persisted task type, visibility, scope key, and payload discriminator."""

    allowed_visibilities = _TASK_VISIBILITY_MATRIX.get(task.task_type)
    if allowed_visibilities is None:
        raise TaskHandlerError(f"Unsupported task type: {task.task_type}")
    try:
        visibility = TaskVisibility(task.visibility)
        scope = TaskScope(
            visibility,
            workspace_id=task.workspace_id,
            requested_by_user_id=task.requested_by_user_id,
            credential_id=task.credential_id,
        )
    except (TypeError, ValueError) as error:
        raise TaskAuthorizationError("Task ownership fields are invalid.") from error
    if task.scope_key != scope.scope_key or visibility not in allowed_visibilities:
        raise TaskAuthorizationError(
            "Task type, visibility, and ownership scope are inconsistent."
        )

    if task.task_type in {RENDER_TABLE, RENDER_FIGURE}:
        repair_task_id = task.payload.get("repair_task_id")
        has_repair_owner = isinstance(repair_task_id, str) and bool(repair_task_id.strip())
        if visibility is TaskVisibility.GLOBAL_DERIVED:
            if repair_task_id not in (None, ""):
                raise TaskAuthorizationError(
                    "Canonical render tasks cannot claim a workspace repair owner."
                )
        elif not has_repair_owner:
            raise TaskAuthorizationError(
                "Workspace render tasks require an explicit repair task owner."
            )

    if task.task_type == RENDER_LATEX_FORMULA:
        target_kind = str(task.payload.get("formula_target", "BLOCK"))
        translation_id = task.payload.get("translation_id")
        if target_kind == "BLOCK":
            if visibility not in {
                TaskVisibility.GLOBAL_DERIVED,
                TaskVisibility.WORKSPACE_DERIVED,
            }:
                raise TaskAuthorizationError(
                    "Block formula tasks require global or workspace-derived visibility."
                )
            if translation_id not in (None, ""):
                raise TaskAuthorizationError(
                    "Block formula tasks cannot reference a translation revision."
                )
        elif target_kind == "TRANSLATION":
            if visibility not in {
                TaskVisibility.WORKSPACE_DERIVED,
                TaskVisibility.USER_PRIVATE,
            }:
                raise TaskAuthorizationError(
                    "Translated formula tasks require workspace or user-private visibility."
                )
            if not isinstance(translation_id, str) or not translation_id:
                raise TaskAuthorizationError(
                    "Translated formula tasks require an explicit translation revision."
                )
        else:
            raise TaskAuthorizationError("Formula task target kind is unsupported.")
    return scope


def _load_task_source_manifest(
    storage: Storage,
    *,
    paper_id: str,
    manifest_key: str,
    required: bool = True,
) -> dict[str, Any] | None:
    try:
        return load_validated_source_manifest(
            storage,
            paper_id=paper_id,
            manifest_key=manifest_key,
            required=required,
        )
    except (ArchiveIntegrityError, ArchiveSafetyError) as error:
        raise TaskHandlerError(
            "The paper source manifest is invalid or outside its storage scope."
        ) from error


def _validated_task_preamble_key(*, paper_id: str, preamble_key: str) -> str | None:
    """Bind an optional renderer preamble to the parser artifact for this exact paper."""

    if not preamble_key:
        return None
    path = PurePosixPath(preamble_key)
    if (
        path.as_posix() != preamble_key
        or len(path.parts) != 5
        or path.parts[:4] != ("papers", paper_id, "derived", "parser")
        or _PARSER_PREAMBLE_FILE_PATTERN.fullmatch(path.name) is None
    ):
        raise TaskHandlerError(
            "The renderer preamble is invalid or outside the paper storage scope."
        )
    return preamble_key


def _inspect_pdf_with_toolchain(stream, *, pdfinfo_command: str):
    # Preserve the legacy/default call shape for focused fakes while the worker composition root
    # always supplies the resolver's absolute path.
    if pdfinfo_command == "pdfinfo":
        return inspect_pdf_document(stream)
    return inspect_pdf_document(stream, pdfinfo_command=pdfinfo_command)


def _identifier(task: QueuedTask) -> ArxivIdentifier:
    return ArxivIdentifier(
        arxiv_id=str(task.payload["arxiv_id"]),
        version=str(task.payload.get("arxiv_version", "")),
    )


def _paper(session: Session, task: QueuedTask) -> PaperModel:
    paper = session.get(PaperModel, task.paper_id)
    if paper is None:
        raise TaskHandlerError("The paper record no longer exists.")
    return paper


def fetch_metadata(
    task: QueuedTask,
    client: ArxivClient,
    session_factory: sessionmaker[Session],
) -> dict[str, Any]:
    metadata = client.fetch_metadata(_identifier(task))
    with session_factory() as session:
        paper = _paper(session, task)
        paper.title = metadata.title
        paper.abstract = metadata.abstract
        paper.submitted_at = metadata.submitted_at
        session.execute(delete(PaperAuthorModel).where(PaperAuthorModel.paper_id == paper.id))
        session.add_all(
            PaperAuthorModel(paper_id=paper.id, position=index, name=name)
            for index, name in enumerate(metadata.authors)
        )
        session.commit()
    return {"authors": len(metadata.authors), "canonical_url": metadata.canonical_url}


def _upsert_source(
    session: Session,
    *,
    paper_id: str,
    source_type: str,
    storage_key: str,
    sha256: str,
    byte_size: int,
    mime_type: str,
) -> PaperSourceModel:
    source = session.scalar(
        select(PaperSourceModel).where(
            PaperSourceModel.paper_id == paper_id,
            PaperSourceModel.source_type == source_type,
        )
    )
    if source is None:
        source = PaperSourceModel(paper_id=paper_id, source_type=source_type)
        session.add(source)
    source.storage_key = storage_key
    source.sha256 = sha256
    source.byte_size = byte_size
    source.mime_type = mime_type
    source.retrieved_at = now_utc()
    return source


def _ensure_parse_task(
    session: Session,
    *,
    paper_id: str,
    manifest_key: str,
    source_hash: str,
) -> None:
    idempotency_key = f"paper:{paper_id}:parse_source:{PARSER_VERSION}:{source_hash}"
    exists = session.scalar(
        select(ProcessingTaskModel.id).where(
            ProcessingTaskModel.scope_key == _GLOBAL_TASK_SCOPE.scope_key,
            ProcessingTaskModel.idempotency_key == idempotency_key,
        )
    )
    if exists is None:
        session.add(
            ProcessingTaskModel(
                visibility=_GLOBAL_TASK_SCOPE.visibility,
                scope_key=_GLOBAL_TASK_SCOPE.scope_key,
                workspace_id=None,
                requested_by_user_id=None,
                credential_id=None,
                paper_id=paper_id,
                task_type=PARSE_SOURCE,
                priority=30,
                idempotency_key=idempotency_key,
                payload_json={
                    "manifest_key": manifest_key,
                    "source_hash": source_hash,
                    "parser_version": PARSER_VERSION,
                },
            )
        )


def enqueue_missing_parse_tasks(session_factory: sessionmaker[Session]) -> int:
    """Backfill parse tasks for source-ready papers created before Milestone 2."""
    created = 0
    with session_factory() as session:
        sources = session.scalars(
            select(PaperSourceModel).where(PaperSourceModel.source_type == "EXPANDED_SOURCE")
        ).all()
        for source in sources:
            before = len(session.new)
            _ensure_parse_task(
                session,
                paper_id=source.paper_id,
                manifest_key=source.storage_key,
                source_hash=source.sha256,
            )
            created += len(session.new) - before
        session.commit()
    return created


def _materialize_pdf_only_document(
    session: Session,
    *,
    paper: PaperModel,
    source: PaperSourceModel,
    storage: Storage,
    pdfinfo_command: str = "pdfinfo",
) -> ContentBlockModel:
    """Expose an arXiv PDF as a first-class reading block without pretending it was parsed."""

    if (
        source.paper_id != paper.id
        or source.source_type != "ARXIV_PDF"
        or source.storage_key != f"papers/{paper.id}/source/arxiv-fallback.pdf"
    ):
        raise TaskHandlerError("The arXiv PDF source is outside the current paper storage scope.")

    block_id = stable_content_id(
        "content-block",
        paper.id,
        source.sha256,
        "pdf-only-document",
    )
    try:
        with storage.get(source.storage_key) as pdf_stream:
            metadata = _inspect_pdf_with_toolchain(
                pdf_stream,
                pdfinfo_command=pdfinfo_command,
            )
        pdf_state: dict[str, Any] = {
            "status": "ready",
            "page_count": metadata.page_count,
            "page_width_points": metadata.page_width_points,
            "page_height_points": metadata.page_height_points,
            "viewer": "native-pdf-v1",
            "reader_version": PDF_ONLY_READER_VERSION,
        }
    except (PDFDocumentError, OSError) as exc:
        pdf_state = {
            "status": "failed",
            "error": str(exc),
            "viewer": "native-pdf-v1",
            "reader_version": PDF_ONLY_READER_VERSION,
        }

    block = session.get(ContentBlockModel, block_id)
    if block is None:
        block = ContentBlockModel(id=block_id, paper_id=paper.id)
        session.add(block)
    block.parent_id = None
    block.block_type = "PDF_DOCUMENT"
    block.ordinal_path = "00000000"
    block.heading_level = None
    block.source_text = "Original arXiv PDF"
    block.source_payload_json = {
        "source_type": "ARXIV_PDF",
        "sha256": source.sha256,
    }
    block.render_payload_json = {
        "document_mode": "pdf-only",
        "asset": {
            "path": ARXIV_PDF_ASSET_PATH,
            "mime_type": "application/pdf",
            "byte_size": source.byte_size,
        },
        "pdf_document": pdf_state,
    }
    block.source_locator_json = {"source_type": "ARXIV_PDF"}
    block.content_hash = source.sha256
    paper.processing_stage = ProcessingStage.READING_READY
    paper.processing_health = ProcessingHealth.PARTIAL
    return block


def reconcile_pdf_only_papers(
    session_factory: sessionmaker[Session],
    storage: Storage,
    *,
    toolchain_commands: ToolchainCommands | None = None,
) -> int:
    """Backfill the explicit reader mode for PDF-only records imported by older workers."""

    reconciled = 0
    with session_factory() as session:
        sources = session.scalars(
            select(PaperSourceModel).where(PaperSourceModel.source_type == "ARXIV_PDF")
        ).all()
        for source in sources:
            paper = session.get(PaperModel, source.paper_id)
            if (
                paper is None
                or source.storage_key != f"papers/{source.paper_id}/source/arxiv-fallback.pdf"
                or not storage.exists(source.storage_key)
            ):
                continue
            block_id = stable_content_id(
                "content-block",
                paper.id,
                source.sha256,
                "pdf-only-document",
            )
            block = session.get(ContentBlockModel, block_id)
            reader_version = (
                block.render_payload_json.get("pdf_document", {}).get("reader_version")
                if block is not None
                and isinstance(block.render_payload_json, dict)
                and isinstance(block.render_payload_json.get("pdf_document"), dict)
                else None
            )
            changed = (
                block is None
                or block.content_hash != source.sha256
                or reader_version != PDF_ONLY_READER_VERSION
                or paper.processing_stage != ProcessingStage.READING_READY
                or paper.processing_health != ProcessingHealth.PARTIAL
            )
            if changed:
                _materialize_pdf_only_document(
                    session,
                    paper=paper,
                    source=source,
                    storage=storage,
                    pdfinfo_command=(
                        toolchain_commands.pdfinfo if toolchain_commands else "pdfinfo"
                    ),
                )
            failed_fetch_tasks = session.scalars(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.paper_id == paper.id,
                    ProcessingTaskModel.task_type == FETCH_SOURCE,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                )
            ).all()
            for failed_task in failed_fetch_tasks:
                previous_result = (
                    failed_task.result_json if isinstance(failed_task.result_json, dict) else {}
                )
                failed_task.status = TaskStatus.SUCCEEDED
                failed_task.result_json = {
                    **previous_result,
                    "source_kind": "ARXIV_PDF",
                    "pdf_only": True,
                    "reader_version": PDF_ONLY_READER_VERSION,
                    "reconciled_on_worker_start": True,
                }
                failed_task.lease_expires_at = None
                failed_task.worker_id = None
                failed_task.error_code = None
                failed_task.error_message = None
                failed_task.finished_at = failed_task.finished_at or now_utc()
                changed = True
            if changed:
                reconciled += 1
        session.commit()
    return reconciled


def _ensure_table_render_task(
    session: Session,
    *,
    paper_id: str,
    block_id: str,
    content_hash: str,
    preamble_key: str,
    source_latex: str | None = None,
    repair_task_id: str | None = None,
    task_scope: TaskScope = _GLOBAL_TASK_SCOPE,
    toolchain_fingerprint: str | None = None,
) -> ProcessingTaskModel:
    repair_revision = f":repair:{repair_task_id}" if repair_task_id else ""
    cache_identity = toolchain_cache_identity(
        TABLE_RENDERER_VERSION,
        toolchain_fingerprint,
    )
    idempotency_key = (
        f"paper:{paper_id}:render_table:{cache_identity}:{block_id}:{content_hash}"
        f"{repair_revision}"
    )
    payload = {
        "block_id": block_id,
        "content_hash": content_hash,
        "preamble_key": preamble_key,
        "renderer_version": TABLE_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
        **({"source_latex": source_latex} if source_latex is not None else {}),
        **({"repair_task_id": repair_task_id} if repair_task_id is not None else {}),
    }
    stale_tasks = session.scalars(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.paper_id == paper_id,
            ProcessingTaskModel.task_type == RENDER_TABLE,
            ProcessingTaskModel.scope_key == task_scope.scope_key,
            ProcessingTaskModel.payload_json["block_id"].as_string() == block_id,
            ProcessingTaskModel.idempotency_key != idempotency_key,
            ProcessingTaskModel.status != TaskStatus.CANCELLED,
        )
    ).all()
    for stale_task in stale_tasks:
        current_result = stale_task.result_json if isinstance(stale_task.result_json, dict) else {}
        stale_task.status = TaskStatus.CANCELLED
        stale_task.result_json = {
            **current_result,
            "superseded": True,
            "superseded_reason": "A parser rerun produced a new canonical table source.",
            "superseded_by_content_hash": content_hash,
        }
        stale_task.lease_expires_at = None
        stale_task.worker_id = None
        stale_task.finished_at = stale_task.finished_at or now_utc()
    stale_repairs = session.scalars(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.paper_id == paper_id,
            ProcessingTaskModel.task_type == REPAIR_RENDER,
            ProcessingTaskModel.payload_json["block_id"].as_string() == block_id,
            ProcessingTaskModel.status.notin_([TaskStatus.SUCCEEDED, TaskStatus.CANCELLED]),
        )
    ).all()
    for stale_repair in stale_repairs:
        repair_payload = (
            stale_repair.payload_json if isinstance(stale_repair.payload_json, dict) else {}
        )
        if str(repair_payload.get("content_hash", "")) == content_hash:
            continue
        current_result = (
            stale_repair.result_json if isinstance(stale_repair.result_json, dict) else {}
        )
        stale_repair.status = TaskStatus.CANCELLED
        stale_repair.result_json = {
            **current_result,
            "superseded": True,
            "superseded_reason": "A parser rerun produced a new canonical table source.",
            "superseded_by_content_hash": content_hash,
        }
        stale_repair.lease_expires_at = None
        stale_repair.worker_id = None
        stale_repair.finished_at = stale_repair.finished_at or now_utc()
    existing = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.scope_key == task_scope.scope_key,
            ProcessingTaskModel.idempotency_key == idempotency_key,
        )
    )
    if existing is None:
        existing = ProcessingTaskModel(
            visibility=task_scope.visibility,
            scope_key=task_scope.scope_key,
            workspace_id=task_scope.workspace_id,
            requested_by_user_id=task_scope.requested_by_user_id,
            credential_id=task_scope.credential_id,
            paper_id=paper_id,
            task_type=RENDER_TABLE,
            priority=35,
            idempotency_key=idempotency_key,
            payload_json=payload,
            max_attempts=2,
        )
        session.add(existing)
        session.flush()
        return existing
    existing.status = TaskStatus.QUEUED
    existing.payload_json = payload
    existing.attempt_count = 0
    existing.max_attempts = 2
    existing.available_at = now_utc()
    existing.started_at = None
    existing.finished_at = None
    existing.lease_expires_at = None
    existing.worker_id = None
    existing.error_code = None
    existing.error_message = None
    existing.result_json = None
    return existing


def _stored_table_asset_is_visible(
    storage: Storage,
    *,
    paper_id: str,
    table_render: dict[str, Any],
    requires_raster: bool,
) -> bool:
    asset = table_render.get("asset")
    if not isinstance(asset, dict):
        return False
    mime_type = asset.get("mime_type")
    expected_mime_type = "image/png" if requires_raster else "image/svg+xml"
    if mime_type != expected_mime_type:
        return False
    asset_path = asset.get("path")
    if not isinstance(asset_path, str) or not asset_path:
        return False
    storage_key = f"papers/{paper_id}/derived/tables/{asset_path}"
    if not storage.exists(storage_key):
        return False
    with storage.get(storage_key) as stream:
        content = stream.read(5_000_001)
    if requires_raster:
        return table_png_has_visible_geometry(content)
    return table_svg_has_visible_geometry(content)


def _table_asset_is_content_addressed(table_render: dict[str, Any]) -> bool:
    asset = table_render.get("asset")
    asset_path = asset.get("path") if isinstance(asset, dict) else None
    return (
        isinstance(asset_path, str)
        and re.search(
            r"-[0-9a-f]{16}-[0-9a-f]{16}\.(?:svg|png)$",
            asset_path,
        )
        is not None
    )


def enqueue_missing_table_render_tasks(
    session_factory: sessionmaker[Session],
    storage: Storage | None = None,
    *,
    toolchain_fingerprint: str | None = None,
) -> int:
    """Upgrade legacy or incompatible table assets to the current render policy."""
    created = 0
    with session_factory() as session:
        source_hashes = {
            source.paper_id: source.sha256
            for source in session.scalars(
                select(PaperSourceModel).where(PaperSourceModel.source_type == "EXPANDED_SOURCE")
            ).all()
        }
        tables = session.scalars(
            select(ContentBlockModel).where(ContentBlockModel.block_type == "TABLE")
        ).all()
        for block in tables:
            source_latex = str(block.source_payload_json.get("latex", "")).strip()
            if not source_latex:
                continue
            requires_raster = bool(re.search(r"\\includegraphics\b", source_latex))
            table_render = (
                block.render_payload_json.get("table_render", {})
                if isinstance(block.render_payload_json, dict)
                else {}
            )
            if (
                isinstance(table_render, dict)
                and table_render.get("status") == "succeeded"
                and (
                    toolchain_fingerprint is None
                    or _toolchain_identity_matches(
                        table_render,
                        renderer_version=TABLE_RENDERER_VERSION,
                        fingerprint=toolchain_fingerprint,
                    )
                )
                and (
                    (
                        storage is not None
                        and _stored_table_asset_is_visible(
                            storage,
                            paper_id=block.paper_id,
                            table_render=table_render,
                            requires_raster=requires_raster,
                        )
                    )
                    or storage is None
                )
                and not (
                    table_render.get("renderer_version") == "latex-svg-v2.3"
                    and not _table_asset_is_content_addressed(table_render)
                )
                and table_render.get("renderer_version")
                not in INCOMPATIBLE_TABLE_RENDERER_VERSIONS
            ):
                continue
            corrected_latex = (
                table_render.get("corrected_latex")
                if isinstance(table_render, dict)
                and table_render.get("source") == "ai-repaired-latex"
                and isinstance(table_render.get("corrected_latex"), str)
                else None
            )
            repair_task_id = (
                str(table_render["repair_task_id"])
                if corrected_latex and isinstance(table_render.get("repair_task_id"), str)
                else None
            )
            cache_identity = toolchain_cache_identity(
                TABLE_RENDERER_VERSION,
                toolchain_fingerprint,
            )
            repair_revision = f":repair:{repair_task_id}" if repair_task_id else ""
            idempotency_key = (
                f"paper:{block.paper_id}:render_table:{cache_identity}:"
                f"{block.id}:{block.content_hash}{repair_revision}"
            )
            existing = session.scalar(
                select(ProcessingTaskModel.id).where(
                    ProcessingTaskModel.scope_key == _GLOBAL_TASK_SCOPE.scope_key,
                    ProcessingTaskModel.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                continue
            payload = dict(block.render_payload_json)
            payload["table_render"] = {
                "strategy": "latex-svg",
                "status": "queued",
                "complexity_reasons": table_complexity_reasons(source_latex),
                "renderer_version": TABLE_RENDERER_VERSION,
                **_toolchain_provenance(toolchain_fingerprint),
                **(
                    {
                        "source": "ai-repaired-latex",
                        "corrected_latex": corrected_latex,
                        "repair_task_id": repair_task_id,
                    }
                    if corrected_latex and repair_task_id
                    else {}
                ),
            }
            block.render_payload_json = payload
            source_hash = source_hashes.get(block.paper_id, "")
            preamble_key = (
                f"papers/{block.paper_id}/derived/parser/preamble-{source_hash}.tex"
                if source_hash
                else ""
            )
            _ensure_table_render_task(
                session,
                paper_id=block.paper_id,
                block_id=block.id,
                content_hash=block.content_hash,
                preamble_key=preamble_key,
                source_latex=corrected_latex,
                repair_task_id=repair_task_id,
                toolchain_fingerprint=toolchain_fingerprint,
            )
            created += 1
        session.commit()
    return created


def _ensure_latex_block_render_task(
    session: Session,
    *,
    paper_id: str,
    block_id: str,
    content_hash: str,
    preamble_key: str,
    render_kind: str,
    toolchain_fingerprint: str | None = None,
) -> ProcessingTaskModel:
    cache_identity = toolchain_cache_identity(
        LATEX_BLOCK_RENDERER_VERSION,
        toolchain_fingerprint,
    )
    idempotency_key = (
        f"paper:{paper_id}:render_latex_block:{cache_identity}:"
        f"{block_id}:{content_hash}"
    )
    payload = {
        "block_id": block_id,
        "content_hash": content_hash,
        "preamble_key": preamble_key,
        "render_kind": render_kind,
        "renderer_version": LATEX_BLOCK_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
    }
    stale_tasks = session.scalars(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.paper_id == paper_id,
            ProcessingTaskModel.task_type == RENDER_LATEX_BLOCK,
            ProcessingTaskModel.payload_json["block_id"].as_string() == block_id,
            ProcessingTaskModel.idempotency_key != idempotency_key,
            ProcessingTaskModel.status != TaskStatus.CANCELLED,
        )
    ).all()
    for stale_task in stale_tasks:
        previous_result = stale_task.result_json if isinstance(stale_task.result_json, dict) else {}
        stale_task.status = TaskStatus.CANCELLED
        stale_task.result_json = {
            **previous_result,
            "superseded": True,
            "superseded_reason": "A parser rerun changed the canonical LaTeX block.",
            "superseded_by_content_hash": content_hash,
        }
        stale_task.lease_expires_at = None
        stale_task.worker_id = None
        stale_task.finished_at = stale_task.finished_at or now_utc()
    existing = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.scope_key == _GLOBAL_TASK_SCOPE.scope_key,
            ProcessingTaskModel.idempotency_key == idempotency_key,
        )
    )
    if existing is None:
        existing = ProcessingTaskModel(
            visibility=_GLOBAL_TASK_SCOPE.visibility,
            scope_key=_GLOBAL_TASK_SCOPE.scope_key,
            workspace_id=None,
            requested_by_user_id=None,
            credential_id=None,
            paper_id=paper_id,
            task_type=RENDER_LATEX_BLOCK,
            priority=35,
            idempotency_key=idempotency_key,
            payload_json=payload,
            max_attempts=2,
        )
        session.add(existing)
        session.flush()
        return existing
    existing.status = TaskStatus.QUEUED
    existing.payload_json = payload
    existing.attempt_count = 0
    existing.max_attempts = 2
    existing.available_at = now_utc()
    existing.started_at = None
    existing.finished_at = None
    existing.lease_expires_at = None
    existing.worker_id = None
    existing.error_code = None
    existing.error_message = None
    existing.result_json = None
    return existing


def enqueue_missing_latex_block_render_tasks(
    session_factory: sessionmaker[Session],
    *,
    toolchain_fingerprint: str | None = None,
) -> int:
    """Backfill source-faithful rendering for algorithms and visual custom environments."""

    created = 0
    with session_factory() as session:
        source_hashes = {
            source.paper_id: source.sha256
            for source in session.scalars(
                select(PaperSourceModel).where(PaperSourceModel.source_type == "EXPANDED_SOURCE")
            ).all()
        }
        blocks = session.scalars(
            select(ContentBlockModel).where(
                ContentBlockModel.block_type.in_({"ALGORITHM", "LATEX_BLOCK"})
            )
        ).all()
        for block in blocks:
            source_latex = str(block.source_payload_json.get("latex", "")).strip()
            if not source_latex:
                continue
            render_state = (
                block.render_payload_json.get("latex_block_render", {})
                if isinstance(block.render_payload_json, dict)
                else {}
            )
            if (
                isinstance(render_state, dict)
                and render_state.get("status") == "succeeded"
                and _toolchain_identity_matches(
                    render_state,
                    renderer_version=LATEX_BLOCK_RENDERER_VERSION,
                    fingerprint=toolchain_fingerprint,
                )
            ):
                continue
            render_kind = "algorithm" if block.block_type == "ALGORITHM" else "visual"
            cache_identity = toolchain_cache_identity(
                LATEX_BLOCK_RENDERER_VERSION,
                toolchain_fingerprint,
            )
            idempotency_key = (
                f"paper:{block.paper_id}:render_latex_block:{cache_identity}:"
                f"{block.id}:{block.content_hash}"
            )
            if (
                session.scalar(
                    select(ProcessingTaskModel).where(
                        ProcessingTaskModel.idempotency_key == idempotency_key
                    )
                )
                is not None
            ):
                continue
            payload = dict(block.render_payload_json)
            payload["latex_block_render"] = {
                "strategy": "native-latex-block",
                "status": "queued",
                "render_kind": render_kind,
                "renderer_version": LATEX_BLOCK_RENDERER_VERSION,
                **_toolchain_provenance(toolchain_fingerprint),
            }
            block.render_payload_json = payload
            source_hash = source_hashes.get(block.paper_id, "")
            preamble_key = (
                f"papers/{block.paper_id}/derived/parser/preamble-{source_hash}.tex"
                if source_hash
                else ""
            )
            _ensure_latex_block_render_task(
                session,
                paper_id=block.paper_id,
                block_id=block.id,
                content_hash=block.content_hash,
                preamble_key=preamble_key,
                render_kind=render_kind,
                toolchain_fingerprint=toolchain_fingerprint,
            )
            created += 1
        session.commit()
    return created


def _ensure_figure_render_task(
    session: Session,
    *,
    paper_id: str,
    block_id: str,
    content_hash: str,
    preamble_key: str,
    source_latex: str | None = None,
    repair_task_id: str | None = None,
    task_scope: TaskScope = _GLOBAL_TASK_SCOPE,
    toolchain_fingerprint: str | None = None,
) -> ProcessingTaskModel:
    repair_revision = f":repair:{repair_task_id}" if repair_task_id else ""
    cache_identity = toolchain_cache_identity(
        FIGURE_RENDERER_VERSION,
        toolchain_fingerprint,
    )
    idempotency_key = (
        f"paper:{paper_id}:render_figure:{cache_identity}:{block_id}:{content_hash}"
        f"{repair_revision}"
    )
    payload = {
        "block_id": block_id,
        "content_hash": content_hash,
        "preamble_key": preamble_key,
        "renderer_version": FIGURE_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
        **({"source_latex": source_latex} if source_latex is not None else {}),
        **({"repair_task_id": repair_task_id} if repair_task_id is not None else {}),
    }
    stale_tasks = session.scalars(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.paper_id == paper_id,
            ProcessingTaskModel.task_type == RENDER_FIGURE,
            ProcessingTaskModel.scope_key == task_scope.scope_key,
            ProcessingTaskModel.payload_json["block_id"].as_string() == block_id,
            ProcessingTaskModel.idempotency_key != idempotency_key,
            ProcessingTaskModel.status != TaskStatus.CANCELLED,
        )
    ).all()
    for stale_task in stale_tasks:
        current_result = stale_task.result_json if isinstance(stale_task.result_json, dict) else {}
        stale_task.status = TaskStatus.CANCELLED
        stale_task.result_json = {
            **current_result,
            "superseded": True,
            "superseded_reason": "A parser rerun produced a new canonical figure source.",
            "superseded_by_content_hash": content_hash,
        }
        stale_task.lease_expires_at = None
        stale_task.worker_id = None
        stale_task.finished_at = stale_task.finished_at or now_utc()
    existing = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.scope_key == task_scope.scope_key,
            ProcessingTaskModel.idempotency_key == idempotency_key,
        )
    )
    if existing is None:
        existing = ProcessingTaskModel(
            visibility=task_scope.visibility,
            scope_key=task_scope.scope_key,
            workspace_id=task_scope.workspace_id,
            requested_by_user_id=task_scope.requested_by_user_id,
            credential_id=task_scope.credential_id,
            paper_id=paper_id,
            task_type=RENDER_FIGURE,
            priority=35,
            idempotency_key=idempotency_key,
            payload_json=payload,
            max_attempts=2,
        )
        session.add(existing)
        session.flush()
        return existing
    existing.status = TaskStatus.QUEUED
    existing.payload_json = payload
    existing.attempt_count = 0
    existing.max_attempts = 2
    existing.available_at = now_utc()
    existing.started_at = None
    existing.finished_at = None
    existing.lease_expires_at = None
    existing.worker_id = None
    existing.error_code = None
    existing.error_message = None
    existing.result_json = None
    return existing


def enqueue_missing_figure_render_tasks(
    session_factory: sessionmaker[Session],
    *,
    toolchain_fingerprint: str | None = None,
) -> int:
    """Backfill LaTeX-drawn figures parsed before the figure renderer existed."""
    created = 0
    with session_factory() as session:
        source_hashes = {
            source.paper_id: source.sha256
            for source in session.scalars(
                select(PaperSourceModel).where(PaperSourceModel.source_type == "EXPANDED_SOURCE")
            ).all()
        }
        figures = session.scalars(
            select(ContentBlockModel).where(ContentBlockModel.block_type == "FIGURE")
        ).all()
        for block in figures:
            source_latex = str(block.source_payload_json.get("latex", "")).strip()
            payload = (
                dict(block.render_payload_json)
                if isinstance(block.render_payload_json, dict)
                else {}
            )
            if not figure_requires_latex_render(
                source_latex,
                resolved_assets=payload.get("assets"),
            ):
                continue
            figure_render = payload.get("figure_render", {})
            if (
                isinstance(figure_render, dict)
                and figure_render.get("status") == "succeeded"
                and _toolchain_identity_matches(
                    figure_render,
                    renderer_version=FIGURE_RENDERER_VERSION,
                    fingerprint=toolchain_fingerprint,
                )
            ):
                continue
            corrected_latex = (
                figure_render.get("corrected_latex")
                if isinstance(figure_render, dict)
                and figure_render.get("source") == "ai-repaired-latex"
                and isinstance(figure_render.get("corrected_latex"), str)
                else None
            )
            repair_task_id = (
                str(figure_render["repair_task_id"])
                if corrected_latex and isinstance(figure_render.get("repair_task_id"), str)
                else None
            )
            cache_identity = toolchain_cache_identity(
                FIGURE_RENDERER_VERSION,
                toolchain_fingerprint,
            )
            repair_revision = f":repair:{repair_task_id}" if repair_task_id else ""
            idempotency_key = (
                f"paper:{block.paper_id}:render_figure:{cache_identity}:"
                f"{block.id}:{block.content_hash}{repair_revision}"
            )
            existing = session.scalar(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                continue
            payload["figure_render"] = {
                "strategy": "latex-svg",
                "status": "queued",
                "renderer_version": FIGURE_RENDERER_VERSION,
                **_toolchain_provenance(toolchain_fingerprint),
                **(
                    {
                        "source": "ai-repaired-latex",
                        "corrected_latex": corrected_latex,
                        "repair_task_id": repair_task_id,
                    }
                    if corrected_latex and repair_task_id
                    else {}
                ),
            }
            block.render_payload_json = payload
            source_hash = source_hashes.get(block.paper_id, "")
            preamble_key = (
                # The parser owns the one canonical paper-local render context shared by
                # formulas, tables, and figures. Backfills must reference that durable key
                # instead of guessing a renderer-specific copy that may not exist.
                f"papers/{block.paper_id}/derived/parser/preamble-{source_hash}.tex"
                if source_hash
                else ""
            )
            _ensure_figure_render_task(
                session,
                paper_id=block.paper_id,
                block_id=block.id,
                content_hash=block.content_hash,
                preamble_key=preamble_key,
                source_latex=corrected_latex,
                repair_task_id=repair_task_id,
                toolchain_fingerprint=toolchain_fingerprint,
            )
            created += 1
        session.commit()
    return created


def fetch_source(
    task: QueuedTask,
    client: ArxivClient,
    session_factory: sessionmaker[Session],
    storage: Storage,
    *,
    toolchain_commands: ToolchainCommands | None = None,
) -> dict[str, Any]:
    if task.paper_id is None:
        raise TaskHandlerError("Source tasks require a paper identifier.")
    with session_factory() as session:
        _paper(session, task)

    paper_prefix = f"papers/{task.paper_id}"
    archive_key = f"{paper_prefix}/source/arxiv-source.tar"
    partial_key = f"{paper_prefix}/tmp/arxiv-source.part"
    identifier = _identifier(task).canonical
    partial_content = b""
    expected_total: int | None = None
    expected_identity: str | None = None

    with session_factory() as session:
        stored_task = session.get(ProcessingTaskModel, task.id)
        saved_result = stored_task.result_json if stored_task is not None else None
        saved_download = saved_result.get("download") if isinstance(saved_result, dict) else None
    if storage.exists(partial_key):
        with storage.get(partial_key) as stream:
            candidate = stream.read()
        saved_bytes = saved_download.get("bytes_downloaded") if saved_download else None
        saved_identifier = saved_download.get("identifier") if saved_download else None
        if saved_identifier == identifier and saved_bytes == len(candidate):
            partial_content = candidate
            saved_total = saved_download.get("bytes_total")
            saved_identity = saved_download.get("source_identity")
            expected_total = saved_total if isinstance(saved_total, int) else None
            expected_identity = saved_identity if isinstance(saved_identity, str) else None
        else:
            storage.delete(partial_key)

    partial_buffer = bytearray(partial_content)

    def persist_progress(
        progress: SourceDownloadProgress,
        chunk: bytes,
        replace_partial: bool,
    ) -> None:
        nonlocal partial_buffer
        if replace_partial:
            partial_buffer = bytearray(chunk)
        else:
            partial_buffer.extend(chunk)
        if len(partial_buffer) != progress.bytes_downloaded:
            raise ArxivClientError("The local partial source checkpoint is inconsistent.")
        storage.put(partial_key, io.BytesIO(partial_buffer))
        remaining = (
            max(0, progress.bytes_total - progress.bytes_downloaded)
            if progress.bytes_total is not None
            else None
        )
        percent = (
            round(progress.bytes_downloaded / progress.bytes_total * 100, 1)
            if progress.bytes_total
            else None
        )
        with session_factory() as progress_session:
            stored = progress_session.get(ProcessingTaskModel, task.id)
            if stored is None:
                return
            current_result = stored.result_json if isinstance(stored.result_json, dict) else {}
            stored.result_json = {
                **current_result,
                "download": {
                    "phase": "DOWNLOADING",
                    "identifier": identifier,
                    "bytes_downloaded": progress.bytes_downloaded,
                    "bytes_total": progress.bytes_total,
                    "bytes_remaining": remaining,
                    "percent": percent,
                    "resumable": progress.bytes_downloaded > 0,
                    "resumed_bytes": progress.resumed_bytes,
                    "source_identity": progress.source_identity,
                    "updated_at": now_utc().isoformat(),
                },
            }
            if stored.status == TaskStatus.RUNNING:
                stored.lease_expires_at = now_utc() + timedelta(seconds=120)
            progress_session.commit()

    try:
        downloaded = client.fetch_source(
            _identifier(task),
            partial_content=partial_content,
            expected_total=expected_total,
            expected_identity=expected_identity,
            progress_callback=persist_progress,
        )
    except ArxivResumeError:
        # A new arXiv revision or changed response validator makes old bytes unsafe.
        # Remove them and restart in the same durable task instead of binding ranges.
        storage.delete(partial_key)
        partial_buffer = bytearray()
        downloaded = client.fetch_source(
            _identifier(task),
            progress_callback=persist_progress,
        )

    with session_factory() as progress_session:
        stored = progress_session.get(ProcessingTaskModel, task.id)
        if stored is not None:
            current_result = stored.result_json if isinstance(stored.result_json, dict) else {}
            download_state = current_result.get("download", {})
            stored.result_json = {
                **current_result,
                "download": {**download_state, "phase": "VALIDATING"},
            }
            progress_session.commit()

    archive_sha = hashlib.sha256(downloaded.content).hexdigest()
    if downloaded.content.startswith(b"%PDF-"):
        # Some submissions expose only the published PDF. Treat that immutable response as an
        # explicit, limited reading mode; never pass binary data to the LaTeX source parser.
        pdf_key = f"{paper_prefix}/source/{ARXIV_PDF_ASSET_PATH}"
        storage.put(pdf_key, io.BytesIO(downloaded.content))
        storage.delete(partial_key)
        with session_factory() as session:
            paper = _paper(session, task)
            source = _upsert_source(
                session,
                paper_id=task.paper_id,
                source_type="ARXIV_PDF",
                storage_key=pdf_key,
                sha256=archive_sha,
                byte_size=len(downloaded.content),
                mime_type="application/pdf",
            )
            block = _materialize_pdf_only_document(
                session,
                paper=paper,
                source=source,
                storage=storage,
                pdfinfo_command=(toolchain_commands.pdfinfo if toolchain_commands else "pdfinfo"),
            )
            session.commit()
        return {
            "source_kind": "ARXIV_PDF",
            "pdf_only": True,
            "block_id": block.id,
            "byte_size": len(downloaded.content),
            "reader_version": PDF_ONLY_READER_VERSION,
        }
    expanded_prefix = f"{paper_prefix}/source/expanded"
    storage.delete_prefix(expanded_prefix)
    try:
        manifest = extract_source_archive(
            downloaded.content,
            storage,
            expanded_prefix,
        )
    except ArchiveIntegrityError as error:
        storage.delete_prefix(expanded_prefix)
        storage.delete(archive_key)
        storage.delete(partial_key)
        with session_factory() as progress_session:
            stored = progress_session.get(ProcessingTaskModel, task.id)
            if stored is not None:
                current_result = stored.result_json if isinstance(stored.result_json, dict) else {}
                stored.result_json = {
                    **current_result,
                    "download": {
                        "phase": "RESTART_REQUIRED",
                        "identifier": identifier,
                        "bytes_downloaded": 0,
                        "bytes_total": None,
                        "bytes_remaining": None,
                        "percent": None,
                        "resumable": False,
                        "resumed_bytes": 0,
                        "source_identity": None,
                        "updated_at": now_utc().isoformat(),
                    },
                }
                progress_session.commit()
        raise ArxivClientError(
            "The arXiv source archive was incomplete or corrupt. Its partial data was removed; "
            "the next retry will download a clean copy."
        ) from error
    storage.put(archive_key, io.BytesIO(downloaded.content))
    manifest.update(
        {
            "arxiv_id": task.payload["arxiv_id"],
            "arxiv_version": task.payload.get("arxiv_version", ""),
            "archive_sha256": archive_sha,
        }
    )
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    manifest_key = f"{paper_prefix}/derived/source-manifest.json"
    storage.put(manifest_key, io.BytesIO(manifest_bytes))

    try:
        with session_factory() as session:
            paper = _paper(session, task)
            _upsert_source(
                session,
                paper_id=paper.id,
                source_type="ARXIV_TARBALL",
                storage_key=archive_key,
                sha256=archive_sha,
                byte_size=len(downloaded.content),
                mime_type=downloaded.content_type,
            )
            _upsert_source(
                session,
                paper_id=paper.id,
                source_type="EXPANDED_SOURCE",
                storage_key=manifest_key,
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                byte_size=manifest["total_bytes"],
                mime_type="application/json",
            )
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            _ensure_parse_task(
                session,
                paper_id=paper.id,
                manifest_key=manifest_key,
                source_hash=manifest_sha,
            )
            failed_count = session.scalar(
                select(func.count())
                .select_from(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == paper.id,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                )
            )
            paper.processing_stage = ProcessingStage.SOURCE_READY
            paper.processing_health = (
                ProcessingHealth.PARTIAL if failed_count else ProcessingHealth.OK
            )
            session.commit()
    except TaskHandlerError:
        storage.delete_prefix(paper_prefix)
        raise
    storage.delete(partial_key)
    return {
        "archive_sha256": archive_sha,
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "manifest_key": manifest_key,
    }


def _bibliography_digest(result: BibliographyResult) -> str:
    material = {
        "processor_version": result.processor_version,
        "requested_style": result.requested_style,
        "resolved_style": result.resolved_style,
        "fallback_used": result.fallback_used,
        "source_files": result.source_files,
        "entries": [
            {
                "key": entry.citation_key,
                "type": entry.entry_type,
                "fields": entry.fields,
                "ordinal": entry.ordinal,
                "label": entry.display_label,
                "formatted_latex": entry.formatted_latex,
            }
            for entry in result.entries
        ],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _persist_bibliography(
    session: Session,
    *,
    paper: PaperModel,
    result: BibliographyResult,
    renderer_preamble: str,
) -> dict[str, tuple[str, Any]]:
    bibliography_id = stable_content_id("paper-bibliography", paper.id)
    bibliography = session.get(PaperBibliographyModel, bibliography_id)
    if bibliography is None:
        bibliography = PaperBibliographyModel(id=bibliography_id, paper_id=paper.id)
        session.add(bibliography)
    bibliography.status = result.status
    bibliography.requested_style = result.requested_style
    bibliography.resolved_style = result.resolved_style
    bibliography.fallback_used = result.fallback_used
    bibliography.processor_version = result.processor_version
    bibliography.source_files_json = list(result.source_files)
    bibliography.warnings_json = list(result.warnings)
    bibliography.content_hash = _bibliography_digest(result)
    existing_entries = {
        entry.id: entry
        for entry in session.scalars(
            select(BibliographyEntryModel).where(
                BibliographyEntryModel.bibliography_id == bibliography_id
            )
        ).all()
    }
    current_entry_ids: set[str] = set()
    entries_by_key: dict[str, tuple[str, Any]] = {}
    persisted_keys: set[str] = set()
    for entry in result.entries:
        normalized_key = entry.citation_key.casefold()
        if normalized_key in persisted_keys:
            continue
        persisted_keys.add(normalized_key)
        entry_id = stable_content_id(
            "bibliography-entry",
            paper.id,
            normalized_key,
        )
        current_entry_ids.add(entry_id)
        entries_by_key[normalized_key] = (entry_id, entry)
        stored = existing_entries.get(entry_id)
        if stored is None:
            stored = BibliographyEntryModel(
                id=entry_id,
                bibliography_id=bibliography_id,
            )
            session.add(stored)
        formatted_text, render_payload = render_latex_prose(entry.formatted_latex or "")
        render_payload = normalize_render_payload_with_preamble(
            render_payload,
            renderer_preamble,
        )
        stored.citation_key = entry.citation_key
        stored.citation_key_normalized = normalized_key
        stored.entry_type = entry.entry_type
        stored.source_path = entry.source_path
        stored.raw_bibtex = entry.raw_bibtex
        stored.fields_json = entry.fields
        stored.title = entry.title
        stored.container_title = entry.container_title
        stored.publication_year = entry.year
        stored.doi = entry.doi
        stored.url = entry.url
        stored.eprint = entry.eprint
        stored.archive_prefix = entry.archive_prefix
        stored.isbn = entry.isbn
        stored.issn = entry.issn
        stored.ordinal = entry.ordinal
        stored.display_label = entry.display_label
        stored.citation_author = entry.citation_author
        stored.citation_year = entry.citation_year
        stored.formatted_latex = entry.formatted_latex
        stored.formatted_text = formatted_text or entry.title or entry.citation_key
        stored.render_payload_json = render_payload
        stored.content_hash = hashlib.sha256(
            json.dumps(
                {
                    "fields": entry.fields,
                    "persons": [
                        {
                            "role": person.role,
                            "position": person.position,
                            "display_name": person.display_name,
                        }
                        for person in entry.persons
                    ],
                    "formatted_latex": entry.formatted_latex,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        session.execute(
            delete(BibliographyPersonModel).where(BibliographyPersonModel.entry_id == entry_id)
        )
        session.add_all(
            BibliographyPersonModel(
                id=stable_content_id(
                    "bibliography-person",
                    entry_id,
                    person.role,
                    str(person.position),
                ),
                entry_id=entry_id,
                role=person.role,
                position=person.position,
                display_name=person.display_name,
                given_names_json=list(person.given_names),
                family_names_json=list(person.family_names),
                prefix_names_json=list(person.prefix_names),
                suffix_names_json=list(person.suffix_names),
                initials_json=list(person.initials),
            )
            for person in entry.persons
        )
    removed_entry_ids = set(existing_entries) - current_entry_ids
    if removed_entry_ids:
        session.execute(
            delete(BibliographyEntryModel).where(BibliographyEntryModel.id.in_(removed_entry_ids))
        )
    session.flush()
    return entries_by_key


def parse_source(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    storage: Storage,
    *,
    toolchain_commands: ToolchainCommands | None = None,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    manifest_key = str(task.payload.get("manifest_key", ""))
    source_hash = str(task.payload.get("source_hash", ""))
    if task.paper_id is None or not manifest_key or not source_hash:
        raise TaskHandlerError("Parse tasks require a paper, manifest, and source hash.")
    manifest = _load_task_source_manifest(
        storage,
        paper_id=task.paper_id,
        manifest_key=manifest_key,
    )
    if manifest is None:  # pragma: no cover - required manifests fail in the loader
        raise TaskHandlerError("The paper source manifest is unavailable.")
    document = parse_latex_project(manifest, storage)
    bibliography_index = (
        document.project_index.get("bibliography", {})
        if isinstance(document.project_index, dict)
        and isinstance(document.project_index.get("bibliography"), dict)
        else {}
    )
    try:
        bibliography_result = process_bibliography(
            manifest=manifest,
            storage=storage,
            bibliography_index=bibliography_index,
            latex_command=(toolchain_commands.pdflatex if toolchain_commands else None),
            biber_command=(toolchain_commands.biber if toolchain_commands else None),
            bibtex_command=(toolchain_commands.bibtex if toolchain_commands else None),
            pdftotext_command=(toolchain_commands.pdftotext if toolchain_commands else None),
        )
    except BibliographyProcessingError as error:
        bibliography_result = BibliographyResult(
            status="FAILED",
            requested_style=(str(bibliography_index.get("requested_style") or "").strip() or None),
            resolved_style=None,
            fallback_used=True,
            source_files=tuple(
                str(path)
                for path in bibliography_index.get("source_files", [])
                if isinstance(path, str)
            ),
            entries=(),
            warnings=(str(error),),
        )
    manifest_files = {
        str(item.get("path", "")): item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }
    pdf_document_states: dict[str, dict[str, Any]] = {}
    for block in document.blocks:
        if block.block_type != "PDF_DOCUMENT":
            continue
        asset = block.render_payload.get("asset")
        asset_path = str(asset.get("path", "")) if isinstance(asset, dict) else ""
        manifest_item = manifest_files.get(asset_path)
        storage_key = (
            str(manifest_item.get("storage_key", "")) if isinstance(manifest_item, dict) else ""
        )
        if not storage_key:
            pdf_document_states[block.ordinal_path] = {
                "status": "failed",
                "error": "The embedded PDF could not be resolved from the source manifest.",
                "viewer": "native-pdf-v1",
            }
            continue
        try:
            with storage.get(storage_key) as pdf_stream:
                metadata = _inspect_pdf_with_toolchain(
                    pdf_stream,
                    pdfinfo_command=(
                        toolchain_commands.pdfinfo if toolchain_commands else "pdfinfo"
                    ),
                )
            pdf_document_states[block.ordinal_path] = {
                "status": "ready",
                "page_count": metadata.page_count,
                "page_width_points": metadata.page_width_points,
                "page_height_points": metadata.page_height_points,
                "viewer": "native-pdf-v1",
            }
        except (PDFDocumentError, OSError) as exc:
            pdf_document_states[block.ordinal_path] = {
                "status": "failed",
                "error": str(exc),
                "viewer": "native-pdf-v1",
            }
    renderable_tables = [
        block
        for block in document.blocks
        if block.block_type == "TABLE" and str(block.source_payload.get("latex", "")).strip()
    ]
    complex_tables = [
        block
        for block in renderable_tables
        if table_complexity_reasons(str(block.source_payload.get("latex", "")))
    ]
    preamble_key = f"papers/{task.paper_id}/derived/parser/preamble-{source_hash}.tex"
    storage.put(preamble_key, io.BytesIO(document.renderer_preamble.encode("utf-8")))
    project_index_key = f"papers/{task.paper_id}/derived/parser/project-index-{source_hash}.json"
    storage.put(
        project_index_key,
        io.BytesIO(
            json.dumps(
                document.project_index or {},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
        ),
    )
    renderable_figures = [
        block
        for block in document.blocks
        if block.block_type == "FIGURE"
        and figure_requires_latex_render(
            str(block.source_payload.get("latex", "")),
            resolved_assets=block.render_payload.get("assets"),
        )
    ]
    renderable_latex_blocks = [
        block
        for block in document.blocks
        if block.block_type in {"ALGORITHM", "LATEX_BLOCK"}
        and str(block.source_payload.get("latex", "")).strip()
    ]
    figure_preamble_key = preamble_key

    with session_factory() as session:
        paper = _paper(session, task)
        session.execute(
            delete(CitationOccurrenceModel).where(CitationOccurrenceModel.paper_id == paper.id)
        )
        bibliography_entries_by_key = _persist_bibliography(
            session,
            paper=paper,
            result=bibliography_result,
            renderer_preamble=document.renderer_preamble,
        )
        block_ids = {
            block.ordinal_path: stable_content_id(
                "content-block",
                paper.id,
                source_hash,
                block.ordinal_path,
            )
            for block in document.blocks
        }
        pending_citation_occurrences: list[dict[str, Any]] = []

        def materialize_reference_targets(
            value: Any,
            *,
            block_id: str,
            path: tuple[str | int, ...] = (),
            suppress_top_level_segment_occurrences: bool = False,
        ) -> Any:
            if isinstance(value, list):
                return [
                    materialize_reference_targets(
                        item,
                        block_id=block_id,
                        path=(*path, index),
                        suppress_top_level_segment_occurrences=(
                            suppress_top_level_segment_occurrences
                        ),
                    )
                    for index, item in enumerate(value)
                ]
            if isinstance(value, dict):
                materialized = {
                    key: materialize_reference_targets(
                        item,
                        block_id=block_id,
                        path=(*path, key),
                        suppress_top_level_segment_occurrences=(
                            suppress_top_level_segment_occurrences
                        ),
                    )
                    for key, item in value.items()
                }
                ordinal = materialized.get("target_ordinal_path")
                if materialized.get("type") == "reference" and isinstance(ordinal, str):
                    target_block_id = block_ids.get(ordinal)
                    if target_block_id is None:
                        materialized.update(
                            {
                                "status": "UNRESOLVED",
                                "text": "??",
                                "reason": "TARGET_BLOCK_MISSING",
                            }
                        )
                    else:
                        materialized["target_block_id"] = target_block_id
                if materialized.get("type") == "citation":
                    materialized = materialize_citation_segment(
                        materialized,
                        entries_by_key=bibliography_entries_by_key,
                    )
                    cluster_path = json.dumps(list(path), ensure_ascii=False, separators=(",", ":"))
                    record_occurrence = not (
                        suppress_top_level_segment_occurrences
                        and len(path) >= 2
                        and path[0] == "segments"
                    )
                    for item_index, item in enumerate(materialized.get("items", [])):
                        if not isinstance(item, dict):
                            continue
                        citation_key = str(item.get("citation_key") or "").strip()
                        if not citation_key or not record_occurrence:
                            continue
                        pending_citation_occurrences.append(
                            {
                                "id": stable_content_id(
                                    "citation-occurrence",
                                    block_id,
                                    cluster_path,
                                    str(item_index),
                                ),
                                "paper_id": paper.id,
                                "block_id": block_id,
                                "bibliography_entry_id": item.get("entry_id"),
                                "cluster_path": cluster_path,
                                "item_index": item_index,
                                "citation_key": citation_key,
                                "citation_key_normalized": citation_key.casefold(),
                                "command": str(materialized.get("command") or "cite")[:64],
                                "prefix": materialized.get("prefix"),
                                "suffix": materialized.get("suffix"),
                            }
                        )
                return materialized
            return value

        existing_blocks = {
            block.id: block
            for block in session.scalars(
                select(ContentBlockModel).where(ContentBlockModel.paper_id == paper.id)
            ).all()
        }
        for block in document.blocks:
            block_id = block_ids[block.ordinal_path]
            render_payload = materialize_reference_targets(
                block.render_payload,
                block_id=block_id,
                suppress_top_level_segment_occurrences=(
                    block.block_type == "THEOREM"
                    and isinstance(block.render_payload.get("parts"), list)
                    and bool(block.render_payload["parts"])
                ),
            )
            if block.block_type == "PDF_DOCUMENT":
                render_payload["pdf_document"] = pdf_document_states.get(
                    block.ordinal_path,
                    {
                        "status": "failed",
                        "error": "Embedded PDF inspection did not complete.",
                        "viewer": "native-pdf-v1",
                    },
                )
            complexity_reasons = (
                table_complexity_reasons(str(block.source_payload.get("latex", "")))
                if block.block_type == "TABLE"
                else []
            )
            if block.block_type == "TABLE":
                has_latex_source = bool(str(block.source_payload.get("latex", "")).strip())
                render_payload["table_render"] = {
                    "strategy": "latex-svg",
                    "status": "queued" if has_latex_source else "failed",
                    "complexity_reasons": complexity_reasons,
                    "renderer_version": TABLE_RENDERER_VERSION,
                    **_toolchain_provenance(toolchain_fingerprint),
                    **(
                        {}
                        if has_latex_source
                        else {"error": "The canonical table has no renderable LaTeX source."}
                    ),
                }
            if block.block_type == "FIGURE" and figure_requires_latex_render(
                str(block.source_payload.get("latex", "")),
                resolved_assets=block.render_payload.get("assets"),
            ):
                render_payload["figure_render"] = {
                    "strategy": "latex-svg",
                    "status": "queued",
                    "renderer_version": FIGURE_RENDERER_VERSION,
                    **_toolchain_provenance(toolchain_fingerprint),
                }
            if block.block_type in {"ALGORITHM", "LATEX_BLOCK"}:
                render_kind = "algorithm" if block.block_type == "ALGORITHM" else "visual"
                has_latex_source = bool(str(block.source_payload.get("latex", "")).strip())
                render_payload["latex_block_render"] = {
                    "strategy": "native-latex-block",
                    "status": "queued" if has_latex_source else "failed",
                    "render_kind": render_kind,
                    "renderer_version": LATEX_BLOCK_RENDERER_VERSION,
                    **_toolchain_provenance(toolchain_fingerprint),
                    **(
                        {}
                        if has_latex_source
                        else {"error": "The canonical block has no renderable LaTeX source."}
                    ),
                }
            stored = existing_blocks.get(block_id)
            if stored is None:
                stored = ContentBlockModel(id=block_id, paper_id=paper.id)
                session.add(stored)
            elif stored.content_hash == block.content_hash:
                render_payload = _preserve_applied_render_repair(
                    block_type=block.block_type,
                    parsed_payload=render_payload,
                    stored_payload=stored.render_payload_json,
                )
                render_payload = _preserve_successful_formula_renders(
                    block_type=block.block_type,
                    parsed_payload=render_payload,
                    stored_payload=stored.render_payload_json,
                    toolchain_fingerprint=toolchain_fingerprint,
                )
            stored.parent_id = (
                block_ids.get(block.parent_ordinal_path)
                if block.parent_ordinal_path is not None
                else None
            )
            stored.block_type = block.block_type
            stored.ordinal_path = block.ordinal_path
            stored.heading_level = block.heading_level
            stored.source_text = block.source_text
            stored.source_payload_json = block.source_payload
            stored.render_payload_json = render_payload
            stored.source_locator_json = block.source_locator
            stored.content_hash = block.content_hash
            if block.block_type == "TABLE" and str(block.source_payload.get("latex", "")).strip():
                _ensure_table_render_task(
                    session,
                    paper_id=paper.id,
                    block_id=block_id,
                    content_hash=block.content_hash,
                    preamble_key=preamble_key,
                    toolchain_fingerprint=toolchain_fingerprint,
                )
            if block.block_type == "FIGURE" and figure_requires_latex_render(
                str(block.source_payload.get("latex", "")),
                resolved_assets=block.render_payload.get("assets"),
            ):
                _ensure_figure_render_task(
                    session,
                    paper_id=paper.id,
                    block_id=block_id,
                    content_hash=block.content_hash,
                    preamble_key=figure_preamble_key,
                    toolchain_fingerprint=toolchain_fingerprint,
                )
            if (
                block.block_type in {"ALGORITHM", "LATEX_BLOCK"}
                and str(block.source_payload.get("latex", "")).strip()
            ):
                _ensure_latex_block_render_task(
                    session,
                    paper_id=paper.id,
                    block_id=block_id,
                    content_hash=block.content_hash,
                    preamble_key=preamble_key,
                    render_kind=("algorithm" if block.block_type == "ALGORITHM" else "visual"),
                    toolchain_fingerprint=toolchain_fingerprint,
                )
        session.add_all(
            CitationOccurrenceModel(**occurrence) for occurrence in pending_citation_occurrences
        )
        abstract_block = ensure_abstract_block(
            session,
            paper,
            renderer_preamble=document.renderer_preamble,
        )
        current_block_specs = {
            block_ids[block.ordinal_path]: (block.block_type, block.content_hash)
            for block in document.blocks
        }
        if abstract_block is not None:
            current_block_specs[abstract_block.id] = (
                abstract_block.block_type,
                abstract_block.content_hash,
            )
        block_scoped_tasks = session.scalars(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.paper_id == paper.id,
                ProcessingTaskModel.task_type.in_(
                    {
                        TRANSLATE_BLOCK,
                        RENDER_TABLE,
                        RENDER_FIGURE,
                        RENDER_LATEX_BLOCK,
                        RENDER_LATEX_FORMULA,
                        REPAIR_RENDER,
                    }
                ),
                ProcessingTaskModel.status != TaskStatus.CANCELLED,
            )
        ).all()
        for block_task in block_scoped_tasks:
            payload = block_task.payload_json if isinstance(block_task.payload_json, dict) else {}
            former_block_id = payload.get("block_id")
            current_spec = (
                current_block_specs.get(former_block_id)
                if isinstance(former_block_id, str)
                else None
            )
            expected_block_type = {
                RENDER_TABLE: "TABLE",
                RENDER_FIGURE: "FIGURE",
            }.get(block_task.task_type)
            task_content_hash = payload.get("content_hash")
            content_hash_still_matches = (
                block_task.task_type == REPAIR_RENDER
                or not isinstance(task_content_hash, str)
                or (current_spec is not None and task_content_hash == current_spec[1])
            )
            task_still_matches = (
                current_spec is not None
                and (expected_block_type is None or current_spec[0] == expected_block_type)
                and content_hash_still_matches
            )
            if task_still_matches:
                continue
            current_result = (
                block_task.result_json if isinstance(block_task.result_json, dict) else {}
            )
            block_task.status = TaskStatus.CANCELLED
            block_task.result_json = {
                **current_result,
                "superseded": True,
                "superseded_reason": (
                    "A parser rerun removed, merged, or changed the task's former content block."
                ),
            }
            block_task.lease_expires_at = None
            block_task.worker_id = None
            block_task.finished_at = block_task.finished_at or now_utc()
        removed_ids = {
            block_id
            for block_id, block in existing_blocks.items()
            if block.block_type != "ABSTRACT"
        } - set(block_ids.values())
        if removed_ids:
            session.execute(delete(ContentBlockModel).where(ContentBlockModel.id.in_(removed_ids)))
        session.flush()
        translations = list(
            session.scalars(
                select(TranslationModel)
                .join(ContentBlockModel, TranslationModel.block_id == ContentBlockModel.id)
                .where(
                    ContentBlockModel.paper_id == paper.id,
                    TranslationModel.status == TranslationStatus.SUCCEEDED,
                    TranslationModel.prompt_kind == "global",
                )
            ).all()
        )
        normalized_translation_payloads = normalize_render_payload_with_preamble(
            [translation.translated_payload_json for translation in translations],
            document.renderer_preamble,
        )
        for translation, normalized_payload in zip(
            translations,
            normalized_translation_payloads,
            strict=True,
        ):
            translation.translated_payload_json = normalized_payload
        _reconcile_deterministic_formula_tasks_in_session(
            session,
            paper_id=paper.id,
            reconciliation_key="reconciled_on_parse",
        )
        session.flush()
        failed_count = session.scalar(
            select(func.count())
            .select_from(ProcessingTaskModel)
            .where(
                ProcessingTaskModel.paper_id == paper.id,
                ProcessingTaskModel.status == TaskStatus.FAILED,
            )
        )
        paper.processing_stage = ProcessingStage.READING_READY
        paper.processing_health = ProcessingHealth.PARTIAL if failed_count else ProcessingHealth.OK
        session.commit()
    return {
        "root_file": document.root_file,
        "block_count": len(document.blocks),
        "warnings": document.warnings,
        "parser_version": document.parser_version,
        "project_index_key": project_index_key,
        "project_ir_version": (document.project_index or {}).get("version"),
        "project_file_count": len((document.project_index or {}).get("files", [])),
        "declared_package_count": len((document.project_index or {}).get("packages", [])),
        "declared_macro_count": len((document.project_index or {}).get("macros", [])),
        "complex_table_count": len(complex_tables),
        "table_render_count": len(renderable_tables),
        "figure_render_count": len(renderable_figures),
        "latex_block_render_count": len(renderable_latex_blocks),
        "pdf_document_count": len(pdf_document_states),
        "bibliography_status": bibliography_result.status,
        "bibliography_entry_count": len(bibliography_result.entries),
        "bibliography_style": bibliography_result.resolved_style,
        "citation_occurrence_count": len(pending_citation_occurrences),
    }


def _preserve_successful_formula_renders(
    *,
    block_type: str,
    parsed_payload: dict[str, Any],
    stored_payload: object,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Keep verified immutable TeX assets across a source-identical reparse.

    The caller has already proved that the block content hash is unchanged. We
    additionally require the exact structural path and LaTeX value to match, so
    a parser change can never attach an old image to a changed formula.
    """

    if not isinstance(stored_payload, dict):
        return parsed_payload
    preserved = deepcopy(parsed_payload)

    def visit(parsed: object, stored: object, *, root: bool = False) -> None:
        if isinstance(parsed, list):
            if not isinstance(stored, list):
                return
            for index, item in enumerate(parsed):
                if index < len(stored):
                    visit(item, stored[index])
            return
        if not isinstance(parsed, dict) or not isinstance(stored, dict):
            return
        is_formula = parsed.get("type") in {"math", "equation"} or (
            root and block_type == "EQUATION"
        )
        stored_render = stored.get("latex_render")
        if (
            is_formula
            and isinstance(parsed.get("latex"), str)
            and parsed.get("latex") == stored.get("latex")
            and isinstance(stored_render, dict)
            and stored_render.get("status") == "succeeded"
            and (
                toolchain_fingerprint is None
                or _toolchain_identity_matches(
                    stored_render,
                    renderer_version=LATEX_FORMULA_RENDERER_VERSION,
                    fingerprint=toolchain_fingerprint,
                )
            )
            and isinstance(stored_render.get("asset"), dict)
        ):
            parsed["latex_render"] = deepcopy(stored_render)
        for key, item in parsed.items():
            if key != "latex_render" and key in stored:
                visit(item, stored[key])

    visit(preserved, stored_payload, root=True)
    return preserved


def _preserve_applied_render_repair(
    *,
    block_type: str,
    parsed_payload: dict[str, Any],
    stored_payload: object,
) -> dict[str, Any]:
    """Keep a valid derived repair when an unchanged source block is reparsed."""
    if not isinstance(stored_payload, dict):
        return parsed_payload
    ai_repair = stored_payload.get("ai_repair")
    if not isinstance(ai_repair, dict) or ai_repair.get("model_assisted") is not True:
        return parsed_payload

    preserved = dict(parsed_payload)
    applied = False
    if block_type == "EQUATION":
        corrected_latex = stored_payload.get("latex")
        original_latex = stored_payload.get("original_latex")
        if (
            isinstance(corrected_latex, str)
            and corrected_latex.strip()
            and isinstance(original_latex, str)
            and original_latex.strip()
        ):
            preserved["latex"] = corrected_latex
            preserved["original_latex"] = original_latex
            applied = True
    elif block_type == "TABLE":
        table_render = stored_payload.get("table_render")
        rows = stored_payload.get("rows")
        if (
            isinstance(table_render, dict)
            and table_render.get("source") == "ai-repaired-latex"
            and isinstance(table_render.get("corrected_latex"), str)
        ):
            if isinstance(rows, list):
                preserved["rows"] = rows
                preserved["structured"] = bool(stored_payload.get("structured", True))
            preserved["table_render"] = dict(table_render)
            applied = True
    elif block_type == "FIGURE":
        assets = stored_payload.get("assets")
        if isinstance(assets, list) and assets:
            preserved["assets"] = assets
            missing_assets = stored_payload.get("missing_assets")
            preserved["missing_assets"] = missing_assets if isinstance(missing_assets, list) else []
            applied = True
        else:
            figure_render = stored_payload.get("figure_render")
            if (
                isinstance(figure_render, dict)
                and figure_render.get("source") == "ai-repaired-latex"
                and isinstance(figure_render.get("corrected_latex"), str)
            ):
                preserved["figure_render"] = dict(figure_render)
                applied = True
    else:
        repaired_text = stored_payload.get("text")
        if isinstance(repaired_text, str) and repaired_text.strip():
            preserved["text"] = repaired_text
            applied = True

    if not applied:
        return parsed_payload
    preserved["ai_repair"] = dict(ai_repair)
    return preserved


def _replacement_render_owner(task: QueuedTask) -> tuple[str, str] | None:
    """Return the owner of an AI-repaired replacement or fail closed on bad scope."""

    if not task.payload.get("repair_task_id"):
        return None
    return _require_repair_task_scope(task)


def _render_payload_for_task(
    session: Session,
    *,
    task: QueuedTask,
    block: ContentBlockModel,
    owner: tuple[str, str] | None,
) -> dict[str, Any]:
    if owner is None:
        return (
            dict(block.render_payload_json)
            if isinstance(block.render_payload_json, dict)
            else {}
        )
    workspace_id, requested_by_user_id = owner
    if task.paper_id is None or not _repair_task_has_library_access(
        session,
        paper_id=task.paper_id,
        workspace_id=workspace_id,
        requested_by_user_id=requested_by_user_id,
    ):
        raise TaskAuthorizationError(
            "The replacement render paper is outside the task workspace."
        )
    return effective_block_render(
        session,
        workspace_id=workspace_id,
        block=block,
    ).payload


def _save_render_payload_for_task(
    session: Session,
    *,
    task: QueuedTask,
    block: ContentBlockModel,
    owner: tuple[str, str] | None,
    render_payload: dict[str, Any],
    render_kind: str,
    status: str,
) -> None:
    if owner is None:
        block.render_payload_json = render_payload
        return
    workspace_id, requested_by_user_id = owner
    if task.paper_id is None or not _repair_task_has_library_access(
        session,
        paper_id=task.paper_id,
        workspace_id=workspace_id,
        requested_by_user_id=requested_by_user_id,
    ):
        raise TaskAuthorizationError(
            "The replacement render paper left the task workspace."
        )
    current = effective_block_render(
        session,
        workspace_id=workspace_id,
        block=block,
    )
    metadata = (
        dict(current.overlay.metadata_json)
        if current.overlay is not None and isinstance(current.overlay.metadata_json, dict)
        else {}
    )
    metadata.update(
        {
            "source": "workspace-derived-render",
            "last_task_id": task.id,
            "render_kind": render_kind,
            "status": status,
        }
    )
    save_workspace_render_overlay(
        session,
        workspace_id=workspace_id,
        requested_by_user_id=requested_by_user_id,
        block=block,
        render_payload=render_payload,
        metadata=metadata,
    )


def render_table(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    storage: Storage,
    renderer: LatexTableRenderer | None,
    *,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    _require_toolchain_task_identity(
        task,
        renderer_version=TABLE_RENDERER_VERSION,
        fingerprint=toolchain_fingerprint,
    )
    if task.paper_id is None or renderer is None:
        raise TaskHandlerError("Table rendering requires a paper and local LaTeX renderer.")
    block_id = str(task.payload.get("block_id", ""))
    expected_hash = str(task.payload.get("content_hash", ""))
    preamble_key = _validated_task_preamble_key(
        paper_id=task.paper_id,
        preamble_key=str(task.payload.get("preamble_key", "")),
    )
    repaired_source_latex = task.payload.get("source_latex")
    replacement_owner = _replacement_render_owner(task)
    with session_factory() as session:
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
                ContentBlockModel.block_type == "TABLE",
            )
        )
        if block is None or block.content_hash != expected_hash:
            raise TaskHandlerError("The table render target changed or no longer exists.")
        _render_payload_for_task(
            session,
            task=task,
            block=block,
            owner=replacement_owner,
        )
        canonical_source_latex = str(block.source_payload_json.get("latex", ""))
        source_latex = (
            repaired_source_latex
            if isinstance(repaired_source_latex, str) and repaired_source_latex.strip()
            else canonical_source_latex
        )
        source_record = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == task.paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
        manifest_key = source_record.storage_key if source_record is not None else ""
        source_locator = (
            block.source_locator_json if isinstance(block.source_locator_json, dict) else {}
        )
        root_file = str(source_locator.get("root_file", "main.tex"))
    preamble = ""
    if preamble_key and storage.exists(preamble_key):
        with storage.get(preamble_key) as stream:
            preamble = stream.read(100_001).decode("utf-8", errors="replace")
    resolved_assets: list[dict[str, Any]] = []
    missing_assets: list[str] = []
    loaded_manifest = (
        _load_task_source_manifest(
            storage,
            paper_id=task.paper_id,
            manifest_key=manifest_key,
            required=False,
        )
        if manifest_key
        else None
    )
    manifest: dict[str, Any] = loaded_manifest or {"files": []}
    asset_content = _latex_support_assets(
        manifest,
        storage,
        root_file=root_file,
        source_latex=source_latex,
        max_asset_count=TABLE_RENDER_MAX_ASSET_COUNT,
        max_asset_bytes=TABLE_RENDER_MAX_ASSET_BYTES,
    )
    try:
        render_source_latex = source_latex
        if re.search(r"\\includegraphics\b", source_latex):
            if loaded_manifest is None:
                raise TableRenderError(
                    "The table references an image, but its source manifest is unavailable.",
                    repair_eligible=False,
                    repair_ineligible_reason=(
                        "The source manifest must be recovered before this table can render; "
                        "AI repair cannot recreate a missing runtime asset."
                    ),
                )
            render_source_latex, resolved_assets, missing_assets = prepare_includegraphics_assets(
                source_latex,
                root_file=root_file,
                manifest=manifest,
                preamble=preamble,
            )
            if missing_assets:
                raise TableRenderError(
                    "The table references image assets missing from the source manifest: "
                    + ", ".join(missing_assets[:8]),
                    repair_eligible=False,
                    repair_ineligible_reason=(
                        "Referenced image assets must be resolved from the canonical source "
                        "manifest; AI repair must not delete or invent them."
                    ),
                )
            if len(resolved_assets) + len(asset_content) > TABLE_RENDER_MAX_ASSET_COUNT:
                raise TableRenderError(
                    "The table references too many image assets.",
                    repair_eligible=False,
                    repair_ineligible_reason=(
                        "The deterministic image-asset safety limit was exceeded."
                    ),
                )
            remaining_asset_bytes = TABLE_RENDER_MAX_ASSET_BYTES - sum(
                len(content) for content in asset_content.values()
            )
            for item in resolved_assets:
                asset_path = str(item.get("path", ""))
                storage_key = str(item.get("storage_key", ""))
                if not asset_path or not storage_key or not storage.exists(storage_key):
                    unavailable_asset = asset_path or "unknown"
                    raise TableRenderError(
                        f"The manifest-backed table image is unavailable: {unavailable_asset}",
                        repair_eligible=False,
                        repair_ineligible_reason=(
                            "The manifest-backed image must be restored in storage before "
                            "rendering; AI repair cannot recreate it."
                        ),
                    )
                with storage.get(storage_key) as stream:
                    content = stream.read(remaining_asset_bytes + 1)
                if len(content) > remaining_asset_bytes:
                    raise TableRenderError(
                        "The table image assets exceed the render size limit.",
                        repair_eligible=False,
                        repair_ineligible_reason=(
                            "The deterministic image-asset safety limit was exceeded."
                        ),
                    )
                asset_content[asset_path] = content
                remaining_asset_bytes -= len(content)
        artifact = renderer.render(
            source_latex=render_source_latex,
            preamble=preamble,
            assets=asset_content,
        )
    except TableRenderError as error:
        with session_factory() as session:
            block = session.get(ContentBlockModel, block_id)
            if block is not None and block.content_hash == expected_hash:
                payload = _render_payload_for_task(
                    session,
                    task=task,
                    block=block,
                    owner=replacement_owner,
                )
                if resolved_assets:
                    payload["assets"] = [
                        {
                            "path": item["path"],
                            "mime_type": item.get("mime_type", "application/octet-stream"),
                            "byte_size": item.get("byte_size"),
                        }
                        for item in resolved_assets
                    ]
                payload["missing_assets"] = missing_assets
                table_render = dict(payload.get("table_render", {}))
                repair_eligible = (
                    len(source_latex) <= RENDER_REPAIR_MAX_LATEX_CHARS and error.repair_eligible
                )
                repair_ineligible_reason = error.repair_ineligible_reason
                if len(source_latex) > RENDER_REPAIR_MAX_LATEX_CHARS:
                    repair_ineligible_reason = "This table is too large for a lossless AI repair."
                table_render.update(
                    {
                        "status": "failed",
                        "error": str(error)[:2000],
                        "error_code": error.error_code,
                        "attempts": list(error.attempts),
                        **_toolchain_provenance(toolchain_fingerprint),
                        "repair_eligible": repair_eligible,
                        **(
                            {}
                            if repair_eligible
                            else {
                                "repair_ineligible_reason": (
                                    repair_ineligible_reason
                                    or "This failure is not eligible for AI repair."
                                )
                            }
                        ),
                    }
                )
                if repair_eligible:
                    table_render.pop("repair_ineligible_reason", None)
                payload["table_render"] = table_render
                _save_render_payload_for_task(
                    session,
                    task=task,
                    block=block,
                    owner=replacement_owner,
                    render_payload=payload,
                    render_kind="table",
                    status="failed",
                )
                if replacement_owner is None:
                    paper = session.get(PaperModel, task.paper_id)
                    if paper is not None:
                        paper.processing_health = ProcessingHealth.PARTIAL
                session.commit()
        raise

    artifact_hash = hashlib.sha256(artifact.content).hexdigest()
    asset_name = f"{block_id}-{expected_hash[:16]}-{artifact_hash[:16]}.{artifact.extension}"
    storage_key = (
        workspace_render_asset_key(
            workspace_id=replacement_owner[0],
            paper_id=task.paper_id,
            asset_directory="tables",
            asset_name=asset_name,
        )
        if replacement_owner is not None
        else f"papers/{task.paper_id}/derived/tables/{asset_name}"
    )
    storage.put(storage_key, io.BytesIO(artifact.content))
    with session_factory() as session:
        block = session.get(ContentBlockModel, block_id)
        if block is None or block.content_hash != expected_hash:
            storage.delete(storage_key)
            raise TaskHandlerError("The table render target changed before it could be saved.")
        payload = _render_payload_for_task(
            session,
            task=task,
            block=block,
            owner=replacement_owner,
        )
        if resolved_assets:
            payload["assets"] = [
                {
                    "path": item["path"],
                    "mime_type": item.get("mime_type", "application/octet-stream"),
                    "byte_size": item.get("byte_size"),
                }
                for item in resolved_assets
            ]
        payload["missing_assets"] = []
        table_render = dict(payload.get("table_render", {}))
        table_render.update(
            {
                "status": "succeeded",
                "asset": {
                    "path": asset_name,
                    "mime_type": artifact.mime_type,
                    "byte_size": len(artifact.content),
                    **({"scope": "workspace"} if replacement_owner is not None else {}),
                },
                "compiler": artifact.compiler,
                "renderer_version": TABLE_RENDERER_VERSION,
                **_toolchain_provenance(toolchain_fingerprint),
                "render_context": artifact.render_context,
                "source": (
                    "ai-repaired-latex" if task.payload.get("repair_task_id") else "canonical-latex"
                ),
                **(
                    {"repair_task_id": str(task.payload["repair_task_id"])}
                    if task.payload.get("repair_task_id")
                    else {}
                ),
            }
        )
        table_render.pop("error", None)
        table_render.pop("error_code", None)
        table_render.pop("attempts", None)
        table_render.pop("repair_eligible", None)
        table_render.pop("repair_ineligible_reason", None)
        payload["table_render"] = table_render
        _save_render_payload_for_task(
            session,
            task=task,
            block=block,
            owner=replacement_owner,
            render_payload=payload,
            render_kind="table",
            status="succeeded",
        )
        stale_repair_statement = select(ProcessingTaskModel).where(
            ProcessingTaskModel.paper_id == task.paper_id,
            ProcessingTaskModel.task_type == REPAIR_RENDER,
            ProcessingTaskModel.payload_json["block_id"].as_string() == block_id,
            ProcessingTaskModel.payload_json["content_hash"].as_string() == expected_hash,
            ProcessingTaskModel.status.in_([TaskStatus.FAILED, TaskStatus.QUEUED]),
        )
        if replacement_owner is not None:
            stale_repair_statement = stale_repair_statement.where(
                ProcessingTaskModel.scope_key == task.scope_key
            )
        stale_repairs = session.scalars(stale_repair_statement).all()
        for stale_repair in stale_repairs:
            current_result = (
                stale_repair.result_json if isinstance(stale_repair.result_json, dict) else {}
            )
            stale_repair.status = TaskStatus.CANCELLED
            stale_repair.result_json = {
                **current_result,
                "superseded": True,
                "superseded_by_renderer_version": TABLE_RENDERER_VERSION,
                "superseded_reason": (
                    "The upgraded deterministic renderer produced the table successfully."
                ),
            }
            stale_repair.lease_expires_at = None
            stale_repair.worker_id = None
            stale_repair.finished_at = stale_repair.finished_at or now_utc()
        if replacement_owner is None:
            failed_count = session.scalar(
                select(func.count())
                .select_from(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == task.paper_id,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                )
            )
            paper = session.get(PaperModel, task.paper_id)
            if paper is not None and not failed_count:
                paper.processing_health = ProcessingHealth.OK
        session.commit()
    return {
        "block_id": block_id,
        "asset_path": asset_name,
        "byte_size": len(artifact.content),
        "renderer_version": TABLE_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
    }


def render_figure(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    storage: Storage,
    renderer: LatexFigureRenderer | None,
    *,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    _require_toolchain_task_identity(
        task,
        renderer_version=FIGURE_RENDERER_VERSION,
        fingerprint=toolchain_fingerprint,
    )
    if task.paper_id is None or renderer is None:
        raise TaskHandlerError("Figure rendering requires a paper and local LaTeX renderer.")
    block_id = str(task.payload.get("block_id", ""))
    expected_hash = str(task.payload.get("content_hash", ""))
    preamble_key = _validated_task_preamble_key(
        paper_id=task.paper_id,
        preamble_key=str(task.payload.get("preamble_key", "")),
    )
    repaired_source_latex = task.payload.get("source_latex")
    replacement_owner = _replacement_render_owner(task)
    with session_factory() as session:
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
                ContentBlockModel.block_type == "FIGURE",
            )
        )
        if block is None or block.content_hash != expected_hash:
            raise TaskHandlerError("The figure render target changed or no longer exists.")
        effective_payload = _render_payload_for_task(
            session,
            task=task,
            block=block,
            owner=replacement_owner,
        )
        canonical_source_latex = str(block.source_payload_json.get("latex", ""))
        source_latex = (
            repaired_source_latex
            if isinstance(repaired_source_latex, str) and repaired_source_latex.strip()
            else canonical_source_latex
        )
        if not figure_requires_latex_render(
            source_latex,
            resolved_assets=effective_payload.get("assets"),
        ):
            raise TaskHandlerError("The figure is not eligible for local LaTeX rendering.")
        source_record = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == task.paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
        manifest_key = source_record.storage_key if source_record is not None else ""
        source_locator = (
            block.source_locator_json if isinstance(block.source_locator_json, dict) else {}
        )
        root_file = str(source_locator.get("root_file", "main.tex"))
    preamble = ""
    if preamble_key and storage.exists(preamble_key):
        with storage.get(preamble_key) as stream:
            preamble = stream.read(250_001).decode("utf-8", errors="replace")
    resolved_assets: list[dict[str, Any]] = []
    missing_assets: list[str] = []
    loaded_manifest = (
        _load_task_source_manifest(
            storage,
            paper_id=task.paper_id,
            manifest_key=manifest_key,
            required=False,
        )
        if manifest_key
        else None
    )
    manifest: dict[str, Any] = loaded_manifest or {"files": []}
    parameterized_graphics = (
        re.search(
            r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{[^{}]*#[1-9][^{}]*\}",
            source_latex,
        )
        is not None
    )
    asset_content = _latex_support_assets(
        manifest,
        storage,
        root_file=root_file,
        source_latex=source_latex,
        max_asset_count=FIGURE_RENDER_MAX_ASSET_COUNT,
        max_asset_bytes=FIGURE_RENDER_MAX_ASSET_BYTES,
        include_graphics=parameterized_graphics,
    )
    try:
        render_source_latex = source_latex
        if re.search(r"\\includegraphics\b", source_latex) and not parameterized_graphics:
            if loaded_manifest is None:
                raise FigureRenderError(
                    "The figure references an image, but its source manifest is unavailable."
                )
            render_source_latex, resolved_assets, missing_assets = prepare_includegraphics_assets(
                source_latex,
                root_file=root_file,
                manifest=manifest,
                preamble=preamble,
            )
            if missing_assets:
                raise FigureRenderError(
                    "The figure references image assets missing from the source manifest: "
                    + ", ".join(missing_assets[:8])
                )
            if len(resolved_assets) + len(asset_content) > FIGURE_RENDER_MAX_ASSET_COUNT:
                raise FigureRenderError("The figure references too many source assets.")
            remaining_asset_bytes = FIGURE_RENDER_MAX_ASSET_BYTES - sum(
                len(content) for content in asset_content.values()
            )
            for item in resolved_assets:
                asset_path = str(item.get("path", ""))
                storage_key = str(item.get("storage_key", ""))
                if not asset_path or not storage_key or not storage.exists(storage_key):
                    unavailable_asset = asset_path or "unknown"
                    raise FigureRenderError(
                        f"The manifest-backed figure asset is unavailable: {unavailable_asset}"
                    )
                with storage.get(storage_key) as stream:
                    content = stream.read(remaining_asset_bytes + 1)
                if len(content) > remaining_asset_bytes:
                    raise FigureRenderError(
                        "The figure source assets exceed the render size limit."
                    )
                asset_content[asset_path] = content
                remaining_asset_bytes -= len(content)
        artifact = renderer.render(
            source_latex=render_source_latex,
            preamble=preamble,
            assets=asset_content,
        )
    except FigureRenderError as error:
        with session_factory() as session:
            block = session.get(ContentBlockModel, block_id)
            if block is not None and block.content_hash == expected_hash:
                payload = _render_payload_for_task(
                    session,
                    task=task,
                    block=block,
                    owner=replacement_owner,
                )
                if resolved_assets:
                    payload["assets"] = [
                        {
                            "path": item["path"],
                            "mime_type": item.get("mime_type", "application/octet-stream"),
                            "byte_size": item.get("byte_size"),
                        }
                        for item in resolved_assets
                    ]
                payload["missing_assets"] = missing_assets
                figure_render = dict(payload.get("figure_render", {}))
                figure_render.update(
                    {
                        "status": "failed",
                        "error": str(error)[:2000],
                        "error_code": error.error_code,
                        "attempts": list(error.attempts),
                        **_toolchain_provenance(toolchain_fingerprint),
                        "repair_eligible": error.repair_eligible,
                        **(
                            {}
                            if error.repair_eligible
                            else {
                                "repair_ineligible_reason": (
                                    error.repair_ineligible_reason
                                    or "The deterministic figure context must be repaired first."
                                )
                            }
                        ),
                    }
                )
                payload["figure_render"] = figure_render
                _save_render_payload_for_task(
                    session,
                    task=task,
                    block=block,
                    owner=replacement_owner,
                    render_payload=payload,
                    render_kind="figure",
                    status="failed",
                )
                if replacement_owner is None:
                    paper = session.get(PaperModel, task.paper_id)
                    if paper is not None:
                        paper.processing_health = ProcessingHealth.PARTIAL
                session.commit()
        raise

    artifact_hash = hashlib.sha256(artifact.content).hexdigest()
    asset_name = f"{block_id}-{expected_hash[:16]}-{artifact_hash[:16]}.{artifact.extension}"
    storage_key = (
        workspace_render_asset_key(
            workspace_id=replacement_owner[0],
            paper_id=task.paper_id,
            asset_directory="figures",
            asset_name=asset_name,
        )
        if replacement_owner is not None
        else f"papers/{task.paper_id}/derived/figures/{asset_name}"
    )
    storage.put(storage_key, io.BytesIO(artifact.content))
    with session_factory() as session:
        block = session.get(ContentBlockModel, block_id)
        if block is None or block.content_hash != expected_hash:
            storage.delete(storage_key)
            raise TaskHandlerError("The figure render target changed before it could be saved.")
        payload = _render_payload_for_task(
            session,
            task=task,
            block=block,
            owner=replacement_owner,
        )
        if resolved_assets:
            payload["assets"] = [
                {
                    "path": item["path"],
                    "mime_type": item.get("mime_type", "application/octet-stream"),
                    "byte_size": item.get("byte_size"),
                }
                for item in resolved_assets
            ]
        payload["missing_assets"] = []
        figure_render = dict(payload.get("figure_render", {}))
        figure_render.update(
            {
                "status": "succeeded",
                "asset": {
                    "path": asset_name,
                    "mime_type": artifact.mime_type,
                    "byte_size": len(artifact.content),
                    **({"scope": "workspace"} if replacement_owner is not None else {}),
                },
                "compiler": artifact.compiler,
                "renderer_version": FIGURE_RENDERER_VERSION,
                **_toolchain_provenance(toolchain_fingerprint),
                "render_context": artifact.render_context,
                "source": (
                    "ai-repaired-latex"
                    if isinstance(repaired_source_latex, str) and repaired_source_latex.strip()
                    else "canonical-latex"
                ),
            }
        )
        figure_render.pop("error", None)
        figure_render.pop("error_code", None)
        figure_render.pop("attempts", None)
        figure_render.pop("repair_eligible", None)
        figure_render.pop("repair_ineligible_reason", None)
        payload["figure_render"] = figure_render
        _save_render_payload_for_task(
            session,
            task=task,
            block=block,
            owner=replacement_owner,
            render_payload=payload,
            render_kind="figure",
            status="succeeded",
        )
        if replacement_owner is None:
            failed_count = session.scalar(
                select(func.count())
                .select_from(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == task.paper_id,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                )
            )
            paper = session.get(PaperModel, task.paper_id)
            if paper is not None and not failed_count:
                paper.processing_health = ProcessingHealth.OK
        session.commit()
    return {
        "block_id": block_id,
        "asset_path": asset_name,
        "byte_size": len(artifact.content),
        "renderer_version": FIGURE_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
    }


def render_latex_block(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    storage: Storage,
    renderer: LatexTableRenderer | None,
    *,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Render one layout-sensitive block without interpreting or rewriting its TeX program."""

    _require_toolchain_task_identity(
        task,
        renderer_version=LATEX_BLOCK_RENDERER_VERSION,
        fingerprint=toolchain_fingerprint,
    )
    if task.paper_id is None or renderer is None:
        raise TaskHandlerError(
            "Native LaTeX block rendering requires a paper and local LaTeX renderer."
        )
    block_id = str(task.payload.get("block_id", ""))
    expected_hash = str(task.payload.get("content_hash", ""))
    preamble_key = _validated_task_preamble_key(
        paper_id=task.paper_id,
        preamble_key=str(task.payload.get("preamble_key", "")),
    )
    requested_kind = str(task.payload.get("render_kind", ""))
    with session_factory() as session:
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
                ContentBlockModel.block_type.in_({"ALGORITHM", "LATEX_BLOCK"}),
            )
        )
        if block is None or block.content_hash != expected_hash:
            raise TaskHandlerError("The native LaTeX block target changed or no longer exists.")
        render_kind = "algorithm" if block.block_type == "ALGORITHM" else "visual"
        if requested_kind and requested_kind != render_kind:
            raise TaskHandlerError("The native LaTeX block render strategy no longer matches.")
        source_latex = str(block.source_payload_json.get("latex", ""))
        if not source_latex.strip():
            raise TaskHandlerError("The native LaTeX block has no canonical source.")
        source_record = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == task.paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
        manifest_key = source_record.storage_key if source_record is not None else ""
        source_locator = (
            block.source_locator_json if isinstance(block.source_locator_json, dict) else {}
        )
        root_file = str(source_locator.get("root_file", "main.tex"))
    preamble = ""
    if preamble_key and storage.exists(preamble_key):
        with storage.get(preamble_key) as stream:
            preamble = stream.read(250_001).decode("utf-8", errors="replace")
    loaded_manifest = (
        _load_task_source_manifest(
            storage,
            paper_id=task.paper_id,
            manifest_key=manifest_key,
            required=False,
        )
        if manifest_key
        else None
    )
    manifest: dict[str, Any] = loaded_manifest or {"files": []}
    asset_content = _latex_support_assets(
        manifest,
        storage,
        root_file=root_file,
        source_latex=source_latex,
        max_asset_count=TABLE_RENDER_MAX_ASSET_COUNT,
        max_asset_bytes=TABLE_RENDER_MAX_ASSET_BYTES,
        include_graphics=re.search(r"\\includegraphics\b", source_latex) is not None,
    )
    try:
        artifact = renderer.render(
            source_latex=source_latex,
            preamble=preamble,
            assets=asset_content,
            content_kind=render_kind,
        )
    except TableRenderError as error:
        with session_factory() as session:
            block = session.get(ContentBlockModel, block_id)
            if block is not None and block.content_hash == expected_hash:
                payload = dict(block.render_payload_json)
                render_state = dict(payload.get("latex_block_render", {}))
                render_state.update(
                    {
                        "status": "failed",
                        "error": str(error)[:2000],
                        "error_code": error.error_code,
                        "attempts": list(error.attempts),
                        **_toolchain_provenance(toolchain_fingerprint),
                        # Canonical source must remain untouched. A later explicit AI workflow
                        # may be offered, but deterministic rendering never auto-repairs it.
                        "repair_eligible": False,
                        "repair_ineligible_reason": (
                            error.repair_ineligible_reason
                            or "The canonical visual block was preserved for deterministic review."
                        ),
                    }
                )
                payload["latex_block_render"] = render_state
                block.render_payload_json = payload
                paper = session.get(PaperModel, task.paper_id)
                if paper is not None:
                    paper.processing_health = ProcessingHealth.PARTIAL
                session.commit()
        raise

    artifact_hash = hashlib.sha256(artifact.content).hexdigest()
    asset_name = f"{block_id}-{expected_hash[:16]}-{artifact_hash[:16]}.{artifact.extension}"
    storage_key = f"papers/{task.paper_id}/derived/latex-blocks/{asset_name}"
    storage.put(storage_key, io.BytesIO(artifact.content))
    with session_factory() as session:
        block = session.get(ContentBlockModel, block_id)
        if block is None or block.content_hash != expected_hash:
            storage.delete(storage_key)
            raise TaskHandlerError(
                "The native LaTeX block target changed before it could be saved."
            )
        payload = dict(block.render_payload_json)
        render_state = dict(payload.get("latex_block_render", {}))
        render_state.update(
            {
                "status": "succeeded",
                "render_kind": render_kind,
                "asset": {
                    "path": asset_name,
                    "mime_type": artifact.mime_type,
                    "byte_size": len(artifact.content),
                },
                "compiler": artifact.compiler,
                "renderer_version": LATEX_BLOCK_RENDERER_VERSION,
                **_toolchain_provenance(toolchain_fingerprint),
                "render_context": artifact.render_context,
                "source": "canonical-latex",
            }
        )
        for key in (
            "error",
            "error_code",
            "attempts",
            "repair_eligible",
            "repair_ineligible_reason",
        ):
            render_state.pop(key, None)
        payload["latex_block_render"] = render_state
        block.render_payload_json = payload
        failed_count = session.scalar(
            select(func.count())
            .select_from(ProcessingTaskModel)
            .where(
                ProcessingTaskModel.paper_id == task.paper_id,
                ProcessingTaskModel.status == TaskStatus.FAILED,
            )
        )
        paper = session.get(PaperModel, task.paper_id)
        if paper is not None and not failed_count:
            paper.processing_health = ProcessingHealth.OK
        session.commit()
    return {
        "block_id": block_id,
        "asset_path": asset_name,
        "byte_size": len(artifact.content),
        "renderer_version": LATEX_BLOCK_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
        "render_kind": render_kind,
    }


def _latex_support_assets(
    manifest: dict[str, Any],
    storage: Storage,
    *,
    root_file: str,
    source_latex: str = "",
    max_asset_count: int = FIGURE_RENDER_MAX_ASSET_COUNT,
    max_asset_bytes: int = FIGURE_RENDER_MAX_ASSET_BYTES,
    include_graphics: bool = False,
) -> dict[str, bytes]:
    """Load bounded paper-local TeX dependencies for any isolated block compile.

    Resolve the paper's local class/package/input graph before using the remaining
    budget for incidental assets.  Sorting only by literal filename occurrence is
    insufficient because TeX normally omits ``.cls``/``.sty`` at the call site and
    because a class can load a companion package transitively (REVTeX loading
    ``revsymb4-2`` is a representative case).  The closure is still manifest-only,
    byte/count bounded, and never follows an arbitrary filesystem path.
    """

    assets: dict[str, bytes] = {}
    remaining = max_asset_bytes
    supported_suffixes = {
        # Paper-local TeX implementation and class companions.
        ".sty",
        ".cls",
        ".clo",
        ".def",
        ".rtx",
        ".ltx",
        ".tex",
        ".fd",
        ".cfg",
        # Data consumed directly by pgfplots/pgfplotstable and similar packages.
        ".csv",
        ".dat",
        ".tsv",
        ".txt",
        # Paper-local font resources used by legacy and Unicode engines.
        ".tfm",
        ".pfb",
        ".map",
        ".enc",
        ".ttf",
        ".otf",
    }
    if include_graphics:
        supported_suffixes.update({".png", ".jpg", ".jpeg", ".pdf", ".eps", ".ps"})
    candidates = [
        item
        for item in manifest.get("files", [])
        if isinstance(item, dict)
        and PurePosixPath(str(item.get("path", ""))).suffix.lower() in supported_suffixes
        and str(item.get("storage_key", ""))
    ]
    root_parent = PurePosixPath(root_file).parent

    candidates_by_path = {
        PurePosixPath(str(item.get("path", ""))).as_posix(): item for item in candidates
    }
    candidates_by_name: dict[str, list[str]] = {}
    for path in candidates_by_path:
        candidates_by_name.setdefault(PurePosixPath(path).name, []).append(path)

    def without_comments(value: str) -> str:
        output: list[str] = []
        for line in value.splitlines(keepends=True):
            comment_at: int | None = None
            for index, character in enumerate(line):
                if character != "%":
                    continue
                slash_count = 0
                before = index - 1
                while before >= 0 and line[before] == "\\":
                    slash_count += 1
                    before -= 1
                if slash_count % 2 == 0:
                    comment_at = index
                    break
            if comment_at is None:
                output.append(line)
            elif line.endswith("\n"):
                output.append(line[:comment_at] + "\n")
            else:
                output.append(line[:comment_at])
        return "".join(output)

    reference_pattern = re.compile(
        r"\\(?P<command>documentclass|LoadClass(?:WithOptions)?|"
        r"usepackage|RequirePackage(?:WithOptions)?|input|include|@input|"
        r"InputIfFileExists|IfFileExists)"
        r"\*?\s*(?:\[[^\]]*\]\s*)?\{(?P<targets>[^{}]+)\}",
        re.IGNORECASE,
    )
    unbraced_input_pattern = re.compile(
        r"\\(?P<command>input|include)\s+(?P<target>[^\s%{}]+)",
        re.IGNORECASE,
    )

    def command_is_escaped(value: str, start: int) -> bool:
        slash_count = 0
        cursor = start - 1
        while cursor >= 0 and value[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        return slash_count % 2 == 1

    def command_suffixes(command: str) -> tuple[str, ...]:
        lowered = command.casefold()
        if lowered in {"documentclass", "loadclass", "loadclasswithoptions"}:
            return (".cls",)
        if lowered in {"usepackage", "requirepackage", "requirepackagewithoptions"}:
            return (".sty",)
        return ("", ".tex", ".sty", ".cls", ".clo", ".def", ".rtx", ".cfg")

    def resolve_reference(
        target: str,
        *,
        command: str,
        source_parent: PurePosixPath,
    ) -> str | None:
        normalized_target = target.strip().strip("\"'")
        if not normalized_target or "#" in normalized_target:
            return None
        raw_path = PurePosixPath(normalized_target)
        if raw_path.is_absolute() or ".." in raw_path.parts:
            return None
        suffixes = ("",) if raw_path.suffix else command_suffixes(command)
        bases: list[PurePosixPath] = []
        for base in (source_parent, root_parent, PurePosixPath(".")):
            candidate = (base / raw_path).as_posix()
            if candidate.startswith("./"):
                candidate = candidate[2:]
            path = PurePosixPath(candidate)
            if path not in bases:
                bases.append(path)
        for base in bases:
            for suffix in suffixes:
                candidate_path = f"{base.as_posix()}{suffix}"
                if candidate_path in candidates_by_path:
                    return candidate_path
        for suffix in suffixes:
            basename = f"{raw_path.name}{suffix}"
            matches = candidates_by_name.get(basename, [])
            if len(matches) == 1:
                return matches[0]
        return None

    def referenced_paths(value: str, *, source_parent: PurePosixPath) -> list[str]:
        uncommented = without_comments(value)
        resolved: list[str] = []
        for match in reference_pattern.finditer(uncommented):
            if command_is_escaped(uncommented, match.start()):
                continue
            targets = match.group("targets").split(",")
            for target in targets:
                path = resolve_reference(
                    target,
                    command=match.group("command"),
                    source_parent=source_parent,
                )
                if path is not None and path not in resolved:
                    resolved.append(path)
        for match in unbraced_input_pattern.finditer(uncommented):
            if command_is_escaped(uncommented, match.start()):
                continue
            path = resolve_reference(
                match.group("target"),
                command=match.group("command"),
                source_parent=source_parent,
            )
            if path is not None and path not in resolved:
                resolved.append(path)
        return resolved

    content_cache: dict[str, bytes] = {}

    def candidate_content(path: str) -> bytes:
        if path not in content_cache:
            with storage.get(str(candidates_by_path[path]["storage_key"])) as stream:
                content_cache[path] = stream.read(max_asset_bytes + 1)
        return content_cache[path]

    # Breadth-first order keeps direct declarations ahead of transitive companions while
    # preserving source order inside each file.
    dependency_order: list[str] = []
    queue = referenced_paths(source_latex, source_parent=root_parent)
    queued = set(queue)
    while queue and len(dependency_order) < max_asset_count:
        path = queue.pop(0)
        if path in dependency_order:
            continue
        dependency_order.append(path)
        content = candidate_content(path)
        if len(content) > max_asset_bytes:
            continue
        if PurePosixPath(path).suffix.lower() not in {
            ".sty",
            ".cls",
            ".clo",
            ".def",
            ".rtx",
            ".ltx",
            ".tex",
            ".cfg",
        }:
            continue
        nested = referenced_paths(
            content.decode("utf-8", errors="replace"),
            source_parent=PurePosixPath(path).parent,
        )
        for nested_path in nested:
            if nested_path not in queued and nested_path not in dependency_order:
                queue.append(nested_path)
                queued.add(nested_path)

    def candidate_is_unreferenced(item: dict[str, Any]) -> bool:
        path = PurePosixPath(str(item.get("path", "")))
        variants = {path.as_posix()}
        with suppress(ValueError):
            variants.add(path.relative_to(root_parent).as_posix())
        return not any(candidate and candidate in source_latex for candidate in variants)

    candidates.sort(
        key=lambda item: (
            str(item.get("path", "")) not in dependency_order,
            (
                dependency_order.index(str(item.get("path", "")))
                if str(item.get("path", "")) in dependency_order
                else max_asset_count
            ),
            candidate_is_unreferenced(item),
            PurePosixPath(str(item.get("path", ""))).suffix.lower() == ".tex",
            str(item.get("path", "")),
        )
    )
    for item in candidates[:max_asset_count]:
        path = str(item["path"])
        key = str(item["storage_key"])
        content = content_cache.get(path)
        if content is None:
            with storage.get(key) as stream:
                content = stream.read(remaining + 1)
        if len(content) > remaining:
            continue
        assets[path] = content
        try:
            relative = PurePosixPath(path).relative_to(root_parent).as_posix()
        except ValueError:
            relative = path
        if relative != path and len(assets) < max_asset_count:
            assets[relative] = content
        remaining -= len(content)
    return assets


def _supersede_failed_formula_render_tasks(
    session: Session,
    *,
    successful_task_id: str,
    paper_id: str,
    block_id: str,
    content_hash: str,
    selector: object,
    target_kind: str,
    translation_id: str,
    scope_key: str | None = None,
) -> list[str]:
    """Retire failed attempts after the exact immutable formula renders successfully.

    Renderer upgrades intentionally create a new task idempotency key.  Without explicit
    supersession, a failure from an older renderer version keeps the paper ``PARTIAL`` forever,
    even though the current block already points at a successful SVG.  Match the durable formula
    identity rather than just the block so that a success never hides an unrelated formula error.
    """

    try:
        selector_identity = (
            formula_selector_key(selector) if isinstance(selector, dict) else "equation"
        )
    except FormulaSelectorError:
        return []
    superseded: list[str] = []
    statement = select(ProcessingTaskModel).where(
        ProcessingTaskModel.paper_id == paper_id,
        ProcessingTaskModel.task_type == RENDER_LATEX_FORMULA,
        ProcessingTaskModel.status == TaskStatus.FAILED,
    )
    if scope_key is not None:
        statement = statement.where(ProcessingTaskModel.scope_key == scope_key)
    failed_tasks = session.scalars(statement).all()
    for failed_task in failed_tasks:
        payload = failed_task.payload_json if isinstance(failed_task.payload_json, dict) else {}
        failed_selector = payload.get("formula_selector")
        try:
            failed_selector_identity = (
                formula_selector_key(failed_selector)
                if isinstance(failed_selector, dict)
                else "equation"
            )
        except FormulaSelectorError:
            continue
        if (
            str(payload.get("block_id", "")) != block_id
            or str(payload.get("content_hash", "")) != content_hash
            or str(payload.get("formula_target", "BLOCK")) != target_kind
            or str(payload.get("translation_id", "")) != translation_id
            or failed_selector_identity != selector_identity
        ):
            continue
        failed_task.status = TaskStatus.CANCELLED
        previous_result = (
            failed_task.result_json if isinstance(failed_task.result_json, dict) else {}
        )
        failed_task.result_json = {
            **previous_result,
            "superseded": True,
            "superseded_by_task_id": successful_task_id,
            "superseded_reason": (
                "A newer deterministic TeX renderer successfully rendered the exact formula."
            ),
        }
        failed_task.lease_expires_at = None
        failed_task.worker_id = None
        failed_task.finished_at = failed_task.finished_at or now_utc()
        superseded.append(failed_task.id)
    return superseded


def render_latex_formula(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    storage: Storage,
    renderer: LatexFigureRenderer | None,
    *,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Compile one exact formula with the paper preamble and cache an immutable SVG."""

    _require_toolchain_task_identity(
        task,
        renderer_version=LATEX_FORMULA_RENDERER_VERSION,
        fingerprint=toolchain_fingerprint,
    )
    if task.paper_id is None or renderer is None:
        raise TaskHandlerError("Formula rendering requires a paper and local TeX renderer.")
    block_id = str(task.payload.get("block_id", ""))
    expected_hash = str(task.payload.get("content_hash", ""))
    selector = task.payload.get("formula_selector")
    target_kind = str(task.payload.get("formula_target", "BLOCK"))
    translation_id = str(task.payload.get("translation_id", ""))
    preamble_key = _validated_task_preamble_key(
        paper_id=task.paper_id,
        preamble_key=str(task.payload.get("preamble_key", "")),
    )
    manifest_key = str(task.payload.get("manifest_key", ""))
    block_overlay_owner: tuple[str, str] | None = None

    with session_factory() as session:
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
            )
        )
        if block is None or block.content_hash != expected_hash:
            raise TaskHandlerError("The formula render target changed or no longer exists.")
        target_payload: object = block.render_payload_json
        translation: TranslationModel | None = None
        if target_kind == "BLOCK" and task.visibility is TaskVisibility.WORKSPACE_DERIVED:
            block_overlay_owner = _require_repair_task_scope(task)
            if not _repair_task_has_library_access(
                session,
                paper_id=task.paper_id,
                workspace_id=block_overlay_owner[0],
                requested_by_user_id=block_overlay_owner[1],
            ):
                raise TaskAuthorizationError(
                    "The formula render paper is outside the task workspace."
                )
            target_payload = effective_block_render(
                session,
                workspace_id=block_overlay_owner[0],
                block=block,
            ).payload
        if target_kind == "TRANSLATION":
            if task.workspace_id is None or task.requested_by_user_id is None:
                raise TaskAuthorizationError(
                    "The translated formula render scope is incomplete."
                )
            if task.visibility not in {
                TaskVisibility.WORKSPACE_DERIVED,
                TaskVisibility.USER_PRIVATE,
            }:
                raise TaskAuthorizationError(
                    "The translated formula render visibility is invalid."
                )
            expected_scope = TaskScope(
                task.visibility,
                workspace_id=task.workspace_id,
                requested_by_user_id=task.requested_by_user_id,
                credential_id=task.credential_id,
            )
            if task.scope_key != expected_scope.scope_key or not _repair_task_has_library_access(
                session,
                paper_id=task.paper_id,
                workspace_id=task.workspace_id,
                requested_by_user_id=task.requested_by_user_id,
            ):
                raise TaskAuthorizationError(
                    "The translated formula render scope is unauthorized."
                )
            translation = session.scalar(
                _repair_translation_statement(
                    task,
                    translation_id=translation_id,
                    workspace_id=task.workspace_id,
                    block_id=block.id,
                ).where(TranslationModel.source_hash == block.content_hash)
            )
            if translation is None or translation.source_hash != block.content_hash:
                raise TaskHandlerError("The translated formula revision is no longer current.")
            target_payload = translation.translated_payload_json

        if selector is not None:
            try:
                node = selected_formula_node(target_payload, selector)
            except FormulaSelectorError as error:
                raise TaskHandlerError(str(error)) from error
            rendered_latex = str(node.get("latex", ""))
            canonical_latex = str(node.get("canonical_latex") or rendered_latex)
            display = bool(node.get("display") or node.get("type") == "equation")
            math_node_id = (
                str(node["math_node_id"]) if isinstance(node.get("math_node_id"), str) else None
            )
        elif block.block_type == "EQUATION":
            equation_payload = target_payload if isinstance(target_payload, dict) else {}
            rendered_latex = str(equation_payload.get("latex", ""))
            canonical_latex = str(
                equation_payload.get("canonical_latex") or rendered_latex
            )
            display = True
            math_node_id = (
                str(equation_payload["math_node_id"])
                if isinstance(equation_payload.get("math_node_id"), str)
                else None
            )
        else:
            raise TaskHandlerError("An exact formula selector is required for this block.")

    preamble = ""
    if preamble_key and storage.exists(preamble_key):
        with storage.get(preamble_key) as stream:
            preamble = stream.read(250_001).decode("utf-8", errors="replace")
    loaded_manifest = (
        _load_task_source_manifest(
            storage,
            paper_id=task.paper_id,
            manifest_key=manifest_key,
            required=False,
        )
        if manifest_key
        else None
    )
    manifest: dict[str, Any] = loaded_manifest or {"files": []}
    assets = _latex_support_assets(
        manifest,
        storage,
        root_file=str(task.payload.get("root_file", "main.tex")),
        # Local style/class files are selected from the formula *and* the preamble that declares
        # them.  Looking at the isolated formula alone makes every transitive package dependency
        # appear unrelated and can exclude the exact paper-local implementation at the asset cap.
        source_latex=f"{preamble}\n{canonical_latex}",
    )
    # Use the standard environments rather than the shorthand ``\(...\)`` / ``\[...\]``.
    # Legacy papers sometimes redefine those two control symbols for literal scalable
    # parentheses, which silently removes the math-mode boundary in an isolated compile.
    formula_environment = "displaymath" if display else "math"
    formula_body = (
        rf"\begin{{{formula_environment}}}{canonical_latex}"
        rf"\end{{{formula_environment}}}"
    )
    artifact = renderer.render(
        source_latex=formula_body,
        preamble=preamble,
        assets=assets,
    )
    artifact_hash = hashlib.sha256(artifact.content).hexdigest()
    asset_name = f"{block_id}-{expected_hash[:16]}-{artifact_hash[:16]}.{artifact.extension}"
    scoped_asset_workspace = task.workspace_id if task.workspace_id is not None else None
    storage_key = (
        workspace_render_asset_key(
            workspace_id=scoped_asset_workspace,
            paper_id=task.paper_id,
            asset_directory="formulas",
            asset_name=asset_name,
        )
        if scoped_asset_workspace is not None
        else f"papers/{task.paper_id}/derived/formulas/{asset_name}"
    )
    storage.put(storage_key, io.BytesIO(artifact.content))
    render_metadata = {
        "strategy": "texlive-svg",
        "status": "succeeded",
        "asset": {
            "path": asset_name,
            "mime_type": artifact.mime_type,
            "byte_size": len(artifact.content),
            **({"scope": "workspace"} if scoped_asset_workspace is not None else {}),
        },
        "compiler": artifact.compiler,
        "renderer_version": LATEX_FORMULA_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
        "render_context": artifact.render_context,
        "canonical_latex_hash": hashlib.sha256(canonical_latex.encode()).hexdigest(),
        "source": "canonical-latex",
    }
    superseded_task_ids: list[str] = []
    with session_factory() as session:
        block = session.get(ContentBlockModel, block_id)
        if block is None or block.content_hash != expected_hash:
            storage.delete(storage_key)
            raise TaskHandlerError("The formula target changed before its asset was saved.")
        try:
            if selector is None and block_overlay_owner is not None:
                overlay_view = effective_block_render(
                    session,
                    workspace_id=block_overlay_owner[0],
                    block=block,
                )
                updated_payload = dict(overlay_view.payload)
                updated_payload["latex_render"] = render_metadata
                save_workspace_render_overlay(
                    session,
                    workspace_id=block_overlay_owner[0],
                    requested_by_user_id=block_overlay_owner[1],
                    block=block,
                    render_payload=updated_payload,
                    metadata={
                        **(
                            dict(overlay_view.overlay.metadata_json)
                            if overlay_view.overlay is not None
                            and isinstance(overlay_view.overlay.metadata_json, dict)
                            else {}
                        ),
                        "source": "workspace-derived-formula-render",
                        "last_task_id": task.id,
                    },
                )
            elif selector is None:
                updated_payload = dict(block.render_payload_json)
                updated_payload["latex_render"] = render_metadata
                block.render_payload_json = updated_payload
            elif target_kind == "BLOCK" and block_overlay_owner is not None:
                overlay_view = effective_block_render(
                    session,
                    workspace_id=block_overlay_owner[0],
                    block=block,
                )
                updated_payload = apply_formula_latex_render(
                    overlay_view.payload,
                    selector,
                    render_metadata,
                )
                save_workspace_render_overlay(
                    session,
                    workspace_id=block_overlay_owner[0],
                    requested_by_user_id=block_overlay_owner[1],
                    block=block,
                    render_payload=updated_payload,
                    metadata={
                        **(
                            dict(overlay_view.overlay.metadata_json)
                            if overlay_view.overlay is not None
                            and isinstance(overlay_view.overlay.metadata_json, dict)
                            else {}
                        ),
                        "source": "workspace-derived-formula-render",
                        "last_task_id": task.id,
                    },
                )
            elif target_kind == "BLOCK":
                block.render_payload_json = apply_formula_latex_render(
                    block.render_payload_json, selector, render_metadata
                )
            else:
                translation = session.scalar(
                    _repair_translation_statement(
                        task,
                        translation_id=translation_id,
                        workspace_id=task.workspace_id,
                        block_id=block.id,
                    ).where(TranslationModel.source_hash == block.content_hash)
                )
                if translation is None:
                    raise FormulaSelectorError("The translated formula no longer exists.")
                translation.translated_payload_json = apply_formula_latex_render(
                    translation.translated_payload_json,
                    selector,
                    render_metadata,
                )
            # Only global canonical formula renders may fan out to every translation. A
            # workspace overlay or private translation must never mutate another scope.
            if task.visibility in {
                TaskVisibility.GLOBAL_DERIVED,
                TaskVisibility.SYSTEM,
            }:
                block.render_payload_json = propagate_formula_latex_render(
                    block.render_payload_json,
                    math_node_id=math_node_id,
                    latex=rendered_latex,
                    render_metadata=render_metadata,
                )
                translations = session.scalars(
                    select(TranslationModel).where(
                        TranslationModel.block_id == block.id,
                        TranslationModel.status == TranslationStatus.SUCCEEDED,
                        TranslationModel.prompt_kind == "global",
                    )
                ).all()
                for translation in translations:
                    translation.translated_payload_json = propagate_formula_latex_render(
                        translation.translated_payload_json,
                        math_node_id=math_node_id,
                        latex=rendered_latex,
                        render_metadata=render_metadata,
                    )
            superseded_task_ids = _supersede_failed_formula_render_tasks(
                session,
                successful_task_id=task.id,
                paper_id=task.paper_id,
                block_id=block_id,
                content_hash=expected_hash,
                selector=selector,
                target_kind=target_kind,
                translation_id=translation_id,
                scope_key=task.scope_key,
            )
            session.flush()
            if task.visibility in {
                TaskVisibility.GLOBAL_DERIVED,
                TaskVisibility.SYSTEM,
            }:
                remaining_failures = session.scalar(
                    select(func.count())
                    .select_from(ProcessingTaskModel)
                    .where(
                        ProcessingTaskModel.paper_id == task.paper_id,
                        ProcessingTaskModel.status == TaskStatus.FAILED,
                        ProcessingTaskModel.task_type != ANSWER_QUESTION,
                    )
                )
                paper = session.get(PaperModel, task.paper_id)
                if paper is not None:
                    paper.processing_health = (
                        ProcessingHealth.PARTIAL if remaining_failures else ProcessingHealth.OK
                    )
            session.commit()
        except FormulaSelectorError as error:
            storage.delete(storage_key)
            raise TaskHandlerError(str(error)) from error
    return {
        "block_id": block_id,
        "asset_path": asset_name,
        "byte_size": len(artifact.content),
        "renderer_version": LATEX_FORMULA_RENDERER_VERSION,
        **_toolchain_provenance(toolchain_fingerprint),
        "shared_with_translations": True,
        "superseded_task_ids": superseded_task_ids,
    }


def reconcile_superseded_formula_render_tasks(
    session_factory: sessionmaker[Session],
    *,
    toolchain_fingerprint: str | None = None,
) -> int:
    """Backfill formula-render supersession after a renderer upgrade or interrupted deploy."""

    reconciled = 0
    touched_papers: set[str] = set()
    with session_factory() as session:
        successful_tasks = session.scalars(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.task_type == RENDER_LATEX_FORMULA,
                ProcessingTaskModel.status == TaskStatus.SUCCEEDED,
            )
        ).all()
        for successful_task in successful_tasks:
            payload = (
                successful_task.payload_json
                if isinstance(successful_task.payload_json, dict)
                else {}
            )
            if successful_task.paper_id is None or not _toolchain_identity_matches(
                payload,
                renderer_version=LATEX_FORMULA_RENDERER_VERSION,
                fingerprint=toolchain_fingerprint,
            ):
                continue
            superseded = _supersede_failed_formula_render_tasks(
                session,
                successful_task_id=successful_task.id,
                paper_id=successful_task.paper_id,
                block_id=str(payload.get("block_id", "")),
                content_hash=str(payload.get("content_hash", "")),
                selector=payload.get("formula_selector"),
                target_kind=str(payload.get("formula_target", "BLOCK")),
                translation_id=str(payload.get("translation_id", "")),
                scope_key=successful_task.scope_key,
            )
            if superseded:
                reconciled += len(superseded)
                touched_papers.add(successful_task.paper_id)
        session.flush()
        for paper_id in touched_papers:
            remaining_failures = session.scalar(
                select(func.count())
                .select_from(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == paper_id,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                    ProcessingTaskModel.task_type != ANSWER_QUESTION,
                )
            )
            paper = session.get(PaperModel, paper_id)
            if paper is not None:
                paper.processing_health = (
                    ProcessingHealth.PARTIAL if remaining_failures else ProcessingHealth.OK
                )
        session.commit()
    return reconciled


def _ensure_automatic_render_repair(
    session: Session,
    *,
    render_task: ProcessingTaskModel,
    error_message: str,
    preference: UserWorkspacePreference,
    credential_selector: Callable[[str], LlmTaskCredential | None] | None = None,
) -> tuple[ProcessingTaskModel | None, bool]:
    """Create one requester-attributed workspace repair after a canonical render failure."""
    if render_task.paper_id is None:
        return None, False
    payload = render_task.payload_json if isinstance(render_task.payload_json, dict) else {}
    block_id = str(payload.get("block_id", ""))
    content_hash = str(payload.get("content_hash", ""))
    block = session.scalar(
        select(ContentBlockModel).where(
            ContentBlockModel.id == block_id,
            ContentBlockModel.paper_id == render_task.paper_id,
            ContentBlockModel.block_type == "TABLE",
        )
    )
    if (
        block is None
        or block.content_hash != content_hash
        or not str(block.source_payload_json.get("latex", "")).strip()
    ):
        return None, False
    source_latex = str(block.source_payload_json.get("latex", "")).strip()
    render_payload = (
        block.render_payload_json if isinstance(block.render_payload_json, dict) else {}
    )
    table_render = (
        render_payload.get("table_render")
        if isinstance(render_payload.get("table_render"), dict)
        else {}
    )
    if table_render.get("repair_eligible") is False:
        render_result = (
            dict(render_task.result_json) if isinstance(render_task.result_json, dict) else {}
        )
        render_result.update(
            {
                "automatic_repair_skipped": True,
                "automatic_repair_skipped_reason": str(
                    table_render.get("repair_ineligible_reason")
                    or "This deterministic render failure is not eligible for AI repair."
                ),
            }
        )
        render_task.result_json = render_result
        return None, False
    if len(source_latex) > RENDER_REPAIR_MAX_LATEX_CHARS:
        render_result = (
            dict(render_task.result_json) if isinstance(render_task.result_json, dict) else {}
        )
        render_result.update(
            {
                "automatic_repair_skipped": True,
                "automatic_repair_skipped_reason": (
                    "The canonical table exceeds the lossless LLM repair round-trip limit."
                ),
                "source_latex_chars": len(source_latex),
                "repair_source_limit_chars": RENDER_REPAIR_MAX_LATEX_CHARS,
            }
        )
        render_task.result_json = render_result
        return None, False

    idempotency_key = (
        f"paper:{render_task.paper_id}:repair_render:{block.id}:{block.content_hash}:"
        f"latex:{TABLE_RENDERER_VERSION}"
    )
    lookup_scope = TaskScope.workspace_derived(
        workspace_id=preference.workspace_id,
        requested_by_user_id=preference.user_id,
    )
    repair_task = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.scope_key == lookup_scope.scope_key,
            ProcessingTaskModel.idempotency_key == idempotency_key,
        )
    )
    created = repair_task is None
    if repair_task is None:
        # Stale/forged targets and idempotent replays must not resolve a user's protected
        # credential. Validation and lookup stay in the task-creation transaction; the repair
        # handler independently revalidates the target before its lazy provider is first used.
        credential = (
            credential_selector(preference.user_id)
            if credential_selector is not None
            else None
        )
        if credential_selector is not None and credential is None:
            return None, False
        task_scope = TaskScope.workspace_derived(
            workspace_id=preference.workspace_id,
            requested_by_user_id=preference.user_id,
            credential_id=credential.credential_id if credential is not None else None,
        )
        repair_task = ProcessingTaskModel(
            visibility=task_scope.visibility,
            scope_key=task_scope.scope_key,
            workspace_id=task_scope.workspace_id,
            requested_by_user_id=task_scope.requested_by_user_id,
            credential_id=task_scope.credential_id,
            paper_id=render_task.paper_id,
            task_type=REPAIR_RENDER,
            priority=40,
            idempotency_key=idempotency_key,
            payload_json={
                "block_id": block.id,
                "content_hash": block.content_hash,
                "renderer": "latex",
                "error_message": error_message[:2000],
                "trigger": "automatic-render-failure",
                "failed_render_task_id": render_task.id,
            },
            max_attempts=1,
        )
        session.add(repair_task)
        session.flush()

    render_result: dict[str, Any] = (
        dict(render_task.result_json) if isinstance(render_task.result_json, dict) else {}
    )
    task_ids = [
        str(task_id) for task_id in render_result.get("automatic_repair_task_ids", []) if task_id
    ]
    if repair_task.id not in task_ids:
        task_ids.append(repair_task.id)
    render_result.update(
        {
            "automatic_repair_task_id": repair_task.id,
            "automatic_repair_task_ids": task_ids,
            "automatic_repair_created": created,
        }
    )
    render_task.result_json = render_result
    return repair_task, created


def schedule_automatic_render_repair(
    session_factory: sessionmaker[Session],
    *,
    render_task_id: str,
    error_message: str,
    credential_selector: Callable[[str], LlmTaskCredential | None] | None = None,
) -> tuple[str | None, bool]:
    with session_factory() as session:
        render_task = session.scalar(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.id == render_task_id,
                ProcessingTaskModel.task_type == RENDER_TABLE,
            )
        )
        if render_task is None:
            return None, False
        preferences = enabled_user_workspace_preferences(
            session,
            key=AUTOMATIC_AI_REPAIR_ENABLED_KEY,
            paper_id=render_task.paper_id,
        )
        if not preferences:
            render_result = (
                dict(render_task.result_json) if isinstance(render_task.result_json, dict) else {}
            )
            render_result.update(
                {
                    "automatic_repair_skipped": True,
                    "automatic_repair_skipped_reason": (
                        "Automatic AI-assisted repair is disabled for every active user in a "
                        "workspace library containing this paper."
                    ),
                }
            )
            render_task.result_json = render_result
        first_task: ProcessingTaskModel | None = None
        any_created = False
        for preference in preferences:
            repair_task, created = _ensure_automatic_render_repair(
                session,
                render_task=render_task,
                error_message=error_message,
                preference=preference,
                credential_selector=credential_selector,
            )
            first_task = first_task or repair_task
            any_created = any_created or created
        session.commit()
        return (first_task.id if first_task is not None else None), any_created


def enqueue_failed_table_render_repairs(
    session_factory: sessionmaker[Session],
    *,
    credential_selector: Callable[[str], LlmTaskCredential | None] | None = None,
) -> int:
    """Backfill automatic repairs for terminal failures recorded before this policy existed."""
    created = 0
    with session_factory() as session:
        failed_tasks = list(
            session.scalars(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.task_type == RENDER_TABLE,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                )
            ).all()
        )
        for render_task in failed_tasks:
            if render_task.paper_id is None:
                continue
            preferences = enabled_user_workspace_preferences(
                session,
                key=AUTOMATIC_AI_REPAIR_ENABLED_KEY,
                paper_id=render_task.paper_id,
            )
            for preference in preferences:
                _repair_task, was_created = _ensure_automatic_render_repair(
                    session,
                    render_task=render_task,
                    error_message=(
                        render_task.error_message or "Local LaTeX table rendering failed."
                    ),
                    preference=preference,
                    credential_selector=credential_selector,
                )
                created += int(was_created)
        session.commit()
    return created


def _supersede_table_render_tasks(
    session: Session,
    *,
    paper_id: str,
    block_id: str,
    repair_task_id: str,
    scope_key: str | None = None,
) -> list[str]:
    superseded: list[str] = []
    statement = select(ProcessingTaskModel).where(
        ProcessingTaskModel.paper_id == paper_id,
        ProcessingTaskModel.task_type == RENDER_TABLE,
        ProcessingTaskModel.payload_json["block_id"].as_string() == block_id,
        ProcessingTaskModel.status.notin_([TaskStatus.SUCCEEDED, TaskStatus.CANCELLED]),
    )
    if scope_key is not None:
        statement = statement.where(ProcessingTaskModel.scope_key == scope_key)
    tasks = session.scalars(statement).all()
    for render_task in tasks:
        render_task.status = TaskStatus.CANCELLED
        current_result = (
            render_task.result_json if isinstance(render_task.result_json, dict) else {}
        )
        render_task.result_json = {
            **current_result,
            "superseded": True,
            "superseded_by_task_id": repair_task_id,
            "superseded_reason": (
                "AI repair produced validated LaTeX and queued a replacement table render."
            ),
        }
        render_task.lease_expires_at = None
        render_task.worker_id = None
        render_task.finished_at = render_task.finished_at or now_utc()
        superseded.append(render_task.id)
    return superseded


def _supersede_figure_render_tasks(
    session: Session,
    *,
    paper_id: str,
    block_id: str,
    repair_task_id: str,
    scope_key: str | None = None,
) -> list[str]:
    superseded: list[str] = []
    statement = select(ProcessingTaskModel).where(
        ProcessingTaskModel.paper_id == paper_id,
        ProcessingTaskModel.task_type == RENDER_FIGURE,
        ProcessingTaskModel.payload_json["block_id"].as_string() == block_id,
        ProcessingTaskModel.status.notin_([TaskStatus.SUCCEEDED, TaskStatus.CANCELLED]),
    )
    if scope_key is not None:
        statement = statement.where(ProcessingTaskModel.scope_key == scope_key)
    tasks = session.scalars(statement).all()
    for render_task in tasks:
        render_task.status = TaskStatus.CANCELLED
        current_result = (
            render_task.result_json if isinstance(render_task.result_json, dict) else {}
        )
        render_task.result_json = {
            **current_result,
            "superseded": True,
            "superseded_by_task_id": repair_task_id,
            "superseded_reason": (
                "AI repair produced validated LaTeX and queued a replacement figure render."
            ),
        }
        render_task.lease_expires_at = None
        render_task.worker_id = None
        render_task.finished_at = render_task.finished_at or now_utc()
        superseded.append(render_task.id)
    return superseded


def reconcile_repaired_table_tasks(
    session_factory: sessionmaker[Session],
) -> int:
    """Backfill supersession for repairs completed before task linkage was introduced."""
    reconciled = 0
    touched_papers: set[str] = set()
    with session_factory() as session:
        blocks = session.scalars(
            select(ContentBlockModel).where(ContentBlockModel.block_type == "TABLE")
        ).all()
        for block in blocks:
            payload = (
                block.render_payload_json if isinstance(block.render_payload_json, dict) else {}
            )
            rows = payload.get("rows")
            ai_repair = payload.get("ai_repair")
            if (
                not isinstance(rows, list)
                or not rows
                or not isinstance(ai_repair, dict)
                or ai_repair.get("model_assisted") is not True
            ):
                continue
            repair_task = session.scalar(
                select(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == block.paper_id,
                    ProcessingTaskModel.task_type == REPAIR_RENDER,
                    ProcessingTaskModel.payload_json["block_id"].as_string() == block.id,
                    ProcessingTaskModel.status == TaskStatus.SUCCEEDED,
                )
                .order_by(ProcessingTaskModel.finished_at.desc())
                .limit(1)
            )
            if repair_task is None:
                continue
            superseded = _supersede_table_render_tasks(
                session,
                paper_id=block.paper_id,
                block_id=block.id,
                repair_task_id=repair_task.id,
            )
            table_render = payload.get("table_render")
            if isinstance(table_render, dict) and table_render.get("status") == "failed":
                updated_render = dict(table_render)
                previous_error = updated_render.pop("error", None)
                updated_render.update(
                    {
                        "status": "repaired",
                        "repair_task_id": repair_task.id,
                        "fallback": "structured-html",
                    }
                )
                if previous_error:
                    updated_render["superseded_error"] = previous_error
                payload = dict(payload)
                payload["table_render"] = updated_render
                block.render_payload_json = payload
            if superseded:
                reconciled += len(superseded)
                touched_papers.add(block.paper_id)
        session.flush()
        for paper_id in touched_papers:
            remaining_failures = session.scalar(
                select(func.count())
                .select_from(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == paper_id,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                    ProcessingTaskModel.task_type != ANSWER_QUESTION,
                )
            )
            paper = session.get(PaperModel, paper_id)
            if paper is not None:
                paper.processing_health = (
                    ProcessingHealth.PARTIAL if remaining_failures else ProcessingHealth.OK
                )
        session.commit()
    return reconciled


def _translation_for_persisted_task_scope(
    session: Session,
    task: ProcessingTaskModel,
    *,
    translation_id: str,
    block_id: str,
    require_succeeded: bool,
) -> TranslationModel | None:
    """Resolve payload identity only through the persisted task's ownership boundary."""

    try:
        visibility = TaskVisibility(task.visibility)
        expected_scope = TaskScope(
            visibility,
            workspace_id=task.workspace_id,
            requested_by_user_id=task.requested_by_user_id,
            credential_id=task.credential_id,
        )
    except ValueError:
        return None
    if (
        visibility not in {
            TaskVisibility.WORKSPACE_DERIVED,
            TaskVisibility.USER_PRIVATE,
        }
        or task.scope_key != expected_scope.scope_key
        or task.workspace_id is None
    ):
        return None
    statement = select(TranslationModel).where(
        TranslationModel.id == translation_id,
        TranslationModel.block_id == block_id,
        TranslationModel.workspace_id == task.workspace_id,
    )
    if require_succeeded:
        statement = statement.where(
            TranslationModel.status == TranslationStatus.SUCCEEDED,
        )
    if visibility is TaskVisibility.USER_PRIVATE:
        statement = statement.where(
            TranslationModel.prompt_kind.in_(["custom", "paper"]),
            TranslationModel.requested_by_user_id == task.requested_by_user_id,
        )
    else:
        statement = statement.where(TranslationModel.prompt_kind == "global")
    return session.scalar(statement)


def _reconcile_deterministic_formula_tasks_in_session(
    session: Session,
    *,
    paper_id: str | None = None,
    reconciliation_key: str,
) -> tuple[int, set[str]]:
    reconciled = 0
    touched_papers: set[str] = set()
    failed_query = select(ProcessingTaskModel).where(
        ProcessingTaskModel.task_type == REPAIR_RENDER,
        ProcessingTaskModel.status == TaskStatus.FAILED,
    )
    if paper_id is not None:
        failed_query = failed_query.where(ProcessingTaskModel.paper_id == paper_id)
    failed_tasks = session.scalars(failed_query).all()

    def selector_formula_changed(target_payload: object, selector: object) -> bool:
        if not isinstance(selector, dict):
            return False
        path = selector.get("path")
        exact_latex = selector.get("exact_latex")
        if not isinstance(path, list) or not isinstance(exact_latex, str):
            return False
        current = target_payload
        for part in path:
            if isinstance(part, str):
                if not isinstance(current, dict) or part not in current:
                    return False
                current = current[part]
            elif isinstance(part, int):
                if not isinstance(current, list) or part < 0 or part >= len(current):
                    return False
                current = current[part]
            else:
                return False
        return bool(
            isinstance(current, dict)
            and current.get("type") in {"math", "equation"}
            and isinstance(current.get("latex"), str)
            and current["latex"] != exact_latex
        )

    for failed_task in failed_tasks:
        payload = failed_task.payload_json if isinstance(failed_task.payload_json, dict) else {}
        selector = payload.get("formula_selector")
        block_id = str(payload.get("block_id", ""))
        if not block_id or not failed_task.paper_id:
            continue
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == failed_task.paper_id,
            )
        )
        if block is None:
            continue
        applied_kind = "corrected-inline-latex"
        if isinstance(selector, dict):
            target_payload: object = block.render_payload_json
            stale_translation_revision = False
            if payload.get("formula_target") == "TRANSLATION":
                translation = _translation_for_persisted_task_scope(
                    session,
                    failed_task,
                    translation_id=str(payload.get("translation_id", "")),
                    block_id=block.id,
                    require_succeeded=True,
                )
                if translation is None:
                    continue
                target_payload = translation.translated_payload_json
                stale_translation_revision = (
                    isinstance(payload.get("content_hash"), str)
                    and payload["content_hash"] != block.content_hash
                )
            try:
                matches = formula_matches_deterministic_normalization(
                    target_payload,
                    selector,
                )
            except FormulaSelectorError:
                matches = False
            if not matches and stale_translation_revision:
                matches = True
                applied_kind = "superseded-stale-translation-repair"
            elif not matches and selector_formula_changed(target_payload, selector):
                # A parser upgrade or sibling repair replaced the formula at the exact durable
                # path. The old failure no longer describes current derived data. If the new
                # formula also fails, the browser creates a distinct task with its new hash.
                matches = True
                applied_kind = "superseded-inline-latex-repair"
        elif payload.get("renderer") == "katex" and block.block_type == "EQUATION":
            matches = equation_matches_deterministic_normalization(
                block.source_payload_json,
                block.render_payload_json,
                str(payload.get("error_message", "")),
            )
            applied_kind = "corrected-equation-latex"
        else:
            matches = False
        if not matches:
            continue
        failed_task.status = TaskStatus.SUCCEEDED
        failed_task.error_code = None
        failed_task.error_message = None
        failed_task.finished_at = now_utc()
        failed_task.result_json = {
            "block_id": block.id,
            "renderer": "katex",
            "confidence": 1.0,
            "applied": True,
            "applied_kind": applied_kind,
            "repair_mode": "deterministic-katex",
            reconciliation_key: True,
        }
        touched_papers.add(failed_task.paper_id)
        reconciled += 1
    return reconciled, touched_papers


def reconcile_deterministic_formula_tasks(
    session_factory: sessionmaker[Session],
) -> int:
    """Recover stale failed formula tasks whose normalized payload is already present."""

    with session_factory() as session:
        reconciled, touched_papers = _reconcile_deterministic_formula_tasks_in_session(
            session,
            reconciliation_key="reconciled_on_worker_start",
        )
        session.flush()
        for touched_paper_id in touched_papers:
            remaining_failures = session.scalar(
                select(func.count())
                .select_from(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == touched_paper_id,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                    ProcessingTaskModel.task_type != ANSWER_QUESTION,
                )
            )
            paper = session.get(PaperModel, touched_paper_id)
            if paper is not None:
                paper.processing_health = (
                    ProcessingHealth.PARTIAL if remaining_failures else ProcessingHealth.OK
                )
        session.commit()
    return reconciled


def enqueue_stale_formula_render_tasks(
    session_factory: sessionmaker[Session],
    *,
    toolchain_fingerprint: str | None = None,
) -> int:
    """Re-render successful formula assets after a deterministic renderer upgrade."""

    created = 0
    with session_factory() as session:
        previous_tasks = session.scalars(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.task_type == RENDER_LATEX_FORMULA,
                ProcessingTaskModel.status == TaskStatus.SUCCEEDED,
            )
        ).all()
        for previous in previous_tasks:
            payload = previous.payload_json if isinstance(previous.payload_json, dict) else {}
            if _toolchain_identity_matches(
                payload,
                renderer_version=LATEX_FORMULA_RENDERER_VERSION,
                fingerprint=toolchain_fingerprint,
            ):
                continue
            block_id = str(payload.get("block_id") or "")
            content_hash = str(payload.get("content_hash") or "")
            if not previous.paper_id or not block_id or not content_hash:
                continue
            block = session.scalar(
                select(ContentBlockModel).where(
                    ContentBlockModel.id == block_id,
                    ContentBlockModel.paper_id == previous.paper_id,
                )
            )
            if block is None or block.content_hash != content_hash:
                continue
            selector = payload.get("formula_selector")
            if isinstance(selector, dict):
                target_payload: object = block.render_payload_json
                if payload.get("formula_target") == "TRANSLATION":
                    translation_id = str(payload.get("translation_id") or "")
                    translation = _translation_for_persisted_task_scope(
                        session,
                        previous,
                        translation_id=translation_id,
                        block_id=block.id,
                        require_succeeded=True,
                    )
                    if translation is None:
                        continue
                    target_payload = translation.translated_payload_json
                try:
                    selected_formula_node(target_payload, selector)
                    selector_identity = formula_selector_key(selector)
                except FormulaSelectorError:
                    continue
            elif block.block_type == "EQUATION":
                selector_identity = "equation"
            else:
                continue
            target_identity = (
                f"translation:{payload.get('translation_id')}"
                if payload.get("formula_target") == "TRANSLATION"
                else "block"
            )
            cache_identity = toolchain_cache_identity(
                LATEX_FORMULA_RENDERER_VERSION,
                toolchain_fingerprint,
            )
            idempotency_key = (
                f"paper:{previous.paper_id}:render_latex_formula:"
                f"{cache_identity}:{block.id}:{target_identity}:"
                f"{content_hash}:{selector_identity}"
            )
            exists = session.scalar(
                select(ProcessingTaskModel.id).where(
                    ProcessingTaskModel.scope_key == previous.scope_key,
                    ProcessingTaskModel.idempotency_key == idempotency_key,
                )
            )
            if exists is not None:
                continue
            replacement_payload = {
                **payload,
                "renderer_version": LATEX_FORMULA_RENDERER_VERSION,
                **_toolchain_provenance(toolchain_fingerprint),
            }
            if toolchain_fingerprint is None:
                replacement_payload.pop("toolchain_fingerprint", None)
            session.add(
                ProcessingTaskModel(
                    visibility=previous.visibility,
                    scope_key=previous.scope_key,
                    workspace_id=previous.workspace_id,
                    requested_by_user_id=previous.requested_by_user_id,
                    credential_id=previous.credential_id,
                    paper_id=previous.paper_id,
                    task_type=RENDER_LATEX_FORMULA,
                    priority=35,
                    idempotency_key=idempotency_key,
                    payload_json=replacement_payload,
                    max_attempts=1,
                )
            )
            created += 1
        session.commit()
    return created


def _require_repair_task_scope(task: QueuedTask) -> tuple[str, str]:
    if task.visibility not in {
        TaskVisibility.WORKSPACE_DERIVED,
        TaskVisibility.USER_PRIVATE,
    }:
        raise TaskAuthorizationError(
            "Render repair tasks require workspace-derived or user-private visibility."
        )
    if task.workspace_id is None or task.requested_by_user_id is None:
        raise TaskAuthorizationError("Render repair task ownership is incomplete.")
    expected = TaskScope(
        task.visibility,
        workspace_id=task.workspace_id,
        requested_by_user_id=task.requested_by_user_id,
        credential_id=task.credential_id,
    )
    if task.scope_key != expected.scope_key:
        raise TaskAuthorizationError("Render repair task scope is inconsistent.")
    return task.workspace_id, task.requested_by_user_id


def _repair_translation_statement(
    task: QueuedTask,
    *,
    translation_id: str,
    workspace_id: str,
    block_id: str,
):
    statement = select(TranslationModel).where(
        TranslationModel.id == translation_id,
        TranslationModel.workspace_id == workspace_id,
        TranslationModel.block_id == block_id,
        TranslationModel.status == TranslationStatus.SUCCEEDED,
    )
    if task.visibility is TaskVisibility.USER_PRIVATE:
        return statement.where(
            TranslationModel.prompt_kind.in_(["custom", "paper"]),
            TranslationModel.requested_by_user_id == task.requested_by_user_id,
        )
    return statement.where(TranslationModel.prompt_kind == "global")


def _repair_task_has_library_access(
    session: Session,
    *,
    paper_id: str,
    workspace_id: str,
    requested_by_user_id: str,
) -> bool:
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
                LibraryItemModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.user_id == requested_by_user_id,
                UserModel.status == "ACTIVE",
            )
        )
        is not None
    )


def repair_render(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    storage: Storage,
    repairer: RenderRepairClient | None,
    *,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    workspace_id, requested_by_user_id = _require_repair_task_scope(task)
    if task.paper_id is None:
        raise TaskHandlerError("Render repair requires a paper.")
    block_id = str(task.payload.get("block_id", ""))
    expected_hash = str(task.payload.get("content_hash", ""))
    renderer = str(task.payload.get("renderer", "unknown"))[:64]
    error_message = str(task.payload.get("error_message", ""))[:2000]
    formula_selector = task.payload.get("formula_selector")
    formula_target = str(task.payload.get("formula_target", "BLOCK"))
    translation_id = str(task.payload.get("translation_id", ""))
    expected_translation_input_hash = str(task.payload.get("translation_input_hash", ""))
    with session_factory() as session:
        if not _repair_task_has_library_access(
            session,
            paper_id=task.paper_id,
            workspace_id=workspace_id,
            requested_by_user_id=requested_by_user_id,
        ):
            raise TaskAuthorizationError(
                "The render repair paper is outside the task workspace."
            )
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
            )
        )
        if block is None or block.content_hash != expected_hash:
            raise TaskHandlerError("The render repair target changed or no longer exists.")
        workspace_render_payload = effective_block_render(
            session,
            workspace_id=workspace_id,
            block=block,
        ).payload
        source_latex = str(block.source_payload_json.get("latex", ""))
        block_type = block.block_type
        if renderer == "katex" and block_type == "EQUATION" and formula_selector is None:
            source_latex = normalize_katex_latex(
                str(workspace_render_payload.get("latex", source_latex))
            )
        elif block_type == "TABLE" and formula_selector is None:
            source_latex = normalize_table_latex_for_render(source_latex)
        elif block_type == "FIGURE" and formula_selector is None:
            source_latex = normalize_figure_latex_for_render(source_latex)
        if formula_selector is not None:
            target_payload: object = workspace_render_payload
            if formula_target == "TRANSLATION":
                translation = session.scalar(
                    _repair_translation_statement(
                        task,
                        translation_id=translation_id,
                        workspace_id=workspace_id,
                        block_id=block.id,
                    )
                )
                if (
                    translation is None
                    or translation.source_hash != block.content_hash
                    or translation.input_hash != expected_translation_input_hash
                ):
                    raise TaskHandlerError(
                        "The translated formula revision changed or no longer exists."
                    )
                target_payload = translation.translated_payload_json
            try:
                source_latex = selected_formula(target_payload, formula_selector)
            except FormulaSelectorError as error:
                raise TaskHandlerError(str(error)) from error
            block_type = "INLINE_FORMULA"
        source = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == task.paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
        manifest_key = source.storage_key if source else None

    manifest: dict[str, Any] = {"files": []}
    if manifest_key:
        loaded_manifest = _load_task_source_manifest(
            storage,
            paper_id=task.paper_id,
            manifest_key=manifest_key,
        )
        if loaded_manifest is None:  # pragma: no cover - required manifests fail in the loader
            raise TaskHandlerError("The paper source manifest is unavailable.")
        manifest = loaded_manifest
    asset_items = {
        str(item.get("path")): item
        for item in manifest.get("files", [])
        if str(item.get("mime_type", "")).startswith(("image/", "application/pdf"))
    }
    deterministic_katex = (
        normalize_katex_latex(source_latex) if renderer == "katex" else source_latex
    )
    deterministic_repair = renderer == "katex" and deterministic_katex != source_latex
    if deterministic_repair:
        repaired = {
            "corrected_latex": deterministic_katex,
            "plain_text": "",
            "table_rows": [],
            "asset_paths": [],
            "explanation": (
                "Applied the bounded local KaTeX compatibility pipeline; no provider call was "
                "needed."
            ),
            "confidence": 1.0,
        }
    else:
        if repairer is None:
            raise TaskHandlerError(
                "Render repair requires a configured LLM provider after local normalization."
            )
        repaired = repairer.repair(
            block_type=block_type,
            source_latex=source_latex,
            renderer=renderer,
            error_message=error_message,
            available_assets=sorted(asset_items),
        )
    raw_corrected_latex = repaired["corrected_latex"]
    normalization_pipeline: str | None = None
    if deterministic_repair:
        normalization_pipeline = KATEX_NORMALIZER_VERSION
    elif renderer == "katex" and repaired["corrected_latex"]:
        repaired["corrected_latex"] = normalize_katex_latex(repaired["corrected_latex"])
        normalization_pipeline = KATEX_NORMALIZER_VERSION
        try:
            validate_katex_repair(
                source_latex=source_latex,
                corrected_latex=repaired["corrected_latex"],
                renderer_error=error_message,
            )
        except FormulaRepairValidationError as error:
            raise TaskHandlerError(str(error)) from error
    elif block_type == "TABLE" and repaired["corrected_latex"]:
        repaired["corrected_latex"] = normalize_table_latex_for_render(repaired["corrected_latex"])
        normalization_pipeline = TABLE_RENDERER_VERSION
    elif block_type == "FIGURE" and repaired["corrected_latex"]:
        repaired["corrected_latex"] = normalize_figure_latex_for_render(repaired["corrected_latex"])
        normalization_pipeline = FIGURE_RENDERER_VERSION

    with session_factory() as session:
        if not _repair_task_has_library_access(
            session,
            paper_id=task.paper_id,
            workspace_id=workspace_id,
            requested_by_user_id=requested_by_user_id,
        ):
            raise TaskAuthorizationError(
                "The render repair paper left the task workspace before publication."
            )
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
            )
        )
        if block is None or block.content_hash != expected_hash:
            raise TaskHandlerError("The render repair target changed before it could be saved.")
        workspace_render = effective_block_render(
            session,
            workspace_id=workspace_id,
            block=block,
        )
        workspace_render_payload = workspace_render.payload
        corrected_latex = repaired["corrected_latex"]
        applied_kind: str | None = None
        superseded_render_task_ids: list[str] = []
        replacement_render_task_id: str | None = None
        repair_metadata = {
            "renderer": renderer,
            "explanation": repaired["explanation"],
            "confidence": repaired["confidence"],
            "model_assisted": not deterministic_repair,
            "repair_mode": ("deterministic-katex" if deterministic_repair else "model-assisted"),
            "repair_task_id": task.id,
            "content_hash": expected_hash,
            "target": formula_target,
            **(
                {
                    "normalization_pipeline": normalization_pipeline,
                    "normalization_changed": (raw_corrected_latex != corrected_latex),
                    **(
                        {"raw_corrected_latex": raw_corrected_latex}
                        if raw_corrected_latex != corrected_latex
                        else {}
                    ),
                }
                if normalization_pipeline is not None
                else {}
            ),
        }
        if formula_selector is not None:
            if not corrected_latex:
                raise TaskHandlerError("The inline formula repair returned no corrected LaTeX.")
            if formula_target == "TRANSLATION":
                translation = session.scalar(
                    _repair_translation_statement(
                        task,
                        translation_id=translation_id,
                        workspace_id=workspace_id,
                        block_id=block.id,
                    )
                )
                if (
                    translation is None
                    or translation.source_hash != block.content_hash
                    or translation.input_hash != expected_translation_input_hash
                ):
                    raise TaskHandlerError(
                        "The translated formula revision changed before repair could be saved."
                    )
                try:
                    translation.translated_payload_json = apply_formula_repair(
                        translation.translated_payload_json,
                        formula_selector,
                        corrected_latex,
                        repair_metadata=repair_metadata,
                    )
                except FormulaSelectorError as error:
                    raise TaskHandlerError(str(error)) from error
            else:
                try:
                    workspace_render_payload = apply_formula_repair(
                        workspace_render_payload,
                        formula_selector,
                        corrected_latex,
                        repair_metadata=repair_metadata,
                    )
                except FormulaSelectorError as error:
                    raise TaskHandlerError(str(error)) from error
            applied_kind = "corrected-inline-latex"
        elif block.block_type == "EQUATION" and corrected_latex:
            payload = dict(workspace_render_payload)
            payload.setdefault("original_latex", payload.get("latex", source_latex))
            payload["latex"] = corrected_latex
            applied_kind = "corrected-latex"
            payload["ai_repair"] = repair_metadata
            workspace_render_payload = payload
        elif block.block_type == "TABLE" and corrected_latex:
            if not re.search(
                r"\\begin\s*\{(?:tabular\*?|tabularx|longtable)\}",
                corrected_latex,
            ):
                raise TaskHandlerError(
                    "The table repair returned no complete tabular or longtable environment."
                )
            payload = dict(workspace_render_payload)
            if repaired["table_rows"]:
                payload["rows"] = repaired["table_rows"]
                payload["structured"] = True
            table_render = dict(payload.get("table_render", {}))
            previous_error = table_render.pop("error", None)
            previous_render = session.scalar(
                select(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == task.paper_id,
                    ProcessingTaskModel.task_type == RENDER_TABLE,
                    ProcessingTaskModel.visibility.in_(
                        [TaskVisibility.GLOBAL_DERIVED, TaskVisibility.SYSTEM]
                    ),
                    ProcessingTaskModel.payload_json["block_id"].as_string() == block.id,
                )
                .order_by(ProcessingTaskModel.created_at.desc())
                .limit(1)
            )
            previous_render_payload = (
                previous_render.payload_json
                if previous_render is not None and isinstance(previous_render.payload_json, dict)
                else {}
            )
            preamble_key = str(previous_render_payload.get("preamble_key", ""))
            superseded_render_task_ids = _supersede_table_render_tasks(
                session,
                paper_id=task.paper_id,
                block_id=block.id,
                repair_task_id=task.id,
                scope_key=task.scope_key,
            )
            replacement_render = _ensure_table_render_task(
                session,
                paper_id=task.paper_id,
                block_id=block.id,
                content_hash=block.content_hash,
                preamble_key=preamble_key,
                source_latex=corrected_latex,
                repair_task_id=task.id,
                task_scope=TaskScope.workspace_derived(
                    workspace_id=workspace_id,
                    requested_by_user_id=requested_by_user_id,
                    credential_id=task.credential_id,
                ),
                toolchain_fingerprint=toolchain_fingerprint,
            )
            replacement_render_task_id = replacement_render.id
            table_render.update(
                {
                    "status": "queued",
                    "strategy": "latex-svg",
                    "renderer_version": TABLE_RENDERER_VERSION,
                    **_toolchain_provenance(toolchain_fingerprint),
                    "repair_task_id": task.id,
                    "render_task_id": replacement_render.id,
                    "source": "ai-repaired-latex",
                    "corrected_latex": corrected_latex,
                }
            )
            if previous_error:
                table_render["superseded_error"] = previous_error
            payload["table_render"] = table_render
            applied_kind = "corrected-table-latex"
            payload["ai_repair"] = repair_metadata
            workspace_render_payload = payload
        elif block.block_type == "FIGURE" and repaired["asset_paths"]:
            payload = dict(workspace_render_payload)
            payload["assets"] = [
                {
                    "path": path,
                    "mime_type": asset_items[path].get("mime_type", "application/octet-stream"),
                    "byte_size": asset_items[path].get("byte_size"),
                }
                for path in repaired["asset_paths"]
            ]
            payload["missing_assets"] = []
            applied_kind = "resolved-assets"
            payload["ai_repair"] = repair_metadata
            workspace_render_payload = payload
        elif block.block_type == "FIGURE" and corrected_latex:
            payload = dict(workspace_render_payload)
            figure_render = dict(payload.get("figure_render", {}))
            previous_error = figure_render.pop("error", None)
            previous_render = session.scalar(
                select(ProcessingTaskModel)
                .where(
                    ProcessingTaskModel.paper_id == task.paper_id,
                    ProcessingTaskModel.task_type == RENDER_FIGURE,
                    ProcessingTaskModel.visibility.in_(
                        [TaskVisibility.GLOBAL_DERIVED, TaskVisibility.SYSTEM]
                    ),
                    ProcessingTaskModel.payload_json["block_id"].as_string() == block.id,
                )
                .order_by(ProcessingTaskModel.created_at.desc())
                .limit(1)
            )
            previous_render_payload = (
                previous_render.payload_json
                if previous_render is not None and isinstance(previous_render.payload_json, dict)
                else {}
            )
            preamble_key = str(previous_render_payload.get("preamble_key", ""))
            superseded_render_task_ids = _supersede_figure_render_tasks(
                session,
                paper_id=task.paper_id,
                block_id=block.id,
                repair_task_id=task.id,
                scope_key=task.scope_key,
            )
            replacement_render = _ensure_figure_render_task(
                session,
                paper_id=task.paper_id,
                block_id=block.id,
                content_hash=block.content_hash,
                preamble_key=preamble_key,
                source_latex=corrected_latex,
                repair_task_id=task.id,
                task_scope=TaskScope.workspace_derived(
                    workspace_id=workspace_id,
                    requested_by_user_id=requested_by_user_id,
                    credential_id=task.credential_id,
                ),
                toolchain_fingerprint=toolchain_fingerprint,
            )
            replacement_render_task_id = replacement_render.id
            figure_render.update(
                {
                    "status": "queued",
                    "strategy": "latex-svg",
                    "renderer_version": FIGURE_RENDERER_VERSION,
                    **_toolchain_provenance(toolchain_fingerprint),
                    "repair_task_id": task.id,
                    "render_task_id": replacement_render.id,
                    "source": "ai-repaired-latex",
                    "corrected_latex": corrected_latex,
                }
            )
            if previous_error:
                figure_render["superseded_error"] = previous_error
            payload["figure_render"] = figure_render
            applied_kind = "corrected-figure-latex"
            payload["ai_repair"] = repair_metadata
            workspace_render_payload = payload
        elif repaired["plain_text"] and block.block_type != "FIGURE":
            payload = dict(workspace_render_payload)
            payload["text"] = repaired["plain_text"]
            applied_kind = "plain-text"
            payload["ai_repair"] = repair_metadata
            workspace_render_payload = payload
        if applied_kind is None:
            raise TaskHandlerError("The render repair returned no applicable replacement.")
        if formula_selector is None:
            payload = dict(workspace_render_payload)
            ai_repair = dict(payload.get("ai_repair", {}))
            ai_repair["applied_kind"] = applied_kind
            payload["ai_repair"] = ai_repair
            workspace_render_payload = payload
        if not (
            formula_selector is not None and formula_target == "TRANSLATION"
        ):
            metadata = (
                dict(workspace_render.overlay.metadata_json)
                if workspace_render.overlay is not None
                and isinstance(workspace_render.overlay.metadata_json, dict)
                else {}
            )
            metadata.update(
                {
                    "source": "ai-render-repair",
                    "repair_task_id": task.id,
                    "applied_kind": applied_kind,
                    "renderer": renderer,
                }
            )
            save_workspace_render_overlay(
                session,
                workspace_id=workspace_id,
                requested_by_user_id=requested_by_user_id,
                block=block,
                render_payload=workspace_render_payload,
                metadata=metadata,
            )
        session.flush()
        reconciled_formula_task_ids: list[str] = []
        if renderer == "katex":
            stale_formula_tasks = session.scalars(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.paper_id == task.paper_id,
                    ProcessingTaskModel.task_type == REPAIR_RENDER,
                    ProcessingTaskModel.workspace_id == workspace_id,
                    (
                        (
                            ProcessingTaskModel.visibility
                            == TaskVisibility.WORKSPACE_DERIVED
                        )
                        | (
                            (ProcessingTaskModel.visibility == TaskVisibility.USER_PRIVATE)
                            & (
                                ProcessingTaskModel.requested_by_user_id
                                == requested_by_user_id
                            )
                        )
                    ),
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                    ProcessingTaskModel.payload_json["block_id"].as_string() == block.id,
                )
            ).all()
            for stale_task in stale_formula_tasks:
                stale_payload = (
                    stale_task.payload_json if isinstance(stale_task.payload_json, dict) else {}
                )
                stale_selector = stale_payload.get("formula_selector")
                if not isinstance(stale_selector, dict):
                    continue
                stale_target: object = workspace_render_payload
                if stale_payload.get("formula_target") == "TRANSLATION":
                    stale_translation = _translation_for_persisted_task_scope(
                        session,
                        stale_task,
                        translation_id=str(stale_payload.get("translation_id", "")),
                        block_id=block.id,
                        require_succeeded=True,
                    )
                    if (
                        stale_translation is None
                        or stale_translation.source_hash != block.content_hash
                    ):
                        continue
                    stale_target = stale_translation.translated_payload_json
                try:
                    reconciled = formula_matches_deterministic_normalization(
                        stale_target,
                        stale_selector,
                    )
                except FormulaSelectorError:
                    reconciled = False
                if not reconciled:
                    continue
                stale_task.status = TaskStatus.SUCCEEDED
                stale_task.error_code = None
                stale_task.error_message = None
                stale_task.finished_at = now_utc()
                stale_task.result_json = {
                    "block_id": block.id,
                    "renderer": "katex",
                    "confidence": 1.0,
                    "applied": True,
                    "applied_kind": "corrected-inline-latex",
                    "repair_mode": "deterministic-katex",
                    "reconciled_by_task_id": task.id,
                }
                reconciled_formula_task_ids.append(stale_task.id)
        session.commit()
    return {
        "block_id": block_id,
        "renderer": renderer,
        "confidence": repaired["confidence"],
        "applied": True,
        "applied_kind": applied_kind,
        "repair_mode": "deterministic-katex" if deterministic_repair else "model-assisted",
        "superseded_render_task_ids": superseded_render_task_ids,
        "replacement_render_task_id": replacement_render_task_id,
        "reconciled_formula_task_ids": reconciled_formula_task_ids,
    }


def _discarded_entity_count(payload: object) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
        return 0
    count = 0
    for unit in payload["units"]:
        if not isinstance(unit, dict):
            continue
        validation = unit.get("entity_validation")
        if not isinstance(validation, dict):
            continue
        discarded = validation.get("discarded")
        if isinstance(discarded, int) and discarded > 0:
            count += discarded
    return count


def _copy_formula_render_assets(source_payload: object, translated_payload: object) -> object:
    """Attach already-rendered source formula assets to protected translation math nodes."""

    updated = translated_payload

    def visit(value: object) -> None:
        nonlocal updated
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        render_state = value.get("latex_render")
        latex = value.get("latex")
        if (
            value.get("type") in {"math", "equation"}
            and isinstance(latex, str)
            and isinstance(render_state, dict)
            and render_state.get("status") == "succeeded"
        ):
            updated = propagate_formula_latex_render(
                updated,
                math_node_id=(
                    str(value["math_node_id"])
                    if isinstance(value.get("math_node_id"), str)
                    else None
                ),
                latex=latex,
                render_metadata=render_state,
            )
        for child in value.values():
            visit(child)

    visit(source_payload)
    return updated


def _require_translation_task_scope(task: QueuedTask) -> TaskScope:
    if task.visibility not in {
        TaskVisibility.WORKSPACE_DERIVED,
        TaskVisibility.USER_PRIVATE,
    }:
        raise TaskHandlerError(
            "Translation tasks require workspace-derived or user-private visibility."
        )
    if task.workspace_id is None or task.requested_by_user_id is None:
        raise TaskHandlerError("Translation task ownership is incomplete.")
    try:
        scope = TaskScope(
            task.visibility,
            workspace_id=task.workspace_id,
            requested_by_user_id=task.requested_by_user_id,
            credential_id=task.credential_id,
        )
    except ValueError as error:
        raise TaskHandlerError("Translation task ownership is invalid.") from error
    if task.scope_key != scope.scope_key:
        raise TaskHandlerError("Translation task scope is inconsistent.")
    return scope


def _require_translation_prompt_visibility(
    translation: TranslationModel,
    *,
    task_scope: TaskScope,
) -> None:
    if translation.prompt_kind == "global":
        allowed = task_scope.visibility is TaskVisibility.WORKSPACE_DERIVED
    elif translation.prompt_kind == "custom":
        allowed = task_scope.visibility is TaskVisibility.USER_PRIVATE
    elif translation.prompt_kind == "paper":
        # New paper-wide prompt tasks are user-private. The workspace-derived form is accepted
        # only so already-queued rows classified by migration 0018 remain executable; requester
        # identity is still verified exactly below.
        allowed = task_scope.visibility in {
            TaskVisibility.WORKSPACE_DERIVED,
            TaskVisibility.USER_PRIVATE,
        }
    else:
        allowed = False
    if not allowed:
        raise TaskHandlerError("Translation prompt visibility is inconsistent with its task.")
    if (
        translation.prompt_kind != "global"
        and translation.requested_by_user_id != task_scope.requested_by_user_id
    ):
        raise TaskHandlerError("Private translation requester is inconsistent with its task.")


def _translation_task_visibility_predicate(task_scope: TaskScope):
    """Fail closed before a queued worker materializes a translation row.

    ``paper`` under WORKSPACE_DERIVED is the sole compatibility case for a narrowly scoped
    pre-M5 task; its requester must still match exactly. New custom/paper work is USER_PRIVATE.
    """

    if task_scope.visibility is TaskVisibility.WORKSPACE_DERIVED:
        return or_(
            TranslationModel.prompt_kind == "global",
            and_(
                TranslationModel.prompt_kind == "paper",
                TranslationModel.requested_by_user_id == task_scope.requested_by_user_id,
            ),
        )
    return and_(
        TranslationModel.prompt_kind.in_(["custom", "paper"]),
        TranslationModel.requested_by_user_id == task_scope.requested_by_user_id,
    )


def translate_block(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    translator: Translator | None,
) -> dict[str, Any]:
    task_scope = _require_translation_task_scope(task)
    if task.paper_id is None or translator is None:
        raise TranslationProviderError(
            "Translation requires a configured provider.",
            error_type="CONFIGURATION",
            error_code="translator_not_configured",
        )
    translation_id = str(task.payload.get("translation_id", ""))
    block_id = str(task.payload.get("block_id", ""))
    language = str(task.payload.get("language", "zh-CN"))
    expected_source_hash = str(task.payload.get("source_hash", ""))
    expected_input_hash = str(task.payload.get("input_hash", ""))
    if not translation_id or not block_id or not expected_input_hash:
        raise TaskHandlerError("Translation task payload is incomplete.")

    with session_factory() as session:
        if not translation_scope_has_library_access(
            session,
            paper_id=task.paper_id,
            task_scope=task_scope,
        ):
            raise TaskHandlerError("The translation paper is outside the task workspace.")
        translation_query = select(TranslationModel).where(
            TranslationModel.id == translation_id,
            TranslationModel.workspace_id == task_scope.workspace_id,
            _translation_task_visibility_predicate(task_scope),
        )
        translation = session.scalar(translation_query)
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
            )
        )
        if (
            translation is None
            or block is None
            or translation.block_id != block.id
            or translation.language != language
            or translation.source_hash != expected_source_hash
            or block.content_hash != expected_source_hash
            or translation.input_hash != expected_input_hash
        ):
            raise TaskHandlerError("The translation target changed or no longer exists.")
        _require_translation_prompt_visibility(translation, task_scope=task_scope)
        if translation.status == TranslationStatus.SUCCEEDED:
            return {
                "translation_id": translation.id,
                "block_id": block.id,
                "cache_hit": translation.cache_hit,
                "input_tokens": translation.input_tokens or 0,
                "output_tokens": translation.output_tokens or 0,
                "latency_ms": translation.latency_ms or 0,
                "discarded_entities": _discarded_entity_count(translation.translated_payload_json),
            }
        cached_query = select(TranslationModel).where(
            TranslationModel.workspace_id == task_scope.workspace_id,
            TranslationModel.input_hash == expected_input_hash,
            TranslationModel.prompt_kind == translation.prompt_kind,
            TranslationModel.status == TranslationStatus.SUCCEEDED,
            TranslationModel.id != translation.id,
        )
        if translation.prompt_kind != "global":
            cached_query = cached_query.where(
                TranslationModel.requested_by_user_id == task_scope.requested_by_user_id
            )
        cached = session.scalar(cached_query.order_by(TranslationModel.completed_at).limit(1))
        if cached is not None:
            translation.status = TranslationStatus.SUCCEEDED
            translation.translated_text = cached.translated_text
            translation.translated_payload_json = _copy_formula_render_assets(
                block.render_payload_json,
                cached.translated_payload_json,
            )
            translation.cache_hit = True
            translation.cache_source_translation_id = cached.id
            translation.input_tokens = 0
            translation.output_tokens = 0
            translation.latency_ms = 0
            translation.completed_at = now_utc()
            translation.error_type = None
            translation.error_code = None
            translation.error_message = None
            session.commit()
            return {
                "translation_id": translation.id,
                "block_id": block.id,
                "cache_hit": True,
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": 0,
                "discarded_entities": _discarded_entity_count(cached.translated_payload_json),
            }
        spec = translation_spec_for_block(
            session,
            block_id=block_id,
            language=language,
            provider=translation.provider,
            model=translation.model,
            system_prompt=translation.system_prompt or DEFAULT_TRANSLATION_SYSTEM_PROMPT,
            prompt_version=translation.prompt_version,
        )
        if spec is None or spec.input_hash != expected_input_hash:
            raise TaskHandlerError("The translation context changed before execution.")
        translation.status = TranslationStatus.RUNNING
        translation.error_type = None
        translation.error_code = None
        translation.error_message = None
        session.commit()
        request = spec.request

    result = translator.translate(
        request,
        system_prompt=translation.system_prompt or DEFAULT_TRANSLATION_SYSTEM_PROMPT,
    )
    try:
        payload, flat_text = translated_payload(
            request.units,
            result.translations,
            result.entities,
        )
    except ValueError as error:
        message = str(error)
        math_placeholder_changed = "math placeholder" in message
        raise TranslationProviderError(
            message,
            error_type="INVALID_RESPONSE",
            error_code=(
                "math_placeholder_changed"
                if math_placeholder_changed
                else "translation_validation_failed"
            ),
            retryable=math_placeholder_changed,
        ) from error

    with session_factory() as session:
        if not translation_scope_has_library_access(
            session,
            paper_id=task.paper_id,
            task_scope=task_scope,
        ):
            raise TaskHandlerError(
                "The translation paper left the task workspace before publication."
            )
        translation_query = select(TranslationModel).where(
            TranslationModel.id == translation_id,
            TranslationModel.workspace_id == task_scope.workspace_id,
            _translation_task_visibility_predicate(task_scope),
        )
        translation = session.scalar(translation_query)
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == task.paper_id,
            )
        )
        if (
            translation is None
            or block is None
            or translation.block_id != block.id
            or translation.language != language
            or translation.source_hash != expected_source_hash
            or block.content_hash != expected_source_hash
            or translation.input_hash != expected_input_hash
        ):
            raise TaskHandlerError("The translation target changed before it could be saved.")
        _require_translation_prompt_visibility(translation, task_scope=task_scope)
        translation.status = TranslationStatus.SUCCEEDED
        translation.translated_text = flat_text
        translation.translated_payload_json = _copy_formula_render_assets(
            block.render_payload_json,
            payload,
        )
        translation.cache_hit = False
        translation.input_tokens = result.usage.input_tokens
        translation.output_tokens = result.usage.output_tokens
        translation.latency_ms = result.latency_ms
        translation.provider_request_id = result.provider_request_id
        translation.error_type = None
        translation.error_code = None
        translation.error_message = None
        translation.completed_at = now_utc()
        session.commit()
    return {
        "translation_id": translation_id,
        "block_id": block_id,
        "cache_hit": False,
        "input_tokens": result.usage.input_tokens or 0,
        "output_tokens": result.usage.output_tokens or 0,
        "latency_ms": result.latency_ms or 0,
        "discarded_entities": _discarded_entity_count(payload),
    }


def mark_translation_failure(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    error: Exception,
    *,
    will_retry: bool,
) -> None:
    try:
        task_scope = _require_translation_task_scope(task)
    except TaskHandlerError:
        return
    if task.paper_id is None:
        return
    translation_id = str(task.payload.get("translation_id", ""))
    with session_factory() as session:
        if not translation_scope_has_library_access(
            session,
            paper_id=task.paper_id,
            task_scope=task_scope,
        ):
            return
        translation_query = (
            select(TranslationModel)
            .join(ContentBlockModel, ContentBlockModel.id == TranslationModel.block_id)
            .where(
                TranslationModel.id == translation_id,
                TranslationModel.workspace_id == task_scope.workspace_id,
                ContentBlockModel.paper_id == task.paper_id,
                _translation_task_visibility_predicate(task_scope),
            )
        )
        translation = session.scalar(translation_query)
        if translation is None:
            return
        try:
            _require_translation_prompt_visibility(translation, task_scope=task_scope)
        except TaskHandlerError:
            return
        translation.status = TranslationStatus.PENDING if will_retry else TranslationStatus.FAILED
        translation.error_type = getattr(error, "error_type", type(error).__name__)[:64]
        provider_code = getattr(error, "error_code", None)
        translation.error_code = str(provider_code)[:128] if provider_code else None
        translation.error_message = redact_task_error_message(error, max_chars=2_000)
        provider_request_id = getattr(error, "provider_request_id", None)
        if provider_request_id:
            translation.provider_request_id = str(provider_request_id)[:255]
        if not will_retry:
            translation.completed_at = now_utc()
            paper = session.get(PaperModel, task.paper_id)
            if paper is not None:
                paper.processing_health = ProcessingHealth.PARTIAL
        session.commit()


def refresh_paper_health(
    paper_id: str | None,
    session_factory: sessionmaker[Session],
) -> None:
    if paper_id is None:
        return
    with session_factory() as session:
        paper = session.get(PaperModel, paper_id)
        if paper is None:
            return
        failed_count = session.scalar(
            select(func.count())
            .select_from(ProcessingTaskModel)
            .where(
                ProcessingTaskModel.paper_id == paper_id,
                ProcessingTaskModel.status == TaskStatus.FAILED,
                ProcessingTaskModel.task_type != ANSWER_QUESTION,
            )
        )
        pdf_source_count = session.scalar(
            select(func.count())
            .select_from(PaperSourceModel)
            .where(
                PaperSourceModel.paper_id == paper_id,
                PaperSourceModel.source_type == "ARXIV_PDF",
            )
        )
        expanded_source_count = session.scalar(
            select(func.count())
            .select_from(PaperSourceModel)
            .where(
                PaperSourceModel.paper_id == paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
        pdf_only = bool(pdf_source_count) and not bool(expanded_source_count)
        paper.processing_health = (
            ProcessingHealth.PARTIAL if failed_count or pdf_only else ProcessingHealth.OK
        )
        session.commit()


def _validate_qa_citations(
    session: Session,
    *,
    paper_id: str,
    workspace_id: str,
    requested_by_user_id: str,
    target: str,
    citations: tuple[QACitation, ...],
    allowed_contexts: set[tuple[str, str, str | None]],
) -> list[dict[str, Any]]:
    if target != AnnotationTarget.PAPER and not citations:
        raise QAProviderError(
            "Q&A response did not cite the supplied paper context.",
            error_type="INVALID_RESPONSE",
            error_code="citations_required",
        )
    validated: list[dict[str, Any]] = []
    for citation in citations:
        identity = (citation.block_id, citation.target, citation.translation_id)
        if identity not in allowed_contexts:
            raise QAProviderError(
                "Q&A response cited context that was not supplied.",
                error_type="INVALID_RESPONSE",
                error_code="citation_outside_context",
            )
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == citation.block_id,
                ContentBlockModel.paper_id == paper_id,
            )
        )
        if block is None:
            raise QAProviderError(
                "Q&A response cited an unknown block.",
                error_type="INVALID_RESPONSE",
                error_code="citation_block_invalid",
            )
        text = block.source_text or ""
        if citation.target == AnnotationTarget.TRANSLATION:
            translation = None
            if citation.translation_id:
                translation = session.scalar(
                    select(TranslationModel).where(
                        TranslationModel.id == citation.translation_id,
                        TranslationModel.workspace_id == workspace_id,
                        or_(
                            TranslationModel.prompt_kind == "global",
                            and_(
                                TranslationModel.prompt_kind.in_(["custom", "paper"]),
                                TranslationModel.requested_by_user_id
                                == requested_by_user_id,
                            ),
                        ),
                    )
                )
            if translation is None or translation.block_id != block.id:
                raise QAProviderError(
                    "Q&A response cited an unknown translation revision.",
                    error_type="INVALID_RESPONSE",
                    error_code="citation_translation_invalid",
                )
            text = translation.translated_text or ""
        canonical_quote = resolve_canonical_quote(text, citation.quote)
        if canonical_quote is None:
            raise QAProviderError(
                "Q&A response citation quote was not found in the cited text.",
                error_type="INVALID_RESPONSE",
                error_code="citation_quote_invalid",
            )
        validated.append(
            {
                "block_id": block.id,
                "target": citation.target,
                "quote": canonical_quote,
                "translation_id": citation.translation_id,
            }
        )
    return validated


def answer_question(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    answerer: QuestionAnswerer | None,
    *,
    context_token_budget: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    if task.visibility is not TaskVisibility.USER_PRIVATE:
        raise TaskHandlerError("Q&A tasks require user-private visibility.")
    if task.workspace_id is None or task.requested_by_user_id is None:
        raise TaskHandlerError("Q&A task ownership is incomplete.")
    expected_scope = TaskScope.user_private(
        workspace_id=task.workspace_id,
        requested_by_user_id=task.requested_by_user_id,
        credential_id=task.credential_id,
    )
    if task.scope_key != expected_scope.scope_key:
        raise TaskHandlerError("Q&A task scope is inconsistent.")
    if task.paper_id is None or answerer is None:
        raise QAProviderError(
            "Paper-aware Q&A requires a configured provider.",
            error_type="CONFIGURATION",
            error_code="qa_not_configured",
        )
    question_id = str(task.payload.get("question_id", ""))
    generation = int(task.payload.get("generation", 0))
    if not question_id or generation < 1:
        raise TaskHandlerError("Q&A task payload is incomplete.")
    with session_factory() as session:
        requester_membership = session.scalar(
            select(WorkspaceMembershipModel.user_id)
            .join(UserModel, UserModel.id == WorkspaceMembershipModel.user_id)
            .where(
                WorkspaceMembershipModel.workspace_id == task.workspace_id,
                WorkspaceMembershipModel.user_id == task.requested_by_user_id,
                UserModel.status == "ACTIVE",
            )
        )
        question = session.scalar(
            select(AnnotationModel).where(
                AnnotationModel.id == question_id,
                AnnotationModel.workspace_id == task.workspace_id,
                AnnotationModel.creator_user_id == task.requested_by_user_id,
            )
        )
        library_item_id = session.scalar(
            select(LibraryItemModel.id).where(
                LibraryItemModel.workspace_id == task.workspace_id,
                LibraryItemModel.paper_id == task.paper_id,
            )
        )
        if (
            requester_membership is None
            or question is None
            or library_item_id is None
            or question.paper_id != task.paper_id
            or question.annotation_type != AnnotationType.AI_QUESTION
        ):
            raise TaskHandlerError("The Q&A question no longer exists.")
        if question.deleted_at is not None:
            return {"question_id": question.id, "generation": generation, "cancelled": True}
        existing = session.scalar(
            select(AnnotationModel).where(
                AnnotationModel.parent_annotation_id == question.id,
                AnnotationModel.workspace_id == task.workspace_id,
                AnnotationModel.annotation_type == AnnotationType.AI_RESPONSE,
                AnnotationModel.qa_generation == generation,
            )
        )
        if existing is not None:
            return {
                "question_id": question.id,
                "response_id": existing.id,
                "generation": generation,
                "input_tokens": existing.input_tokens or 0,
                "output_tokens": existing.output_tokens or 0,
                "latency_ms": existing.latency_ms or 0,
                "idempotent_replay": True,
            }
        if generation != question.qa_generation:
            raise TaskHandlerError("The Q&A generation is no longer current.")
        question.qa_status = QAStatus.RUNNING
        question.qa_error_type = None
        question.qa_error_code = None
        question.qa_error_message = None
        session.commit()
        qa_request = build_qa_request(
            session,
            question,
            workspace_id=task.workspace_id,
            requested_by_user_id=task.requested_by_user_id,
            token_budget=context_token_budget,
            max_output_tokens=max_output_tokens,
        )
        question.qa_context_json = {
            "version": 1,
            "generation": generation,
            "prompt_version": QA_PROMPT_VERSION,
            "prompt_hash": qa_prompt_hash(),
            "context_token_budget": context_token_budget,
            "max_output_tokens": max_output_tokens,
            "user_payload": qa_user_payload(qa_request),
            "captured_at": now_utc().isoformat(),
        }
        session.commit()

    result = answerer.answer(qa_request, system_prompt=QA_SYSTEM_PROMPT)
    allowed_contexts = {
        (item.block_id, item.target, item.translation_id)
        for item in qa_request.context
        if item.block_id and item.target in {AnnotationTarget.SOURCE, AnnotationTarget.TRANSLATION}
    }
    with session_factory() as session:
        requester_membership = session.scalar(
            select(WorkspaceMembershipModel.user_id)
            .join(UserModel, UserModel.id == WorkspaceMembershipModel.user_id)
            .where(
                WorkspaceMembershipModel.workspace_id == task.workspace_id,
                WorkspaceMembershipModel.user_id == task.requested_by_user_id,
                UserModel.status == "ACTIVE",
            )
        )
        question = session.scalar(
            select(AnnotationModel).where(
                AnnotationModel.id == question_id,
                AnnotationModel.workspace_id == task.workspace_id,
                AnnotationModel.creator_user_id == task.requested_by_user_id,
            )
        )
        library_item_id = session.scalar(
            select(LibraryItemModel.id).where(
                LibraryItemModel.workspace_id == task.workspace_id,
                LibraryItemModel.paper_id == task.paper_id,
            )
        )
        if (
            requester_membership is None
            or question is None
            or library_item_id is None
            or question.deleted_at is not None
        ):
            return {"question_id": question_id, "generation": generation, "cancelled": True}
        existing = session.scalar(
            select(AnnotationModel).where(
                AnnotationModel.parent_annotation_id == question.id,
                AnnotationModel.workspace_id == task.workspace_id,
                AnnotationModel.annotation_type == AnnotationType.AI_RESPONSE,
                AnnotationModel.qa_generation == generation,
            )
        )
        if existing is not None:
            return {
                "question_id": question.id,
                "response_id": existing.id,
                "generation": generation,
                "idempotent_replay": True,
            }
        citations = _validate_qa_citations(
            session,
            paper_id=question.paper_id,
            workspace_id=task.workspace_id,
            requested_by_user_id=task.requested_by_user_id,
            target=question.target,
            citations=result.citations,
            allowed_contexts=allowed_contexts,
        )
        response = AnnotationModel(
            workspace_id=task.workspace_id,
            creator_user_id=task.requested_by_user_id,
            paper_id=question.paper_id,
            block_id=question.block_id,
            parent_annotation_id=question.id,
            annotation_type=AnnotationType.AI_RESPONSE,
            target=question.target,
            body=result.answer,
            creator_type="AI",
            selector_json=question.selector_json,
            target_translation_id=question.target_translation_id,
            target_translation_input_hash=question.target_translation_input_hash,
            resolution_status=question.resolution_status,
            resolved_start_offset=question.resolved_start_offset,
            resolved_end_offset=question.resolved_end_offset,
            resolved_at=now_utc(),
            qa_generation=generation,
            citations_json=citations,
            provider=answerer.provider,
            model=answerer.model,
            prompt_version=QA_PROMPT_VERSION,
            prompt_hash=qa_prompt_hash(),
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            latency_ms=result.latency_ms,
            provider_request_id=result.provider_request_id,
        )
        session.add(response)
        question.qa_status = QAStatus.SUCCEEDED
        question.qa_error_type = None
        question.qa_error_code = None
        question.qa_error_message = None
        session.commit()
        session.refresh(response)
        return {
            "question_id": question.id,
            "response_id": response.id,
            "generation": generation,
            "input_tokens": result.usage.input_tokens or 0,
            "output_tokens": result.usage.output_tokens or 0,
            "latency_ms": result.latency_ms or 0,
            "citations": len(citations),
            "idempotent_replay": False,
        }


def mark_qa_failure(
    task: QueuedTask,
    session_factory: sessionmaker[Session],
    error: Exception,
    *,
    will_retry: bool,
) -> None:
    if (
        task.visibility is not TaskVisibility.USER_PRIVATE
        or task.workspace_id is None
        or task.requested_by_user_id is None
    ):
        return
    expected_scope = TaskScope.user_private(
        workspace_id=task.workspace_id,
        requested_by_user_id=task.requested_by_user_id,
        credential_id=task.credential_id,
    )
    if task.scope_key != expected_scope.scope_key:
        return
    question_id = str(task.payload.get("question_id", ""))
    generation = int(task.payload.get("generation", 0))
    with session_factory() as session:
        question = session.scalar(
            select(AnnotationModel).where(
                AnnotationModel.id == question_id,
                AnnotationModel.workspace_id == task.workspace_id,
                AnnotationModel.creator_user_id == task.requested_by_user_id,
            )
        )
        if question is None or question.deleted_at is not None:
            return
        if question.qa_generation != generation:
            return
        question.qa_status = QAStatus.QUEUED if will_retry else QAStatus.FAILED
        question.qa_error_type = getattr(error, "error_type", type(error).__name__)[:64]
        code = getattr(error, "error_code", None)
        question.qa_error_code = str(code)[:128] if code else None
        question.qa_error_message = redact_task_error_message(error, max_chars=2_000)
        provider_request_id = getattr(error, "provider_request_id", None)
        if provider_request_id:
            question.provider_request_id = str(provider_request_id)[:255]
        session.commit()


def handle_task(
    task: QueuedTask,
    client: ArxivClient,
    session_factory: sessionmaker[Session],
    storage: Storage,
    repairer: RenderRepairClient | None = None,
    table_renderer: LatexTableRenderer | None = None,
    figure_renderer: LatexFigureRenderer | None = None,
    translator: Translator | None = None,
    answerer: QuestionAnswerer | None = None,
    paper_map_generator: PaperMapGenerator | None = None,
    qa_context_token_budget: int = 12_000,
    qa_max_output_tokens: int = 2_000,
    paper_map_context_token_budget: int = 30_000,
    paper_map_max_output_tokens: int = 6_000,
    paper_map_answer_max_attempts: int = 4,
    toolchain_commands: ToolchainCommands | None = None,
    toolchain_fingerprint: str | None = None,
) -> dict[str, Any]:
    authorize_task_scope(task)
    if task.task_type == FETCH_METADATA:
        return fetch_metadata(task, client, session_factory)
    if task.task_type == FETCH_SOURCE:
        return fetch_source(
            task,
            client,
            session_factory,
            storage,
            toolchain_commands=toolchain_commands,
        )
    if task.task_type == PARSE_SOURCE:
        return parse_source(
            task,
            session_factory,
            storage,
            toolchain_commands=toolchain_commands,
            toolchain_fingerprint=toolchain_fingerprint,
        )
    if task.task_type == RENDER_TABLE:
        return render_table(
            task,
            session_factory,
            storage,
            table_renderer,
            toolchain_fingerprint=toolchain_fingerprint,
        )
    if task.task_type == RENDER_FIGURE:
        return render_figure(
            task,
            session_factory,
            storage,
            figure_renderer,
            toolchain_fingerprint=toolchain_fingerprint,
        )
    if task.task_type == RENDER_LATEX_BLOCK:
        return render_latex_block(
            task,
            session_factory,
            storage,
            table_renderer,
            toolchain_fingerprint=toolchain_fingerprint,
        )
    if task.task_type == RENDER_LATEX_FORMULA:
        return render_latex_formula(
            task,
            session_factory,
            storage,
            figure_renderer,
            toolchain_fingerprint=toolchain_fingerprint,
        )
    if task.task_type == REPAIR_RENDER:
        return repair_render(
            task,
            session_factory,
            storage,
            repairer,
            toolchain_fingerprint=toolchain_fingerprint,
        )
    if task.task_type == TRANSLATE_BLOCK:
        return translate_block(task, session_factory, translator)
    if task.task_type == ANSWER_QUESTION:
        return answer_question(
            task,
            session_factory,
            answerer,
            context_token_budget=qa_context_token_budget,
            max_output_tokens=qa_max_output_tokens,
        )
    if task.task_type == PLAN_PAPER_MAP:
        return plan_paper_map(
            task,
            session_factory,
            paper_map_generator,
            context_token_budget=paper_map_context_token_budget,
            max_output_tokens=paper_map_max_output_tokens,
            answer_max_attempts=paper_map_answer_max_attempts,
        )
    if task.task_type == ANSWER_MAP_QUESTION:
        return answer_map_question(
            task,
            session_factory,
            paper_map_generator,
            context_token_budget=paper_map_context_token_budget,
            max_output_tokens=paper_map_max_output_tokens,
        )
    raise TaskHandlerError(f"Unsupported task type: {task.task_type}")


def mark_paper_failure(
    paper_id: str | None,
    session_factory: sessionmaker[Session],
    *,
    will_retry: bool,
) -> None:
    if paper_id is None:
        return
    with session_factory() as session:
        paper = session.get(PaperModel, paper_id)
        if paper is not None:
            paper.processing_health = (
                ProcessingHealth.PARTIAL if will_retry else ProcessingHealth.FAILED
            )
            session.commit()
