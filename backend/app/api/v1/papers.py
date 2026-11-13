from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload, with_loader_criteria

from app.api.byte_ranges import storage_byte_response
from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.llm_credentials import request_llm_task_credential
from app.application.access import WorkspacePaper, load_workspace_paper
from app.application.libraries import (
    LibraryError,
    ensure_default_library,
    import_into_library,
    library_access_predicate,
    library_item_access_predicate,
)
from app.application.paper_maps import (
    ANSWER_MAP_QUESTION,
    PAPER_MAP_ANSWER_SYSTEM_PROMPT,
    PAPER_MAP_PLAN_SYSTEM_PROMPT,
    PLAN_PAPER_MAP,
)
from app.application.qa import ANSWER_QUESTION
from app.application.render_overlays import (
    effective_block_render,
    workspace_render_asset_key,
)
from app.application.render_repairs import (
    FormulaSelectorError,
    formula_repair_metadata,
    formula_selector_key,
    formula_text_hash,
    hydrate_inline_latex_render_payload,
    selected_formula,
)
from app.application.settings import (
    TRANSLATION_SYSTEM_PROMPT_MAX_LENGTH,
    compose_translation_system_prompt,
    get_automatic_ai_repair_enabled,
    get_translation_system_prompt,
    validate_translation_system_prompt,
)
from app.application.translations import (
    TRANSLATE_BLOCK,
    abstract_block_id,
    create_custom_retranslation,
    create_paper_retranslation,
    ensure_abstract_block,
    retry_translation,
    schedule_remaining_paper_translations,
    translation_task_scope_for_base_prompt,
)
from app.domain.arxiv import InvalidArxivIdentifier
from app.domain.auth import Permission, RequestPrincipal
from app.domain.enums import ProcessingHealth, TaskStatus, TranslationStatus
from app.domain.os_adapters import toolchain_cache_identity
from app.domain.tasks import (
    TaskScope,
    TaskSecretError,
    TaskVisibility,
    require_secret_free_task_data,
)
from app.domain.translation import TRANSLATION_PROMPT_VERSION, extract_translation_units
from app.infrastructure.db.library_models import LibraryEntryModel, LibraryModel
from app.infrastructure.db.models import (
    BibliographyEntryModel,
    BlockBookmarkModel,
    ContentBlockModel,
    LibraryItemModel,
    PaperBibliographyModel,
    PaperMapModel,
    PaperMapQuestionModel,
    PaperModel,
    PaperSourceModel,
    ProcessingTaskModel,
    TranslationModel,
    now_utc,
)
from app.infrastructure.latex import (
    ARXIV_PDF_ASSET_PATH,
    KATEX_NORMALIZER_VERSION,
    normalize_katex_latex,
)
from app.infrastructure.llm import (
    DEFAULT_TRANSLATION_SYSTEM_PROMPT,
    RENDER_REPAIR_MAX_LATEX_CHARS,
    RENDER_REPAIR_SYSTEM_PROMPT,
)
from app.workers.handlers import (
    FETCH_METADATA,
    FETCH_SOURCE,
    LATEX_FORMULA_RENDERER_VERSION,
    PARSE_SOURCE,
    RENDER_FIGURE,
    RENDER_LATEX_BLOCK,
    RENDER_LATEX_FORMULA,
    RENDER_TABLE,
    REPAIR_RENDER,
)

router = APIRouter(
    prefix="/papers",
    tags=["papers"],
    dependencies=[Depends(resolve_request_principal)],
)

_DOWNLOADABLE_SOURCE_TYPES = {"ARXIV_TARBALL", "ARXIV_PDF", "EXPANDED_SOURCE"}
_SOURCE_STORAGE_RELATIVE_KEYS = {
    # `source/source.tar.gz` is the exact archive key frozen in the pre-M5 SQLite/storage
    # characterization fixture. Keep it readable in place: changing only the database key would
    # make an otherwise successful tenant upgrade lose access to the user's source bytes. This is
    # an explicit alias, not a prefix allowance.
    "ARXIV_TARBALL": frozenset(
        {
            "source/arxiv-source.tar",
            "source/source.tar.gz",
        }
    ),
    "ARXIV_PDF": frozenset({f"source/{ARXIV_PDF_ASSET_PATH}"}),
    "EXPANDED_SOURCE": frozenset({"derived/source-manifest.json"}),
}
_EXPANDED_SOURCE_DOWNLOAD_MAX_FILES = 2_000
_EXPANDED_SOURCE_DOWNLOAD_MAX_BYTES = 250_000_000
_AUTOMATIC_INLINE_REPAIR_QUEUE_LIMIT = 256
_SOURCE_SVG_CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def _has_owned_source_storage_key(
    source: PaperSourceModel,
    *,
    paper_id: str,
) -> bool:
    relative_keys = _SOURCE_STORAGE_RELATIVE_KEYS.get(source.source_type)
    if relative_keys is None:
        return False
    return source.storage_key in {
        f"papers/{paper_id}/{relative_key}" for relative_key in relative_keys
    }


class ImportPaperRequest(BaseModel):
    source: str = Field(min_length=1, max_length=512)


class AuthorResponse(BaseModel):
    position: int
    name: str


class SourceResponse(BaseModel):
    source_type: str
    storage_key: str
    sha256: str
    byte_size: int
    mime_type: str | None
    retrieved_at: datetime


class TaskDownloadProgressResponse(BaseModel):
    phase: str
    bytes_downloaded: int
    bytes_total: int | None
    bytes_remaining: int | None
    percent: float | None
    resumable: bool
    resumed_bytes: int
    updated_at: datetime | None


class TaskResponse(BaseModel):
    id: str
    task_type: str
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_message: str | None
    download_progress: TaskDownloadProgressResponse | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class PaperResponse(BaseModel):
    id: str
    arxiv_id: str
    arxiv_version: str
    canonical_arxiv_id: str
    title: str
    abstract: str | None
    abstract_block_id: str | None = None
    abstract_render_payload: dict[str, object] | None = None
    submitted_at: datetime | None
    processing_stage: str
    processing_health: str
    reading_status: str
    is_favorite: bool
    pinned_at: datetime | None
    last_opened_at: datetime | None
    created_at: datetime
    updated_at: datetime
    authors: list[AuthorResponse]
    sources: list[SourceResponse]
    tasks: list[TaskResponse]


class ImportPaperResponse(BaseModel):
    created: bool
    paper: PaperResponse


class PaperPreferencesRequest(BaseModel):
    is_favorite: bool | None = None
    is_pinned: bool | None = None


class BlockBookmarkResponse(BaseModel):
    id: str
    paper_id: str
    block_id: str
    block_type: str
    ordinal_path: str
    section_title: str | None
    section_display_number: str | None
    preview: str
    created_at: datetime


class RetryResponse(BaseModel):
    retried_tasks: int
    paper: PaperResponse


class DeletePapersRequest(BaseModel):
    paper_ids: list[str] = Field(min_length=1, max_length=100)


class DeletePapersResponse(BaseModel):
    deleted_count: int
    deleted_ids: list[str]
    deleted_files: int


class OutlineItemResponse(BaseModel):
    id: str
    parent_id: str | None
    ordinal_path: str
    heading_level: int
    title: str
    display_number: str | None = None
    title_segments: list[dict] = Field(default_factory=list)


class ContentBlockResponse(BaseModel):
    id: str
    parent_id: str | None
    block_type: str
    ordinal_path: str
    heading_level: int | None
    source_text: str | None
    source_payload: dict
    render_payload: dict
    source_locator: dict
    translation: TranslationResponse | None = None


class BibliographyPersonResponse(BaseModel):
    role: str
    position: int
    display_name: str
    given_names: list[str]
    family_names: list[str]
    prefix_names: list[str]
    suffix_names: list[str]
    initials: list[str]


class BibliographyEntryResponse(BaseModel):
    id: str
    citation_key: str
    entry_type: str
    source_path: str | None
    fields: dict
    title: str | None
    container_title: str | None
    publication_year: str | None
    doi: str | None
    url: str | None
    eprint: str | None
    archive_prefix: str | None
    isbn: str | None
    issn: str | None
    ordinal: int | None
    display_label: str | None
    formatted_text: str | None
    render_payload: dict
    persons: list[BibliographyPersonResponse]


class PaperBibliographyResponse(BaseModel):
    status: str
    requested_style: str | None
    resolved_style: str | None
    fallback_used: bool
    processor_version: str | None
    source_files: list[str]
    warnings: list[str]
    entries: list[BibliographyEntryResponse]


class TranslationResponse(BaseModel):
    id: str
    block_id: str
    language: str
    status: str
    translated_text: str | None
    translated_payload: dict
    provider: str
    model: str
    prompt_version: str
    system_prompt: str | None
    prompt_kind: str
    target_type: str
    cache_hit: bool
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    estimated_cost_microusd: int | None
    provider_request_id: str | None
    error_type: str | None
    error_code: str | None
    error_message: str | None
    updated_at: datetime


class TranslationErrorResponse(BaseModel):
    block_id: str
    error_type: str | None
    error_code: str | None
    error_message: str | None
    attempt_count: int


class TranslationProgressResponse(BaseModel):
    configured: bool
    target_language: str
    provider: str | None
    model: str | None
    prompt_version: str
    eligible: int
    missing: int
    pending: int
    running: int
    succeeded: int
    failed: int
    cache_hits: int
    input_tokens: int
    output_tokens: int
    average_latency_ms: int | None
    retry_count: int
    recent_errors: list[TranslationErrorResponse]


class TranslationRetryResponse(BaseModel):
    task_id: str
    status: str


class TranslationRetranslateRequest(BaseModel):
    system_prompt: str | None = Field(default=None, max_length=TRANSLATION_SYSTEM_PROMPT_MAX_LENGTH)


class PaperRetranslationResponse(BaseModel):
    batch_id: str
    eligible: int
    queued: int
    superseded_tasks: int


class PaperRemainingTranslationResponse(BaseModel):
    eligible: int
    queued: int
    created: int
    retried: int
    cached: int
    preserved: int
    in_flight: int


class ContentBlockPageResponse(BaseModel):
    items: list[ContentBlockResponse]
    next_cursor: str | None


class InlineFormulaSelectorRequest(BaseModel):
    target: Literal["BLOCK", "TRANSLATION"] = "BLOCK"
    path: list[str | int] = Field(min_length=2, max_length=8)
    exact_latex: str = Field(min_length=1, max_length=16000)
    text_hash: str | None = Field(default=None, min_length=64, max_length=64)
    translation_id: str | None = Field(default=None, max_length=36)


class RenderFailureRequest(BaseModel):
    renderer: Literal["katex", "figure", "table", "latex"]
    error_message: str = Field(min_length=1, max_length=2000)
    formula_selector: InlineFormulaSelectorRequest | None = None
    automatic: bool = False


class LatexFormulaRenderRequest(BaseModel):
    error_message: str = Field(min_length=1, max_length=2000)
    formula_selector: InlineFormulaSelectorRequest | None = None


class RenderRepairTaskResponse(BaseModel):
    queued: bool
    applied: bool
    task_id: str
    status: str


class TaskDetailResponse(BaseModel):
    id: str
    task_type: str
    status: str
    description: str
    execution_kind: Literal["network", "local", "llm"]
    uses_llm: bool = False
    engine: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    renderer: str | None = None
    renderer_error: str | None = None
    target_block_id: str | None = None
    target_block_type: str | None = None
    target_location: str | None = None
    trigger_reason: str | None = None
    complexity_reasons: list[str] = Field(default_factory=list)
    result_summary: str | None = None
    safety_notes: list[str] = Field(default_factory=list)
    system_prompt: str | None = None
    source_latex: str | None = None
    source_text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    estimated_cost_microusd: int | None = None
    cache_hit: bool | None = None
    task_error_code: str | None = None
    task_error_message: str | None = None


_TASK_DESCRIPTIONS = {
    FETCH_METADATA: "Fetches title, authors, abstract, and publication metadata from arXiv.",
    FETCH_SOURCE: "Downloads and safely expands the paper source archive from arXiv.",
    PARSE_SOURCE: ("Discovers the LaTeX document structure and builds renderable content blocks."),
    RENDER_FIGURE: (
        "Compiles one self-contained LaTeX figure locally and converts it to a cached SVG. "
        "No LLM or external rendering service is used."
    ),
    RENDER_LATEX_BLOCK: (
        "Compiles one layout-sensitive algorithm or author-defined visual environment locally "
        "and converts it to a cached image without rewriting canonical LaTeX."
    ),
    RENDER_TABLE: (
        "Compiles one LaTeX table locally and converts it to a cached render image. "
        "No LLM or external rendering service is used."
    ),
    RENDER_LATEX_FORMULA: (
        "Compiles one exact formula with the paper's recorded preamble and TeX Live, then "
        "caches an SVG. No LLM or semantic rewriting is used."
    ),
    REPAIR_RENDER: (
        "Uses the configured LLM to repair one block that the browser renderer could not display. "
        "The validated result updates only the rebuildable render payload; source LaTeX is kept."
    ),
    TRANSLATE_BLOCK: (
        "Translates one content block into Simplified Chinese with versioned context and cache."
    ),
    PLAN_PAPER_MAP: (
        "Classifies one paper and selects a versioned set of visual-map reading questions."
    ),
    ANSWER_MAP_QUESTION: (
        "Answers one visual-map question with server-verified canonical source citations."
    ),
}

_TASK_EXECUTION = {
    FETCH_METADATA: ("network", "arXiv metadata API"),
    FETCH_SOURCE: ("network", "arXiv source archive download"),
    PARSE_SOURCE: ("local", "AcadematePaper structural LaTeX parser"),
    RENDER_LATEX_FORMULA: ("local", "TeX Live + dvisvgm formula renderer"),
    RENDER_LATEX_BLOCK: ("local", "TeX Live native block renderer"),
    TRANSLATE_BLOCK: ("llm", "Configured block translator"),
    PLAN_PAPER_MAP: ("llm", "Configured paper-map generator"),
    ANSWER_MAP_QUESTION: ("llm", "Configured paper-map generator"),
}


def _translation_response(
    translation: TranslationModel,
    principal: RequestPrincipal,
) -> TranslationResponse:
    return TranslationResponse(
        id=translation.id,
        block_id=translation.block_id,
        language=translation.language,
        status=translation.status,
        translated_text=translation.translated_text,
        translated_payload=translation.translated_payload_json,
        provider=translation.provider,
        model=translation.model,
        prompt_version=translation.prompt_version,
        system_prompt=(
            translation.system_prompt or DEFAULT_TRANSLATION_SYSTEM_PROMPT
            if translation.requested_by_user_id == principal.user_id
            else None
        ),
        prompt_kind=translation.prompt_kind,
        target_type=translation.block.block_type,
        cache_hit=translation.cache_hit,
        input_tokens=translation.input_tokens,
        output_tokens=translation.output_tokens,
        latency_ms=translation.latency_ms,
        estimated_cost_microusd=translation.estimated_cost_microusd,
        provider_request_id=translation.provider_request_id,
        error_type=translation.error_type,
        error_code=translation.error_code,
        error_message=translation.error_message,
        updated_at=translation.updated_at,
    )


def _current_translation(
    block: ContentBlockModel,
    settings,
    principal: RequestPrincipal,
) -> TranslationModel | None:
    candidates = [
        item
        for item in block.translations
        if item.source_hash == block.content_hash
        and item.language == settings.translation_target_language
        and item.workspace_id == principal.active_workspace_id
        and (
            item.prompt_kind == "global"
            or (
                item.prompt_kind in {"custom", "paper"}
                and item.requested_by_user_id == principal.user_id
            )
        )
    ]
    if settings.translation_configured:
        configured = [
            item
            for item in candidates
            if item.provider == settings.llm_provider and item.model == settings.llm_model
        ]
        if configured:
            candidates = configured
    return max(candidates, key=lambda item: item.created_at, default=None)


def _task_block(
    session,
    *,
    paper_id: str,
    payload: dict,
) -> ContentBlockModel | None:
    block_id = payload.get("block_id")
    if not isinstance(block_id, str) or not block_id:
        return None
    return session.scalar(
        select(ContentBlockModel).where(
            ContentBlockModel.id == block_id,
            ContentBlockModel.paper_id == paper_id,
        )
    )


def _block_source_latex(
    block: ContentBlockModel | None,
    *,
    max_chars: int | None = 16_000,
) -> str | None:
    if block is None:
        return None
    candidate = block.source_payload_json.get("latex") or block.source_text
    if isinstance(candidate, str) and candidate.strip():
        return candidate if max_chars is None else candidate[:max_chars]
    return None


def _block_location(block: ContentBlockModel | None) -> str | None:
    if block is None:
        return None
    root_file = block.source_locator_json.get("root_file")
    line = block.source_locator_json.get("expanded_line")
    if not isinstance(root_file, str) or not root_file:
        return None
    return f"{root_file}:{line}" if isinstance(line, int) else root_file


def _display_bytes(value: object) -> str | None:
    if not isinstance(value, int) or value < 0:
        return None
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def _options(principal: RequestPrincipal):
    options = [
        selectinload(PaperModel.authors),
        selectinload(PaperModel.sources),
    ]
    if principal.has(Permission.TASK_READ):
        options.extend(
            [
                selectinload(PaperModel.tasks),
                with_loader_criteria(
                    ProcessingTaskModel,
                    _task_visibility_predicate(principal),
                    include_aliases=True,
                ),
            ]
        )
    return tuple(options)


def _task_visible_to_principal(
    task: ProcessingTaskModel,
    principal: RequestPrincipal,
) -> bool:
    if task.visibility == TaskVisibility.GLOBAL_DERIVED:
        return True
    if task.workspace_id != principal.active_workspace_id:
        return False
    if task.visibility == TaskVisibility.WORKSPACE_DERIVED:
        return True
    return (
        task.visibility == TaskVisibility.USER_PRIVATE
        and task.requested_by_user_id == principal.user_id
    )


def _task_visibility_predicate(principal: RequestPrincipal):
    return or_(
        ProcessingTaskModel.visibility == TaskVisibility.GLOBAL_DERIVED,
        and_(
            ProcessingTaskModel.visibility == TaskVisibility.WORKSPACE_DERIVED,
            ProcessingTaskModel.workspace_id == principal.active_workspace_id,
        ),
        and_(
            ProcessingTaskModel.visibility == TaskVisibility.USER_PRIVATE,
            ProcessingTaskModel.workspace_id == principal.active_workspace_id,
            ProcessingTaskModel.requested_by_user_id == principal.user_id,
        ),
    )


def _translation_visibility_predicate(principal: RequestPrincipal):
    return and_(
        TranslationModel.workspace_id == principal.active_workspace_id,
        or_(
            TranslationModel.prompt_kind == "global",
            and_(
                TranslationModel.prompt_kind.in_(["custom", "paper"]),
                TranslationModel.requested_by_user_id == principal.user_id,
            ),
        ),
    )


def _formula_render_scope(
    principal: RequestPrincipal,
    translation: TranslationModel | None,
    *,
    workspace_overlay: bool = False,
) -> TaskScope:
    if workspace_overlay:
        return TaskScope.workspace_derived(
            workspace_id=principal.active_workspace_id,
            requested_by_user_id=principal.user_id,
        )
    if translation is None:
        return TaskScope.global_derived()
    if translation.prompt_kind in {"custom", "paper"}:
        return TaskScope.user_private(
            workspace_id=principal.active_workspace_id,
            requested_by_user_id=principal.user_id,
        )
    if translation.prompt_kind == "global":
        return TaskScope.workspace_derived(
            workspace_id=principal.active_workspace_id,
            requested_by_user_id=principal.user_id,
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="The translated formula revision has an unsupported visibility.",
    )


def _paper_response(
    paper: PaperModel,
    library_item: LibraryItemModel,
    principal: RequestPrincipal,
    *,
    abstract_render_payload: dict[str, object] | None = None,
) -> PaperResponse:
    ordered_tasks = (
        sorted(
            (task for task in paper.tasks if _task_visible_to_principal(task, principal)),
            key=lambda task: (task.priority, task.created_at),
        )
        if principal.has(Permission.TASK_READ)
        else []
    )
    latest_pipeline_tasks: dict[str, ProcessingTaskModel] = {}
    visible_tasks: list[ProcessingTaskModel] = []
    for task in ordered_tasks:
        if task.status == TaskStatus.CANCELLED:
            continue
        if task.task_type in {FETCH_METADATA, FETCH_SOURCE, PARSE_SOURCE}:
            latest_pipeline_tasks[task.task_type] = task
        elif task.task_type in {
            TRANSLATE_BLOCK,
            ANSWER_QUESTION,
            PLAN_PAPER_MAP,
            ANSWER_MAP_QUESTION,
        }:
            continue
        else:
            visible_tasks.append(task)
    visible_tasks.extend(latest_pipeline_tasks.values())
    visible_tasks.sort(key=lambda task: (task.priority, task.created_at))
    return PaperResponse(
        id=paper.id,
        arxiv_id=paper.arxiv_id,
        arxiv_version=paper.arxiv_version,
        canonical_arxiv_id=f"{paper.arxiv_id}{paper.arxiv_version}",
        title=paper.title,
        abstract=paper.abstract,
        abstract_block_id=abstract_block_id(paper.id) if paper.abstract else None,
        abstract_render_payload=abstract_render_payload,
        submitted_at=paper.submitted_at,
        processing_stage=paper.processing_stage,
        processing_health=paper.processing_health,
        reading_status=library_item.reading_status,
        is_favorite=library_item.is_favorite,
        pinned_at=library_item.pinned_at,
        last_opened_at=library_item.last_read_at,
        created_at=paper.created_at,
        updated_at=paper.updated_at,
        authors=[AuthorResponse(position=item.position, name=item.name) for item in paper.authors],
        sources=[
            SourceResponse(
                source_type=item.source_type,
                storage_key=item.storage_key,
                sha256=item.sha256,
                byte_size=item.byte_size,
                mime_type=item.mime_type,
                retrieved_at=item.retrieved_at,
            )
            for item in paper.sources
        ],
        tasks=[
            TaskResponse(
                id=item.id,
                task_type=item.task_type,
                status=item.status,
                priority=item.priority,
                attempt_count=item.attempt_count,
                max_attempts=item.max_attempts,
                error_code=item.error_code,
                error_message=item.error_message,
                download_progress=(
                    TaskDownloadProgressResponse.model_validate(item.result_json["download"])
                    if isinstance(item.result_json, dict)
                    and isinstance(item.result_json.get("download"), dict)
                    else None
                ),
                created_at=item.created_at,
                started_at=item.started_at,
                finished_at=item.finished_at,
            )
            for item in visible_tasks
        ],
    )


def _load_paper(
    session,
    request: Request,
    paper_id: str,
    *,
    permission: Permission = Permission.LIBRARY_READ,
) -> WorkspacePaper | None:
    principal = require_request_permission(request, permission)
    return load_workspace_paper(
        session,
        principal,
        paper_id,
        paper_options=_options(principal),
    )


def _require_paper(
    session,
    request: Request,
    paper_id: str,
    *,
    permission: Permission = Permission.LIBRARY_READ,
) -> WorkspacePaper:
    workspace_paper = _load_paper(
        session,
        request,
        paper_id,
        permission=permission,
    )
    if workspace_paper is None:
        raise HTTPException(status_code=404, detail="Paper not found.")
    return workspace_paper


def _require_paper_block(
    session,
    workspace_paper: WorkspacePaper,
    *,
    paper_id: str,
    block_id: str,
) -> ContentBlockModel:
    block = session.scalar(
        select(ContentBlockModel).where(
            ContentBlockModel.id == block_id,
            ContentBlockModel.paper_id == paper_id,
        )
    )
    if block is None and block_id == abstract_block_id(paper_id):
        block = ensure_abstract_block(session, workspace_paper.paper)
        session.flush()
    if block is None:
        raise HTTPException(status_code=404, detail="Content block not found.")
    return block


@router.post("/import", response_model=ImportPaperResponse)
def import_paper(
    payload: ImportPaperRequest,
    request: Request,
    response: Response,
) -> ImportPaperResponse:
    principal = require_request_permission(request, Permission.LIBRARY_WRITE)
    try:
        with request.app.state.session_factory() as session:
            library = ensure_default_library(session, principal)
            item, created = import_into_library(session, principal, library.id, payload.source)
            paper_id = item.paper_id
            session.commit()
    except InvalidArxivIdentifier as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except LibraryError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from error

    loaded = None
    with request.app.state.session_factory() as session:
        loaded = _load_paper(
            session,
            request,
            paper_id,
            permission=Permission.LIBRARY_WRITE,
        )
        if loaded is None:
            raise HTTPException(status_code=500, detail="Paper could not be reloaded.")
        result = _paper_response(
            loaded.paper,
            loaded.library_item,
            principal,
        )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ImportPaperResponse(created=created, paper=result)


@router.get("", response_model=list[PaperResponse])
def list_papers(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    favorite_only: bool = Query(default=False),
) -> list[PaperResponse]:
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        ensure_default_library(session, principal)
        relationship_options = [
            selectinload(LibraryItemModel.paper).selectinload(PaperModel.authors),
            selectinload(LibraryItemModel.paper).selectinload(PaperModel.sources),
        ]
        if principal.has(Permission.TASK_READ):
            relationship_options.extend(
                [
                    selectinload(LibraryItemModel.paper).selectinload(PaperModel.tasks),
                    with_loader_criteria(
                        ProcessingTaskModel,
                        _task_visibility_predicate(principal),
                        include_aliases=True,
                    ),
                ]
            )
        statement = (
            select(LibraryItemModel)
            .join(PaperModel, PaperModel.id == LibraryItemModel.paper_id)
            .options(*relationship_options)
            .where(library_item_access_predicate(principal))
            .order_by(
                LibraryItemModel.pinned_at.is_(None),
                LibraryItemModel.pinned_at.desc(),
                LibraryItemModel.last_read_at.is_(None),
                LibraryItemModel.last_read_at.desc(),
                LibraryItemModel.created_at.desc(),
            )
        )
        if favorite_only:
            statement = statement.where(LibraryItemModel.is_favorite.is_(True))
        if q and q.strip():
            term = f"%{q.strip()}%"
            statement = statement.where(
                or_(PaperModel.title.ilike(term), PaperModel.arxiv_id.ilike(term))
            )
        library_items = session.scalars(statement).all()
        result = [_paper_response(item.paper, item, principal) for item in library_items]
        session.commit()
        return result


@router.patch("/{paper_id}/preferences", response_model=PaperResponse)
def update_paper_preferences(
    paper_id: str,
    payload: PaperPreferencesRequest,
    request: Request,
) -> PaperResponse:
    principal = require_request_permission(request, Permission.LIBRARY_WRITE)
    if payload.is_favorite is None and payload.is_pinned is None:
        raise HTTPException(status_code=422, detail="At least one paper preference is required.")
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.LIBRARY_WRITE,
        )
        library_item = workspace_paper.library_item
        if payload.is_favorite is not None:
            library_item.is_favorite = payload.is_favorite
        if payload.is_pinned is not None:
            if payload.is_pinned and library_item.pinned_at is None:
                library_item.pinned_at = now_utc()
            elif not payload.is_pinned:
                library_item.pinned_at = None
        session.commit()
        loaded = _load_paper(
            session,
            request,
            paper_id,
            permission=Permission.LIBRARY_WRITE,
        )
        if loaded is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        return _paper_response(
            loaded.paper,
            loaded.library_item,
            principal,
        )


@router.post("/{paper_id}/opened", response_model=PaperResponse)
def mark_paper_opened(paper_id: str, request: Request) -> PaperResponse:
    principal = require_request_permission(request, Permission.LIBRARY_WRITE)
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.LIBRARY_WRITE,
        )
        workspace_paper.library_item.last_read_at = now_utc()
        session.commit()
        loaded = _load_paper(
            session,
            request,
            paper_id,
            permission=Permission.LIBRARY_WRITE,
        )
        if loaded is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        return _paper_response(
            loaded.paper,
            loaded.library_item,
            principal,
        )


def _bookmark_preview(block: ContentBlockModel) -> str:
    payload = block.render_payload_json if isinstance(block.render_payload_json, dict) else {}
    candidates = [payload.get("text"), payload.get("caption"), block.source_text]
    text = next((item for item in candidates if isinstance(item, str) and item.strip()), "")
    normalized = re.sub(r"\s+", " ", text).strip()
    points = list(normalized)
    return normalized if len(points) <= 180 else f"{''.join(points[:177]).rstrip()}…"


def _block_bookmark_response(
    session,
    bookmark: BlockBookmarkModel,
) -> BlockBookmarkResponse:
    block = bookmark.block
    section = session.scalar(
        select(ContentBlockModel)
        .where(
            ContentBlockModel.paper_id == bookmark.paper_id,
            ContentBlockModel.block_type == "SECTION",
            ContentBlockModel.ordinal_path <= block.ordinal_path,
        )
        .order_by(ContentBlockModel.ordinal_path.desc())
        .limit(1)
    )
    section_number: str | None = None
    if section is not None and isinstance(section.render_payload_json, dict):
        numbering = section.render_payload_json.get("numbering")
        if isinstance(numbering, dict) and numbering.get("display_number") is not None:
            section_number = str(numbering["display_number"])
    return BlockBookmarkResponse(
        id=bookmark.id,
        paper_id=bookmark.paper_id,
        block_id=bookmark.block_id,
        block_type=block.block_type,
        ordinal_path=block.ordinal_path,
        section_title=section.source_text if section is not None else None,
        section_display_number=section_number,
        preview=_bookmark_preview(block),
        created_at=bookmark.created_at,
    )


@router.get("/{paper_id}/bookmarks", response_model=list[BlockBookmarkResponse])
def list_block_bookmarks(paper_id: str, request: Request) -> list[BlockBookmarkResponse]:
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        bookmarks = session.scalars(
            select(BlockBookmarkModel)
            .options(selectinload(BlockBookmarkModel.block))
            .where(
                BlockBookmarkModel.workspace_id == principal.active_workspace_id,
                BlockBookmarkModel.creator_user_id == principal.user_id,
                BlockBookmarkModel.paper_id == paper_id,
            )
            .order_by(BlockBookmarkModel.created_at.desc())
        ).all()
        return [_block_bookmark_response(session, bookmark) for bookmark in bookmarks]


@router.put(
    "/{paper_id}/bookmarks/{block_id}",
    response_model=BlockBookmarkResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_block_bookmark(
    paper_id: str,
    block_id: str,
    request: Request,
    response: Response,
) -> BlockBookmarkResponse:
    principal = require_request_permission(request, Permission.LIBRARY_WRITE)
    with request.app.state.session_factory() as session:
        _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.LIBRARY_WRITE,
        )
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == paper_id,
            )
        )
        if block is None:
            raise HTTPException(status_code=404, detail="Paper block not found.")
        bookmark = session.scalar(
            select(BlockBookmarkModel)
            .options(selectinload(BlockBookmarkModel.block))
            .where(
                BlockBookmarkModel.workspace_id == principal.active_workspace_id,
                BlockBookmarkModel.creator_user_id == principal.user_id,
                BlockBookmarkModel.paper_id == paper_id,
                BlockBookmarkModel.block_id == block_id,
            )
        )
        if bookmark is None:
            bookmark = BlockBookmarkModel(
                workspace_id=principal.active_workspace_id,
                creator_user_id=principal.user_id,
                paper_id=paper_id,
                block_id=block_id,
                block=block,
            )
            session.add(bookmark)
            session.commit()
            session.refresh(bookmark)
        else:
            response.status_code = status.HTTP_200_OK
        return _block_bookmark_response(session, bookmark)


@router.delete("/{paper_id}/bookmarks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_block_bookmark(paper_id: str, block_id: str, request: Request) -> Response:
    principal = require_request_permission(request, Permission.LIBRARY_WRITE)
    with request.app.state.session_factory() as session:
        _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.LIBRARY_WRITE,
        )
        bookmark = session.scalar(
            select(BlockBookmarkModel).where(
                BlockBookmarkModel.workspace_id == principal.active_workspace_id,
                BlockBookmarkModel.creator_user_id == principal.user_id,
                BlockBookmarkModel.paper_id == paper_id,
                BlockBookmarkModel.block_id == block_id,
            )
        )
        if bookmark is None:
            raise HTTPException(status_code=404, detail="Block bookmark not found.")
        session.delete(bookmark)
        session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", response_model=DeletePapersResponse)
def delete_papers(payload: DeletePapersRequest, request: Request) -> DeletePapersResponse:
    principal = require_request_permission(request, Permission.LIBRARY_WRITE)
    requested_ids = list(dict.fromkeys(payload.paper_ids))
    with request.app.state.session_factory() as session:
        ensure_default_library(session, principal)
        entries = session.execute(
            select(LibraryEntryModel, LibraryItemModel.paper_id)
            .join(LibraryItemModel, LibraryItemModel.id == LibraryEntryModel.library_item_id)
            .join(LibraryModel, LibraryModel.id == LibraryEntryModel.library_id)
            .where(
                LibraryItemModel.workspace_id == principal.active_workspace_id,
                LibraryItemModel.paper_id.in_(requested_ids),
                library_access_predicate(principal, "remove"),
            )
        ).all()
        deleted_ids = list(dict.fromkeys(paper_id for _entry, paper_id in entries))
        for entry, _paper_id in entries:
            session.delete(entry)
        # Compatibility removal never deletes canonical content, other-library references or
        # another member's private annotations/translations. Physical GC is an operator action.
        session.commit()
    return DeletePapersResponse(
        deleted_count=len(deleted_ids),
        deleted_ids=deleted_ids,
        deleted_files=0,
    )


@router.get("/{paper_id}/outline", response_model=list[OutlineItemResponse])
def get_paper_outline(paper_id: str, request: Request) -> list[OutlineItemResponse]:
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        blocks = session.scalars(
            select(ContentBlockModel)
            .where(
                ContentBlockModel.paper_id == paper_id,
                ContentBlockModel.block_type.in_({"SECTION", "BIBLIOGRAPHY"}),
            )
            .order_by(ContentBlockModel.ordinal_path)
        ).all()
        return [
            OutlineItemResponse(
                id=block.id,
                parent_id=block.parent_id,
                ordinal_path=block.ordinal_path,
                heading_level=block.heading_level or 1,
                display_number=(
                    str(numbering.get("display_number"))
                    if isinstance(block.render_payload_json, dict)
                    and isinstance(numbering := block.render_payload_json.get("numbering"), dict)
                    and numbering.get("display_number") is not None
                    else None
                ),
                title=block.source_text or "Untitled section",
                title_segments=(
                    list(block.render_payload_json.get("segments", []))
                    if isinstance(block.render_payload_json, dict)
                    and isinstance(block.render_payload_json.get("segments"), list)
                    else []
                ),
            )
            for block in blocks
        ]


@router.get("/{paper_id}/blocks", response_model=ContentBlockPageResponse)
def get_paper_blocks(
    paper_id: str,
    request: Request,
    after: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
) -> ContentBlockPageResponse:
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        statement = (
            select(ContentBlockModel)
            .options(
                selectinload(ContentBlockModel.translations),
                with_loader_criteria(
                    TranslationModel,
                    _translation_visibility_predicate(principal),
                    include_aliases=True,
                ),
            )
            .where(
                ContentBlockModel.paper_id == paper_id,
                ContentBlockModel.block_type != "ABSTRACT",
            )
        )
        if after is not None:
            statement = statement.where(ContentBlockModel.ordinal_path > after)
        blocks = list(
            session.scalars(
                statement.order_by(ContentBlockModel.ordinal_path).limit(limit + 1)
            ).all()
        )
        has_more = len(blocks) > limit
        page = blocks[:limit]
        effective_payloads = {
            block.id: effective_block_render(
                session,
                workspace_id=principal.active_workspace_id,
                block=block,
            ).payload
            for block in page
        }
        return ContentBlockPageResponse(
            items=[
                ContentBlockResponse(
                    id=block.id,
                    parent_id=block.parent_id,
                    block_type=block.block_type,
                    ordinal_path=block.ordinal_path,
                    heading_level=block.heading_level,
                    source_text=block.source_text,
                    source_payload=block.source_payload_json,
                    render_payload=effective_payloads[block.id],
                    source_locator=block.source_locator_json,
                    translation=(
                        _translation_response(translation, principal)
                        if (
                            translation := _current_translation(
                                block,
                                request.app.state.settings,
                                principal,
                            )
                        )
                        else None
                    ),
                )
                for block in page
            ],
            next_cursor=page[-1].ordinal_path if has_more and page else None,
        )


@router.get("/{paper_id}/bibliography", response_model=PaperBibliographyResponse)
def get_paper_bibliography(
    paper_id: str,
    request: Request,
) -> PaperBibliographyResponse:
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        bibliography = session.scalar(
            select(PaperBibliographyModel)
            .options(
                selectinload(PaperBibliographyModel.entries).selectinload(
                    BibliographyEntryModel.persons
                )
            )
            .where(PaperBibliographyModel.paper_id == paper_id)
        )
        if bibliography is None:
            return PaperBibliographyResponse(
                status="EMPTY",
                requested_style=None,
                resolved_style=None,
                fallback_used=False,
                processor_version=None,
                source_files=[],
                warnings=[],
                entries=[],
            )
        entries = sorted(
            bibliography.entries,
            key=lambda entry: (
                entry.ordinal is None,
                entry.ordinal or 0,
                entry.citation_key.casefold(),
            ),
        )
        return PaperBibliographyResponse(
            status=bibliography.status,
            requested_style=bibliography.requested_style,
            resolved_style=bibliography.resolved_style,
            fallback_used=bibliography.fallback_used,
            processor_version=bibliography.processor_version,
            source_files=list(bibliography.source_files_json or []),
            warnings=list(bibliography.warnings_json or []),
            entries=[
                BibliographyEntryResponse(
                    id=entry.id,
                    citation_key=entry.citation_key,
                    entry_type=entry.entry_type,
                    source_path=entry.source_path,
                    fields=entry.fields_json,
                    title=entry.title,
                    container_title=entry.container_title,
                    publication_year=entry.publication_year,
                    doi=entry.doi,
                    url=entry.url,
                    eprint=entry.eprint,
                    archive_prefix=entry.archive_prefix,
                    isbn=entry.isbn,
                    issn=entry.issn,
                    ordinal=entry.ordinal,
                    display_label=entry.display_label,
                    formatted_text=entry.formatted_text,
                    render_payload=entry.render_payload_json,
                    persons=[
                        BibliographyPersonResponse(
                            role=person.role,
                            position=person.position,
                            display_name=person.display_name,
                            given_names=list(person.given_names_json or []),
                            family_names=list(person.family_names_json or []),
                            prefix_names=list(person.prefix_names_json or []),
                            suffix_names=list(person.suffix_names_json or []),
                            initials=list(person.initials_json or []),
                        )
                        for person in entry.persons
                    ],
                )
                for entry in entries
            ],
        )


@router.get(
    "/{paper_id}/translations",
    response_model=list[TranslationResponse],
)
def get_paper_translations(paper_id: str, request: Request) -> list[TranslationResponse]:
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        blocks = list(
            session.scalars(
                select(ContentBlockModel)
                .options(
                    selectinload(ContentBlockModel.translations),
                    with_loader_criteria(
                        TranslationModel,
                        _translation_visibility_predicate(principal),
                        include_aliases=True,
                    ),
                )
                .where(ContentBlockModel.paper_id == paper_id)
                .order_by(ContentBlockModel.ordinal_path)
            ).all()
        )
        return [
            _translation_response(translation, principal)
            for block in blocks
            if (
                translation := _current_translation(
                    block,
                    request.app.state.settings,
                    principal,
                )
            )
        ]


@router.get(
    "/{paper_id}/progress",
    response_model=TranslationProgressResponse,
)
def get_paper_progress(paper_id: str, request: Request) -> TranslationProgressResponse:
    settings = request.app.state.settings
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        llm_credential = request_llm_task_credential(
            request,
            user_id=principal.user_id,
            required=False,
        )
        blocks = list(
            session.scalars(
                select(ContentBlockModel)
                .options(
                    selectinload(ContentBlockModel.translations),
                    with_loader_criteria(
                        TranslationModel,
                        _translation_visibility_predicate(principal),
                        include_aliases=True,
                    ),
                )
                .where(ContentBlockModel.paper_id == paper_id)
                .order_by(ContentBlockModel.ordinal_path)
            ).all()
        )
        eligible_blocks = [
            block
            for block in blocks
            if extract_translation_units(
                block_type=block.block_type,
                source_text=block.source_text,
                render_payload=block.render_payload_json,
            )
        ]
        current = [
            translation
            for block in eligible_blocks
            if (translation := _current_translation(block, settings, principal))
        ]
        by_status = {
            status_value: sum(item.status == status_value for item in current)
            for status_value in TranslationStatus
        }
        missing = len(eligible_blocks) - len(current)
        tasks = list(
            session.scalars(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.paper_id == paper_id,
                    ProcessingTaskModel.task_type == TRANSLATE_BLOCK,
                    _task_visibility_predicate(principal),
                )
            ).all()
        )
        attempts_by_translation = {
            str(task.payload_json.get("translation_id")): task.attempt_count for task in tasks
        }
        failures = sorted(
            (item for item in current if item.status == TranslationStatus.FAILED),
            key=lambda item: item.updated_at,
            reverse=True,
        )[:8]
        latencies = [item.latency_ms for item in current if item.latency_ms is not None]
        return TranslationProgressResponse(
            configured=llm_credential is not None,
            target_language=settings.translation_target_language,
            provider=llm_credential.provider if llm_credential is not None else None,
            model=llm_credential.model if llm_credential is not None else None,
            prompt_version=TRANSLATION_PROMPT_VERSION,
            eligible=len(eligible_blocks),
            missing=missing,
            pending=by_status[TranslationStatus.PENDING] + missing,
            running=by_status[TranslationStatus.RUNNING],
            succeeded=by_status[TranslationStatus.SUCCEEDED],
            failed=by_status[TranslationStatus.FAILED],
            cache_hits=sum(item.cache_hit for item in current),
            input_tokens=sum(item.input_tokens or 0 for item in current),
            output_tokens=sum(item.output_tokens or 0 for item in current),
            average_latency_ms=(round(sum(latencies) / len(latencies)) if latencies else None),
            retry_count=sum(max(0, task.attempt_count - 1) for task in tasks),
            recent_errors=[
                TranslationErrorResponse(
                    block_id=item.block_id,
                    error_type=item.error_type,
                    error_code=item.error_code,
                    error_message=item.error_message,
                    attempt_count=attempts_by_translation.get(item.id, 0),
                )
                for item in failures
            ],
        )


@router.post(
    "/{paper_id}/blocks/{block_id}/translation/retry",
    response_model=TranslationRetryResponse,
)
def retry_block_translation(
    paper_id: str,
    block_id: str,
    request: Request,
) -> TranslationRetryResponse:
    principal = require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
        _require_paper_block(
            session,
            workspace_paper,
            paper_id=paper_id,
            block_id=block_id,
        )
        task = retry_translation(
            session,
            paper_id=paper_id,
            block_id=block_id,
            workspace_id=principal.active_workspace_id,
            requested_by_user_id=principal.user_id,
            credential_id=None,
        )
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No failed translation is available to retry for this block.",
            )
        llm_credential = request_llm_task_credential(
            request,
            user_id=principal.user_id,
            allow_unconfigured_environment=True,
        )
        assert llm_credential is not None
        task.credential_id = llm_credential.credential_id
        session.commit()
        return TranslationRetryResponse(task_id=task.id, status=task.status)


@router.post(
    "/{paper_id}/blocks/{block_id}/translation/retranslate",
    response_model=TranslationRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retranslate_block(
    paper_id: str,
    block_id: str,
    payload: TranslationRetranslateRequest,
    request: Request,
) -> TranslationRetryResponse:
    settings = request.app.state.settings
    principal = require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
        _require_paper_block(
            session,
            workspace_paper,
            paper_id=paper_id,
            block_id=block_id,
        )
        llm_credential = request_llm_task_credential(request, user_id=principal.user_id)
        assert llm_credential is not None
        try:
            base_system_prompt = validate_translation_system_prompt(
                payload.system_prompt
                or get_translation_system_prompt(session, user_id=principal.user_id)
            )
            system_prompt = compose_translation_system_prompt(base_system_prompt)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        task = create_custom_retranslation(
            session,
            paper_id=paper_id,
            block_id=block_id,
            language=settings.translation_target_language,
            provider=llm_credential.provider,
            model=llm_credential.model,
            max_attempts=settings.translation_max_attempts,
            system_prompt=system_prompt,
            task_scope=TaskScope.user_private(
                workspace_id=principal.active_workspace_id,
                requested_by_user_id=principal.user_id,
                credential_id=llm_credential.credential_id,
            ),
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Translation target not found.")
        session.commit()
        return TranslationRetryResponse(task_id=task.id, status=task.status)


@router.post(
    "/{paper_id}/translations/remaining",
    response_model=PaperRemainingTranslationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def translate_remaining_paper(
    paper_id: str,
    request: Request,
) -> PaperRemainingTranslationResponse:
    settings = request.app.state.settings
    principal = require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
        llm_credential = request_llm_task_credential(request, user_id=principal.user_id)
        assert llm_credential is not None
        try:
            base_system_prompt = validate_translation_system_prompt(
                get_translation_system_prompt(session, user_id=principal.user_id)
            )
            system_prompt = compose_translation_system_prompt(base_system_prompt)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        result = schedule_remaining_paper_translations(
            session,
            paper_id=paper_id,
            language=settings.translation_target_language,
            provider=llm_credential.provider,
            model=llm_credential.model,
            max_attempts=settings.translation_max_attempts,
            system_prompt=system_prompt,
            task_scope=translation_task_scope_for_base_prompt(
                workspace_id=principal.active_workspace_id,
                requested_by_user_id=principal.user_id,
                base_system_prompt=base_system_prompt,
                credential_id=llm_credential.credential_id,
            ),
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        session.commit()
        return PaperRemainingTranslationResponse(
            eligible=result.eligible,
            queued=result.queued,
            created=result.created,
            retried=result.retried,
            cached=result.cached,
            preserved=result.preserved,
            in_flight=result.in_flight,
        )


@router.post(
    "/{paper_id}/translations/retranslate",
    response_model=PaperRetranslationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retranslate_paper(
    paper_id: str,
    payload: TranslationRetranslateRequest,
    request: Request,
) -> PaperRetranslationResponse:
    settings = request.app.state.settings
    principal = require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
        llm_credential = request_llm_task_credential(request, user_id=principal.user_id)
        assert llm_credential is not None
        try:
            base_system_prompt = validate_translation_system_prompt(
                payload.system_prompt
                or get_translation_system_prompt(session, user_id=principal.user_id)
            )
            system_prompt = compose_translation_system_prompt(base_system_prompt)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        result = create_paper_retranslation(
            session,
            paper_id=paper_id,
            language=settings.translation_target_language,
            provider=llm_credential.provider,
            model=llm_credential.model,
            max_attempts=settings.translation_max_attempts,
            system_prompt=system_prompt,
            task_scope=TaskScope.user_private(
                workspace_id=principal.active_workspace_id,
                requested_by_user_id=principal.user_id,
                credential_id=llm_credential.credential_id,
            ),
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Paper not found.")
        session.commit()
        return PaperRetranslationResponse(
            batch_id=result.batch_id,
            eligible=result.eligible,
            queued=result.queued,
            superseded_tasks=result.superseded_tasks,
        )


@router.get("/{paper_id}/tasks/{task_id}", response_model=TaskDetailResponse)
def get_task_detail(paper_id: str, task_id: str, request: Request) -> TaskDetailResponse:
    principal = require_request_permission(request, Permission.TASK_READ)
    with request.app.state.session_factory() as session:
        _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_READ,
        )
        task = session.scalar(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.id == task_id,
                ProcessingTaskModel.paper_id == paper_id,
                _task_visibility_predicate(principal),
            )
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")

        payload = task.payload_json or {}
        block = _task_block(session, paper_id=paper_id, payload=payload)
        block_render_payload = (
            effective_block_render(
                session,
                workspace_id=principal.active_workspace_id,
                block=block,
            ).payload
            if block is not None
            else {}
        )
        source_latex = _block_source_latex(block)
        target_location = _block_location(block)

        if task.task_type in {RENDER_TABLE, RENDER_FIGURE, RENDER_LATEX_BLOCK}:
            is_figure = task.task_type == RENDER_FIGURE
            repaired_task_source = payload.get("source_latex")
            if isinstance(repaired_task_source, str) and repaired_task_source.strip():
                source_latex = repaired_task_source[:16000]
            render_key = (
                "figure_render"
                if is_figure
                else "latex_block_render"
                if task.task_type == RENDER_LATEX_BLOCK
                else "table_render"
            )
            render_state = block_render_payload.get(render_key, {}) if block is not None else {}
            if not isinstance(render_state, dict):
                render_state = {}
            complexity_reasons = [
                str(reason)[:120]
                for reason in render_state.get("complexity_reasons", [])
                if isinstance(reason, str) and reason.strip()
            ][:12]
            compiler = str(render_state.get("compiler", "")).strip() or "latex + dvisvgm"
            asset = render_state.get("asset", {})
            asset_size = _display_bytes(asset.get("byte_size")) if isinstance(asset, dict) else None
            if task.status == TaskStatus.SUCCEEDED:
                if is_figure:
                    result_summary = "Generated and cached a high-fidelity SVG figure"
                elif task.task_type == RENDER_LATEX_BLOCK:
                    result_summary = "Generated and cached a source-faithful LaTeX block image"
                elif isinstance(asset, dict) and asset.get("mime_type") == "image/png":
                    result_summary = "Generated and cached a high-resolution raster table"
                else:
                    result_summary = "Generated and cached a high-fidelity SVG table"
                if asset_size:
                    result_summary += f" ({asset_size})"
                result_summary += "."
            elif task.status == TaskStatus.FAILED:
                result_summary = (
                    "Local figure compilation failed; canonical source and caption "
                    "remain available."
                    if is_figure
                    else "Local compilation failed; accessible table data and source "
                    "remain available."
                )
                task_result = task.result_json if isinstance(task.result_json, dict) else {}
                skipped_reason = task_result.get("automatic_repair_skipped_reason")
                if isinstance(skipped_reason, str) and skipped_reason:
                    result_summary += f" AI repair was not queued: {skipped_reason}"
            else:
                result_summary = (
                    "The local figure render is waiting for or running in the worker."
                    if is_figure
                    else "The local table render is waiting for or running in the worker."
                )
            if is_figure:
                trigger_reason = (
                    "The figure contains self-contained LaTeX drawing or text layout commands "
                    "and does not reference an external image asset."
                )
            else:
                trigger_reason = (
                    "High-fidelity rendering was selected because the table contains "
                    + ", ".join(complexity_reasons)
                    + "."
                    if complexity_reasons
                    else "The parser selected this table for high-fidelity local rendering."
                )
            return TaskDetailResponse(
                id=task.id,
                task_type=task.task_type,
                status=task.status,
                description=_TASK_DESCRIPTIONS[task.task_type],
                execution_kind="local",
                uses_llm=False,
                engine=compiler,
                renderer="latex-svg",
                renderer_error=task.error_message,
                target_block_id=block.id if block is not None else None,
                target_block_type=(
                    block.block_type if block is not None else ("FIGURE" if is_figure else "TABLE")
                ),
                target_location=target_location,
                trigger_reason=trigger_reason,
                complexity_reasons=complexity_reasons,
                result_summary=result_summary,
                safety_notes=[
                    "Runs inside the local worker; no LLM API or external render "
                    "service is called.",
                    "TeX shell escape is disabled and file access, runtime, input, "
                    "and output size are bounded.",
                    "The SVG is rebuildable derived data; the original LaTeX and "
                    "accessible caption or fallback are preserved.",
                ],
                source_latex=source_latex,
            )

        if task.task_type == REPAIR_RENDER:
            settings = request.app.state.settings
            inline_selector = payload.get("formula_selector")
            inline_payload: object | None = None
            if isinstance(inline_selector, dict):
                source_latex = str(inline_selector.get("exact_latex", ""))[:16000] or None
                if payload.get("formula_target") == "TRANSLATION":
                    translation = session.scalar(
                        select(TranslationModel).where(
                            TranslationModel.id == str(payload.get("translation_id", "")),
                            _translation_visibility_predicate(principal),
                        )
                    )
                    inline_payload = (
                        translation.translated_payload_json if translation is not None else None
                    )
                elif block is not None:
                    inline_payload = block_render_payload
            ai_repair = (
                formula_repair_metadata(inline_payload, inline_selector)
                if isinstance(inline_selector, dict)
                else (block_render_payload.get("ai_repair", {}) if block is not None else {})
            )
            if not isinstance(ai_repair, dict):
                ai_repair = {}
            deterministic_katex = ai_repair.get("repair_mode") == "deterministic-katex" or (
                isinstance(source_latex, str)
                and bool(source_latex)
                and normalize_katex_latex(source_latex) != source_latex
            )
            explanation = ai_repair.get("explanation")
            confidence = ai_repair.get("confidence")
            result_summary = None
            if task.status == TaskStatus.SUCCEEDED:
                result_summary = (
                    "The local KaTeX compatibility result was applied without an LLM call."
                    if deterministic_katex
                    else "The validated model response was applied to the render payload."
                )
                replacement_render_task_id = (
                    task.result_json.get("replacement_render_task_id")
                    if isinstance(task.result_json, dict)
                    else None
                )
                if replacement_render_task_id:
                    repaired_noun = (
                        "figure" if block is not None and block.block_type == "FIGURE" else "table"
                    )
                    result_summary += (
                        f" A replacement local LaTeX {repaired_noun} render was queued and must "
                        f"succeed before the {repaired_noun} is considered repaired."
                    )
                superseded_tasks = (
                    task.result_json.get("superseded_render_task_ids", [])
                    if isinstance(task.result_json, dict)
                    else []
                )
                if isinstance(superseded_tasks, list) and superseded_tasks:
                    repaired_noun = (
                        "figure" if block is not None and block.block_type == "FIGURE" else "table"
                    )
                    result_summary += (
                        f" It superseded {len(superseded_tasks)} failed local {repaired_noun} "
                        "render task"
                        f"{'s' if len(superseded_tasks) != 1 else ''}."
                    )
                if isinstance(confidence, (int, float)):
                    result_summary += f" Reported confidence: {confidence:.2f}."
                if isinstance(explanation, str) and explanation.strip():
                    result_summary += f" {explanation.strip()[:1200]}"
            elif task.status == TaskStatus.FAILED:
                result_summary = (
                    "The local KaTeX compatibility repair failed; the original block remains "
                    "unchanged."
                    if deterministic_katex
                    else "The model-assisted repair failed; the original block remains unchanged."
                )
            elif task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                result_summary = (
                    "The local KaTeX compatibility repair is queued or currently running."
                    if deterministic_katex
                    else "The model-assisted repair is queued or currently running."
                )
            renderer_error = str(payload.get("error_message", ""))[:2000] or None
            return TaskDetailResponse(
                id=task.id,
                task_type=task.task_type,
                status=task.status,
                description=(
                    "Applies a bounded local compatibility transform to an inline formula that "
                    "KaTeX could not display. Canonical source LaTeX is kept."
                    if deterministic_katex
                    else _TASK_DESCRIPTIONS[REPAIR_RENDER]
                ),
                execution_kind="local" if deterministic_katex else "llm",
                uses_llm=not deterministic_katex,
                engine=(
                    KATEX_NORMALIZER_VERSION
                    if deterministic_katex
                    else "Configured LLM render-repair client"
                ),
                provider=None if deterministic_katex else settings.llm_provider or None,
                model=None if deterministic_katex else settings.llm_model or None,
                reasoning_effort=(
                    None if deterministic_katex else settings.repair_render_reasoning_effort
                ),
                renderer=str(payload.get("renderer", "unknown"))[:64],
                renderer_error=renderer_error,
                target_block_id=block.id if block is not None else None,
                target_block_type=block.block_type if block is not None else None,
                target_location=target_location,
                trigger_reason=renderer_error,
                result_summary=result_summary,
                safety_notes=(
                    [
                        "The bounded compatibility transform runs locally and sends no content "
                        "to an LLM provider.",
                        "Only the rebuildable render payload changes; canonical source LaTeX is "
                        "preserved.",
                        "Unknown commands remain explicit failures and are never guessed by the "
                        "local normalizer.",
                    ]
                    if deterministic_katex
                    else [
                        "Only the target block LaTeX, renderer error, block type, and "
                        "available asset names are sent to the configured provider.",
                        "API credentials and unrelated task payload fields are never returned "
                        "by this endpoint or included in the prompt.",
                        "The response is schema-, path-, and command-validated and can only "
                        "update rebuildable render data; source LaTeX is preserved.",
                    ]
                ),
                system_prompt=None if deterministic_katex else RENDER_REPAIR_SYSTEM_PROMPT,
                source_latex=source_latex,
                task_error_code=task.error_code,
                task_error_message=task.error_message,
            )

        if task.task_type == TRANSLATE_BLOCK:
            translation_id = str(payload.get("translation_id", ""))
            translation = session.scalar(
                select(TranslationModel).where(
                    TranslationModel.id == translation_id,
                    _translation_visibility_predicate(principal),
                )
            )
            result_summary = None
            if task.status == TaskStatus.SUCCEEDED:
                result_summary = (
                    "Used a validated cached translation."
                    if translation is not None and translation.cache_hit
                    else "Generated and stored a validated block translation."
                )
            elif task.status == TaskStatus.FAILED:
                result_summary = "Translation failed; the original block remains readable."
            else:
                result_summary = "Translation is queued, retrying, or running."
            return TaskDetailResponse(
                id=task.id,
                task_type=task.task_type,
                status=task.status,
                description=_TASK_DESCRIPTIONS[TRANSLATE_BLOCK],
                execution_kind="llm",
                uses_llm=True,
                engine="AcadematePaper structured block translator",
                provider=translation.provider
                if translation is not None
                else payload.get("provider"),
                model=translation.model if translation is not None else payload.get("model"),
                target_block_id=block.id if block is not None else None,
                target_block_type=block.block_type if block is not None else None,
                target_location=target_location,
                trigger_reason=(
                    "This block contains translatable prose or a caption and has no current "
                    "cached translation."
                ),
                result_summary=result_summary,
                safety_notes=[
                    "The source block, nearby source context, paper title, and section title are "
                    "sent to the configured provider.",
                    "Math placeholders and translation unit keys are validated before storage.",
                    "The model output is rebuildable derived data and never overwrites source or "
                    "render payloads.",
                ],
                system_prompt=(
                    translation.system_prompt
                    if translation is not None
                    and translation.requested_by_user_id == principal.user_id
                    else None
                ),
                source_text=block.source_text[:16000] if block and block.source_text else None,
                input_tokens=translation.input_tokens if translation is not None else None,
                output_tokens=translation.output_tokens if translation is not None else None,
                latency_ms=translation.latency_ms if translation is not None else None,
                estimated_cost_microusd=(
                    translation.estimated_cost_microusd if translation is not None else None
                ),
                cache_hit=translation.cache_hit if translation is not None else None,
            )

        if task.task_type in {PLAN_PAPER_MAP, ANSWER_MAP_QUESTION}:
            settings = request.app.state.settings
            paper_map = session.scalar(
                select(PaperMapModel).where(
                    PaperMapModel.id == str(payload.get("map_id", "")),
                    PaperMapModel.workspace_id == principal.active_workspace_id,
                )
            )
            question = (
                session.scalar(
                    select(PaperMapQuestionModel).where(
                        PaperMapQuestionModel.id == str(payload.get("question_id", "")),
                        PaperMapQuestionModel.map_id
                        == (paper_map.id if paper_map is not None else ""),
                    )
                )
                if task.task_type == ANSWER_MAP_QUESTION
                else None
            )
            return TaskDetailResponse(
                id=task.id,
                task_type=task.task_type,
                status=task.status,
                description=_TASK_DESCRIPTIONS[task.task_type],
                execution_kind="llm",
                uses_llm=True,
                engine="AcadematePaper paper-map generator",
                provider=(
                    question.provider
                    if question is not None and question.provider
                    else paper_map.provider
                    if paper_map is not None
                    else settings.llm_provider or None
                ),
                model=(
                    question.model
                    if question is not None and question.model
                    else paper_map.model
                    if paper_map is not None
                    else settings.llm_model or None
                ),
                reasoning_effort="high",
                result_summary=(
                    "Generated a paper-map question plan or one verified answer."
                    if task.status == TaskStatus.SUCCEEDED
                    else "Paper-map generation failed without changing source or user data."
                    if task.status == TaskStatus.FAILED
                    else "Paper-map generation is queued, retrying, or running."
                ),
                safety_notes=[
                    "Only bounded context from this paper is sent to the configured provider.",
                    "Paper text is treated as untrusted data and cannot expand tool permissions.",
                    "Citations are matched against canonical source blocks before publication.",
                    "All generated map records are rebuildable derived data.",
                ],
                system_prompt=(
                    PAPER_MAP_ANSWER_SYSTEM_PROMPT
                    if task.task_type == ANSWER_MAP_QUESTION
                    else PAPER_MAP_PLAN_SYSTEM_PROMPT
                ),
                input_tokens=(
                    question.input_tokens
                    if question is not None
                    else paper_map.input_tokens
                    if paper_map is not None
                    else None
                ),
                output_tokens=(
                    question.output_tokens
                    if question is not None
                    else paper_map.output_tokens
                    if paper_map is not None
                    else None
                ),
                latency_ms=(
                    question.latency_ms
                    if question is not None
                    else paper_map.latency_ms
                    if paper_map is not None
                    else None
                ),
            )

        execution_kind, engine = _TASK_EXECUTION.get(
            task.task_type, ("local", "AcadematePaper background worker")
        )
        return TaskDetailResponse(
            id=task.id,
            task_type=task.task_type,
            status=task.status,
            description=_TASK_DESCRIPTIONS.get(
                task.task_type, "Runs one background paper-processing step."
            ),
            execution_kind=execution_kind,
            uses_llm=False,
            engine=engine,
            renderer_error=task.error_message,
            target_block_id=block.id if block is not None else None,
            target_block_type=block.block_type if block is not None else None,
            target_location=target_location,
            result_summary=(
                "Completed successfully."
                if task.status == TaskStatus.SUCCEEDED
                else "The task is queued or currently running."
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
                else "The task did not complete successfully."
            ),
        )


@router.api_route("/{paper_id}/assets/{asset_path:path}", methods=["GET", "HEAD"])
def get_paper_asset(paper_id: str, asset_path: str, request: Request) -> Response:
    """Serve validated manifest assets or the one explicit PDF-only source artifact."""
    pure_path = PurePosixPath(asset_path)
    if pure_path.is_absolute() or ".." in pure_path.parts or not pure_path.parts:
        raise HTTPException(status_code=404, detail="Paper asset not found.")
    normalized_path = pure_path.as_posix()
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        pdf_source = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == paper_id,
                PaperSourceModel.source_type == "ARXIV_PDF",
            )
        )
        source = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
    if normalized_path == ARXIV_PDF_ASSET_PATH and pdf_source is not None:
        if not _has_owned_source_storage_key(
            pdf_source, paper_id=paper_id
        ) or not request.app.state.storage.exists(pdf_source.storage_key):
            raise HTTPException(status_code=404, detail="Paper asset not found.")
        return storage_byte_response(
            request,
            storage=request.app.state.storage,
            storage_key=pdf_source.storage_key,
            byte_size=pdf_source.byte_size,
            media_type="application/pdf",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            },
        )
    if source is None:
        raise HTTPException(status_code=404, detail="Paper asset manifest not found.")
    if not _has_owned_source_storage_key(
        source, paper_id=paper_id
    ) or not request.app.state.storage.exists(source.storage_key):
        raise HTTPException(status_code=404, detail="Paper asset manifest not found.")
    with request.app.state.storage.get(source.storage_key) as manifest_stream:
        manifest = json.load(manifest_stream)
    manifest_files = manifest.get("files", []) if isinstance(manifest, dict) else []
    item = next(
        (
            candidate
            for candidate in manifest_files
            if isinstance(candidate, dict) and candidate.get("path") == normalized_path
        ),
        None,
    )
    if item is None or not str(item.get("mime_type", "")).startswith(("image/", "application/pdf")):
        raise HTTPException(status_code=404, detail="Paper asset not found.")
    storage_key = str(item.get("storage_key", ""))
    expected_storage_key = f"papers/{paper_id}/source/expanded/{normalized_path}"
    if storage_key != expected_storage_key or not request.app.state.storage.exists(storage_key):
        raise HTTPException(status_code=404, detail="Paper asset not found.")
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
    }
    media_type = str(item["mime_type"])
    if media_type.lower().partition(";")[0].strip() == "image/svg+xml":
        headers["Content-Security-Policy"] = _SOURCE_SVG_CONTENT_SECURITY_POLICY
    byte_size = item.get("byte_size")
    if not isinstance(byte_size, int) or byte_size < 0:
        raise HTTPException(status_code=409, detail="Paper asset size is invalid.")
    return storage_byte_response(
        request,
        storage=request.app.state.storage,
        storage_key=storage_key,
        byte_size=byte_size,
        media_type=media_type,
        headers=headers,
    )


def _source_download_stem(paper: PaperModel) -> str:
    canonical_id = f"{paper.arxiv_id}{paper.arxiv_version}"
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", canonical_id).strip("._-")
    return f"arxiv-{safe_id or paper.id}-source"


def _raw_source_extension(stream) -> str:
    signature = stream.read(512)
    stream.seek(0)
    if signature.startswith(b"\x1f\x8b"):
        return ".tar.gz"
    if signature.startswith(b"BZh"):
        return ".tar.bz2"
    if signature.startswith(b"\xfd7zXZ\x00"):
        return ".tar.xz"
    if len(signature) >= 262 and signature[257:262] == b"ustar":
        return ".tar"
    return ".source"


def _expanded_source_archive(
    *,
    paper_id: str,
    source: PaperSourceModel,
    storage,
):
    if not _has_owned_source_storage_key(source, paper_id=paper_id) or not storage.exists(
        source.storage_key
    ):
        raise HTTPException(status_code=404, detail="Paper source archive not found.")
    archive_key = f"papers/{paper_id}/derived/downloads/expanded-source-{source.sha256[:16]}.zip"
    if storage.exists(archive_key):
        with storage.get(archive_key) as stream:
            stream.seek(0, 2)
            return archive_key, stream.tell()

    with storage.get(source.storage_key) as manifest_stream:
        try:
            manifest = json.load(manifest_stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise HTTPException(
                status_code=409,
                detail="Expanded source manifest is invalid; please retry source import.",
            ) from error

    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files or len(files) > _EXPANDED_SOURCE_DOWNLOAD_MAX_FILES:
        raise HTTPException(
            status_code=409,
            detail="Expanded source is incomplete; please retry source import.",
        )

    total_bytes = 0
    seen: set[str] = set()
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as temporary:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for item in files:
                if not isinstance(item, dict):
                    raise HTTPException(
                        status_code=409,
                        detail="Expanded source is incomplete; please retry source import.",
                    )
                raw_path = item.get("path")
                byte_size = item.get("byte_size")
                expected_sha256 = item.get("sha256")
                storage_key = item.get("storage_key")
                if (
                    not isinstance(raw_path, str)
                    or not isinstance(storage_key, str)
                    or not isinstance(expected_sha256, str)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Expanded source is incomplete; please retry source import.",
                    )
                pure_path = PurePosixPath(raw_path)
                normalized_path = pure_path.as_posix()
                if (
                    pure_path.is_absolute()
                    or ".." in pure_path.parts
                    or not pure_path.parts
                    or "\\" in raw_path
                    or any(ord(character) < 32 for character in raw_path)
                    or normalized_path != raw_path
                    or normalized_path in seen
                    or not isinstance(byte_size, int)
                    or byte_size < 0
                    or storage_key != f"papers/{paper_id}/source/expanded/{normalized_path}"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Expanded source is incomplete; please retry source import.",
                    )
                total_bytes += byte_size
                if total_bytes > _EXPANDED_SOURCE_DOWNLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Expanded source is too large to package for download.",
                    )
                if not storage.exists(storage_key):
                    raise HTTPException(
                        status_code=409,
                        detail="Expanded source is incomplete; please retry source import.",
                    )
                seen.add(normalized_path)
                with (
                    storage.get(storage_key) as source_stream,
                    archive.open(normalized_path, mode="w") as destination,
                ):
                    digest = hashlib.sha256()
                    copied_bytes = 0
                    while chunk := source_stream.read(64 * 1024):
                        copied_bytes += len(chunk)
                        digest.update(chunk)
                        destination.write(chunk)
                    if copied_bytes != byte_size or digest.hexdigest() != expected_sha256:
                        raise HTTPException(
                            status_code=409,
                            detail="Expanded source is incomplete; please retry source import.",
                        )
        temporary.seek(0)
        content_length = temporary.seek(0, 2)
        temporary.seek(0)
        storage.put(archive_key, temporary)
    return archive_key, content_length


@router.api_route(
    "/{paper_id}/sources/{source_type}/download",
    methods=["GET", "HEAD"],
)
def download_paper_source(
    paper_id: str,
    source_type: str,
    request: Request,
) -> Response:
    """Download a paper's original source archive or its validated expanded files."""
    if source_type not in _DOWNLOADABLE_SOURCE_TYPES:
        raise HTTPException(status_code=404, detail="Paper source download not found.")
    with request.app.state.session_factory() as session:
        paper = _require_paper(session, request, paper_id).paper
        source = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == paper_id,
                PaperSourceModel.source_type == source_type,
            )
        )
        if source is None:
            raise HTTPException(status_code=404, detail="Paper source download not found.")
        filename_stem = _source_download_stem(paper)

    storage = request.app.state.storage
    if source_type in {"ARXIV_TARBALL", "ARXIV_PDF"}:
        if not _has_owned_source_storage_key(source, paper_id=paper_id) or not storage.exists(
            source.storage_key
        ):
            raise HTTPException(status_code=404, detail="Paper source archive not found.")
        storage_key = source.storage_key
        if source_type == "ARXIV_PDF":
            extension = ".pdf"
        else:
            with storage.get(storage_key) as stream:
                extension = _raw_source_extension(stream)
        filename = f"{filename_stem}{extension}"
        media_type = source.mime_type or "application/octet-stream"
        content_length = source.byte_size
    else:
        storage_key, content_length = _expanded_source_archive(
            paper_id=paper_id,
            source=source,
            storage=storage,
        )
        filename = f"{filename_stem}-expanded.zip"
        media_type = "application/zip"

    return storage_byte_response(
        request,
        storage=storage,
        storage_key=storage_key,
        byte_size=content_length,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.api_route(
    "/{paper_id}/blocks/{block_id}/render-assets/{asset_name}",
    methods=["GET", "HEAD"],
)
def get_block_render_asset(
    paper_id: str,
    block_id: str,
    asset_name: str,
    request: Request,
) -> Response:
    """Serve only the derived asset recorded on this exact content block."""
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    toolchain_fingerprint = request.app.state.settings.toolchain_fingerprint
    pure_name = PurePosixPath(asset_name)
    if pure_name.is_absolute() or len(pure_name.parts) != 1 or pure_name.name != asset_name:
        raise HTTPException(status_code=404, detail="Rendered block asset not found.")
    with request.app.state.session_factory() as session:
        _require_paper(session, request, paper_id)
        block = session.scalar(
            select(ContentBlockModel)
            .options(
                with_loader_criteria(
                    TranslationModel,
                    _translation_visibility_predicate(principal),
                    include_aliases=True,
                )
            )
            .where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == paper_id,
            )
        )
        if block is None:
            raise HTTPException(status_code=404, detail="Rendered block asset not found.")
        effective_render = effective_block_render(
            session,
            workspace_id=principal.active_workspace_id,
            block=block,
        )
        render_payload = effective_render.payload

        def recorded_formula_asset(value: object) -> dict[str, Any] | None:
            if isinstance(value, list):
                return next(
                    (
                        found
                        for item in value
                        if (found := recorded_formula_asset(item)) is not None
                    ),
                    None,
                )
            if not isinstance(value, dict):
                return None
            latex_render = value.get("latex_render")
            candidate = latex_render.get("asset") if isinstance(latex_render, dict) else None
            if isinstance(candidate, dict) and candidate.get("path") == asset_name:
                if (
                    toolchain_fingerprint is not None
                    and latex_render.get("toolchain_fingerprint") != toolchain_fingerprint
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Rendered block asset belongs to an older toolchain.",
                    )
                return candidate
            return next(
                (
                    found
                    for item in value.values()
                    if (found := recorded_formula_asset(item)) is not None
                ),
                None,
            )

        if block.block_type in {"TABLE", "FIGURE", "ALGORITHM", "LATEX_BLOCK"}:
            render_key = (
                "figure_render"
                if block.block_type == "FIGURE"
                else "table_render"
                if block.block_type == "TABLE"
                else "latex_block_render"
            )
            render_state = render_payload.get(render_key, {})
            recorded_asset = render_state.get("asset") if isinstance(render_state, dict) else None
            if (
                isinstance(recorded_asset, dict)
                and recorded_asset.get("path") == asset_name
                and toolchain_fingerprint is not None
                and render_state.get("toolchain_fingerprint") != toolchain_fingerprint
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Rendered block asset belongs to an older toolchain.",
                )
            asset = render_state.get("asset", {}) if isinstance(render_state, dict) else {}
            asset_directory = (
                "figures"
                if block.block_type == "FIGURE"
                else "tables"
                if block.block_type == "TABLE"
                else "latex-blocks"
            )
        else:
            asset = recorded_formula_asset(render_payload) or {}
            if not asset:
                for translation in (
                    item
                    for item in block.translations
                    if item.workspace_id == principal.active_workspace_id
                    and (
                        item.prompt_kind == "global"
                        or (
                            item.prompt_kind in {"custom", "paper"}
                            and item.requested_by_user_id == principal.user_id
                        )
                    )
                ):
                    asset = recorded_formula_asset(translation.translated_payload_json) or {}
                    if asset:
                        break
            asset_directory = "formulas"
        if not isinstance(asset, dict) or asset.get("path") != asset_name:
            raise HTTPException(status_code=404, detail="Rendered block asset not found.")
        mime_type = str(asset.get("mime_type", ""))
        if mime_type not in {"image/svg+xml", "application/pdf", "image/png"}:
            raise HTTPException(status_code=404, detail="Rendered block asset not found.")
    storage_key = (
        workspace_render_asset_key(
            workspace_id=principal.active_workspace_id,
            paper_id=paper_id,
            asset_directory=asset_directory,
            asset_name=asset_name,
        )
        if asset.get("scope") == "workspace"
        else f"papers/{paper_id}/derived/{asset_directory}/{asset_name}"
    )
    if not request.app.state.storage.exists(storage_key):
        raise HTTPException(status_code=404, detail="Rendered block asset not found.")
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Disposition": "inline",
        "Content-Security-Policy": _SOURCE_SVG_CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
    }
    byte_size = asset.get("byte_size")
    if not isinstance(byte_size, int) or byte_size < 0:
        raise HTTPException(status_code=409, detail="Rendered block asset size is invalid.")
    return storage_byte_response(
        request,
        storage=request.app.state.storage,
        storage_key=storage_key,
        byte_size=byte_size,
        media_type=mime_type,
        headers=headers,
    )


@router.post(
    "/{paper_id}/blocks/{block_id}/latex-render",
    response_model=RenderRepairTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_latex_formula_render(
    paper_id: str,
    block_id: str,
    payload: LatexFormulaRenderRequest,
    request: Request,
) -> RenderRepairTaskResponse:
    """Queue the deterministic tier-3 renderer; this endpoint never invokes an LLM."""

    try:
        require_secret_free_task_data(payload.model_dump(), field_name="payload")
    except TaskSecretError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Task diagnostics must not contain credential material.",
        ) from error

    principal = require_request_permission(request, Permission.TASK_WRITE)
    toolchain_fingerprint = request.app.state.settings.toolchain_fingerprint
    cache_identity = toolchain_cache_identity(
        LATEX_FORMULA_RENDERER_VERSION,
        toolchain_fingerprint,
    )
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == paper_id,
            )
        )
        if block is None and block_id == abstract_block_id(paper_id):
            block = ensure_abstract_block(session, workspace_paper.paper)
            session.flush()
        if block is None:
            raise HTTPException(status_code=404, detail="Content block not found.")
        if block.block_type == "ABSTRACT":
            hydrated = hydrate_inline_latex_render_payload(
                block.render_payload_json,
                block.source_text or "",
            )
            if hydrated != block.render_payload_json:
                block.render_payload_json = hydrated
                session.flush()
        block_render = effective_block_render(
            session,
            workspace_id=principal.active_workspace_id,
            block=block,
        )

        selector = (
            payload.formula_selector.model_dump(exclude_none=True)
            if payload.formula_selector is not None
            else None
        )
        target_kind = "BLOCK"
        translation: TranslationModel | None = None
        if selector is None:
            if block.block_type != "EQUATION":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="An exact inline formula selector is required for this block.",
                )
            selector_identity = "equation"
        else:
            selector.setdefault("text_hash", formula_text_hash(str(selector["exact_latex"])))
            target_kind = str(selector.get("target", "BLOCK"))
            target_payload: object = block_render.payload
            if target_kind == "TRANSLATION":
                translation_id = str(selector.get("translation_id", ""))
                translation = session.scalar(
                    select(TranslationModel).where(
                        TranslationModel.id == translation_id,
                        TranslationModel.block_id == block.id,
                        TranslationModel.status == TranslationStatus.SUCCEEDED,
                        _translation_visibility_predicate(principal),
                    )
                )
                if translation is None or translation.source_hash != block.content_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The translated formula revision is no longer current.",
                    )
                target_payload = translation.translated_payload_json
            try:
                selected_formula(target_payload, selector)
                selector_identity = formula_selector_key(selector)
            except FormulaSelectorError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(error),
                ) from error

        source = session.scalar(
            select(PaperSourceModel).where(
                PaperSourceModel.paper_id == paper_id,
                PaperSourceModel.source_type == "EXPANDED_SOURCE",
            )
        )
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The paper source manifest is unavailable.",
            )
        target_identity = f"translation:{translation.id}" if translation is not None else "block"
        idempotency_key = (
            f"paper:{paper_id}:render_latex_formula:{cache_identity}:"
            f"{block.id}:{target_identity}:"
            f"{block.content_hash}:{selector_identity}"
        )
        task_scope = _formula_render_scope(
            principal,
            translation,
            workspace_overlay=block_render.overlay is not None and translation is None,
        )
        task = session.scalar(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.scope_key == task_scope.scope_key,
                ProcessingTaskModel.idempotency_key == idempotency_key,
            )
        )
        if task is None:
            task = ProcessingTaskModel(
                visibility=task_scope.visibility,
                scope_key=task_scope.scope_key,
                workspace_id=task_scope.workspace_id,
                requested_by_user_id=task_scope.requested_by_user_id,
                credential_id=task_scope.credential_id,
                paper_id=paper_id,
                task_type=RENDER_LATEX_FORMULA,
                priority=35,
                idempotency_key=idempotency_key,
                payload_json={
                    "block_id": block.id,
                    "content_hash": block.content_hash,
                    "formula_selector": selector,
                    "formula_target": target_kind,
                    "translation_id": translation.id if translation is not None else None,
                    "error_message": payload.error_message,
                    "manifest_key": source.storage_key,
                    "root_file": str(block.source_locator_json.get("root_file", "main.tex")),
                    "preamble_key": (
                        f"papers/{paper_id}/derived/parser/preamble-{source.sha256}.tex"
                    ),
                    "renderer_version": LATEX_FORMULA_RENDERER_VERSION,
                    **(
                        {"toolchain_fingerprint": toolchain_fingerprint}
                        if toolchain_fingerprint is not None
                        else {}
                    ),
                },
                max_attempts=1,
            )
            session.add(task)
            session.flush()
        elif task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            task.status = TaskStatus.QUEUED
            task.attempt_count = 0
            task.available_at = now_utc()
            task.started_at = None
            task.finished_at = None
            task.error_code = None
            task.error_message = None
            task.result_json = None
        session.commit()
        return RenderRepairTaskResponse(
            queued=task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING},
            applied=task.status == TaskStatus.SUCCEEDED,
            task_id=task.id,
            status=task.status,
        )


@router.post(
    "/{paper_id}/blocks/{block_id}/render-failure",
    response_model=RenderRepairTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def report_render_failure(
    paper_id: str,
    block_id: str,
    payload: RenderFailureRequest,
    request: Request,
) -> RenderRepairTaskResponse:
    try:
        require_secret_free_task_data(payload.model_dump(), field_name="payload")
    except TaskSecretError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Task diagnostics must not contain credential material.",
        ) from error
    principal = require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
        if payload.automatic and not get_automatic_ai_repair_enabled(
            session,
            user_id=principal.user_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Automatic AI-assisted repair is disabled in workspace settings. "
                    "Use the block repair action to start it manually."
                ),
            )
        if payload.automatic:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Automatic AI repair is not permitted for formulas. Try the original "
                    "LaTeX renderer first, then choose AI-assisted repair manually only if "
                    "that deterministic task fails."
                ),
            )
        block = session.scalar(
            select(ContentBlockModel).where(
                ContentBlockModel.id == block_id,
                ContentBlockModel.paper_id == paper_id,
            )
        )
        if block is None and block_id == abstract_block_id(paper_id):
            block = ensure_abstract_block(session, workspace_paper.paper)
            session.flush()
        if block is None:
            raise HTTPException(status_code=404, detail="Content block not found.")
        if block.block_type == "ABSTRACT":
            hydrated = hydrate_inline_latex_render_payload(
                block.render_payload_json,
                block.source_text or "",
            )
            if hydrated != block.render_payload_json:
                block.render_payload_json = hydrated
                session.flush()
        block_render = effective_block_render(
            session,
            workspace_id=principal.active_workspace_id,
            block=block,
        )
        formula_selector = (
            payload.formula_selector.model_dump(exclude_none=True)
            if payload.formula_selector is not None
            else None
        )
        if payload.automatic and formula_selector is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Automatic repair is only supported for verified inline formulas.",
            )
        target_payload: object = block_render.payload
        target_revision = block.content_hash
        target_kind = "BLOCK"
        translation: TranslationModel | None = None
        if formula_selector is not None:
            if payload.renderer != "katex":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Inline formula selectors are only valid for KaTeX failures.",
                )
            target_kind = str(formula_selector.get("target", "BLOCK"))
            formula_selector.setdefault(
                "text_hash",
                formula_text_hash(str(formula_selector["exact_latex"])),
            )
            if target_kind == "TRANSLATION":
                translation_id = str(formula_selector.get("translation_id", ""))
                translation = session.scalar(
                    select(TranslationModel).where(
                        TranslationModel.id == translation_id,
                        TranslationModel.block_id == block.id,
                        TranslationModel.status == TranslationStatus.SUCCEEDED,
                        _translation_visibility_predicate(principal),
                    )
                )
                if translation is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Translation not found.",
                    )
                if translation.source_hash != block.content_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The translated formula revision is no longer current.",
                    )
                target_payload = translation.translated_payload_json
                target_revision = translation.input_hash
            try:
                selected_formula(target_payload, formula_selector)
                selector_key = formula_selector_key(formula_selector)
            except FormulaSelectorError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(error),
                ) from error
            target_identity = (
                f"translation:{translation.id}" if translation is not None else "block"
            )
            local_attempts = session.scalars(
                select(ProcessingTaskModel).where(
                    ProcessingTaskModel.paper_id == paper_id,
                    ProcessingTaskModel.task_type == RENDER_LATEX_FORMULA,
                    ProcessingTaskModel.status == TaskStatus.FAILED,
                    ProcessingTaskModel.payload_json["block_id"].as_string() == block.id,
                    ProcessingTaskModel.payload_json["content_hash"].as_string()
                    == block.content_hash,
                    _task_visibility_predicate(principal),
                )
            ).all()
            local_failure = next(
                (
                    candidate
                    for candidate in local_attempts
                    if isinstance(candidate.payload_json, dict)
                    and candidate.payload_json.get("formula_target") == target_kind
                    and (
                        formula_selector_key(candidate.payload_json.get("formula_selector"))
                        == selector_key
                    )
                ),
                None,
            )
            if local_failure is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Use the paper's original LaTeX renderer first. AI-assisted formula "
                        "repair is available only after that deterministic task fails."
                    ),
                )
            idempotency_key = (
                f"paper:{paper_id}:repair_render:{block.id}:{target_identity}:"
                f"{target_revision}:{payload.renderer}:{selector_key}"
            )
        else:
            if payload.renderer == "katex" and block.block_type != "EQUATION":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="A verified inline formula selector is required for this block.",
                )
            source_latex = _block_source_latex(block, max_chars=None) or ""
            if (
                block.block_type == "TABLE"
                and payload.renderer in {"table", "latex"}
                and len(source_latex) > RENDER_REPAIR_MAX_LATEX_CHARS
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        "This table is too large for a lossless AI repair. "
                        "Its original LaTeX and accessible fallback remain available."
                    ),
                )
            idempotency_key = (
                f"paper:{paper_id}:repair_render:{block.id}:{block.content_hash}:{payload.renderer}"
            )
        llm_credential = request_llm_task_credential(request, user_id=principal.user_id)
        assert llm_credential is not None
        repair_scope_factory = (
            TaskScope.user_private
            if translation is not None and translation.prompt_kind in {"custom", "paper"}
            else TaskScope.workspace_derived
        )
        repair_scope = repair_scope_factory(
            workspace_id=principal.active_workspace_id,
            requested_by_user_id=principal.user_id,
            credential_id=llm_credential.credential_id,
        )
        existing = session.scalar(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.scope_key == repair_scope.scope_key,
                ProcessingTaskModel.idempotency_key == idempotency_key,
            )
        )
        content_hash = block.content_hash
        active_count = session.scalar(
            select(func.count())
            .select_from(ProcessingTaskModel)
            .where(
                ProcessingTaskModel.paper_id == paper_id,
                ProcessingTaskModel.task_type == REPAIR_RENDER,
                ProcessingTaskModel.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
                ProcessingTaskModel.scope_key == repair_scope.scope_key,
            )
        )
        if existing is not None and existing.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            return RenderRepairTaskResponse(
                queued=False,
                applied=False,
                task_id=existing.id,
                status=existing.status,
            )
        if payload.automatic and existing is not None:
            return RenderRepairTaskResponse(
                queued=False,
                applied=existing.status == TaskStatus.SUCCEEDED,
                task_id=existing.id,
                status=existing.status,
            )
        active_limit = _AUTOMATIC_INLINE_REPAIR_QUEUE_LIMIT if payload.automatic else 3
        if (active_count or 0) >= active_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Automatic inline formula repair queue is full for this paper."
                    if payload.automatic
                    else "At most three render repairs may be active for one paper."
                ),
            )
        if existing is not None:
            existing.visibility = repair_scope.visibility
            existing.scope_key = repair_scope.scope_key
            existing.workspace_id = repair_scope.workspace_id
            existing.requested_by_user_id = repair_scope.requested_by_user_id
            existing.credential_id = repair_scope.credential_id
            existing.status = TaskStatus.QUEUED
            existing.attempt_count = 0
            existing.max_attempts = 2 if payload.automatic else 1
            existing.available_at = now_utc()
            existing.started_at = None
            existing.finished_at = None
            existing.lease_expires_at = None
            existing.worker_id = None
            existing.error_code = None
            existing.error_message = None
            existing.result_json = None
            existing.payload_json = {
                "block_id": block_id,
                "content_hash": content_hash,
                "renderer": payload.renderer,
                "error_message": payload.error_message,
                "trigger": ("automatic-browser-formula-failure" if payload.automatic else "manual"),
                **(
                    {
                        "formula_selector": formula_selector,
                        "formula_target": target_kind,
                        "translation_id": translation.id if translation is not None else None,
                        "translation_input_hash": (
                            translation.input_hash if translation is not None else None
                        ),
                    }
                    if formula_selector is not None
                    else {}
                ),
            }
            session.commit()
            return RenderRepairTaskResponse(
                queued=True,
                applied=False,
                task_id=existing.id,
                status=TaskStatus.QUEUED,
            )
    task = request.app.state.task_queue.enqueue(
        REPAIR_RENDER,
        {
            "block_id": block_id,
            "content_hash": content_hash,
            "renderer": payload.renderer,
            "error_message": payload.error_message,
            "trigger": ("automatic-browser-formula-failure" if payload.automatic else "manual"),
            **(
                {
                    "formula_selector": formula_selector,
                    "formula_target": target_kind,
                    "translation_id": translation.id if translation is not None else None,
                    "translation_input_hash": (
                        translation.input_hash if translation is not None else None
                    ),
                }
                if formula_selector is not None
                else {}
            ),
        },
        priority=40,
        idempotency_key=idempotency_key,
        paper_id=paper_id,
        max_attempts=2 if payload.automatic else 1,
        scope=repair_scope,
    )
    with request.app.state.session_factory() as session:
        stored = session.scalar(
            select(ProcessingTaskModel).where(
                ProcessingTaskModel.id == task.id,
                ProcessingTaskModel.visibility == repair_scope.visibility,
                ProcessingTaskModel.scope_key == repair_scope.scope_key,
                ProcessingTaskModel.workspace_id == repair_scope.workspace_id,
                ProcessingTaskModel.requested_by_user_id == repair_scope.requested_by_user_id,
            )
        )
        task_status = stored.status if stored else TaskStatus.QUEUED
    return RenderRepairTaskResponse(
        queued=existing is None,
        applied=False,
        task_id=task.id,
        status=task_status,
    )


@router.get("/{paper_id}", response_model=PaperResponse)
def get_paper(paper_id: str, request: Request) -> PaperResponse:
    principal = require_request_permission(request, Permission.LIBRARY_READ)
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(session, request, paper_id)
        abstract = ensure_abstract_block(session, workspace_paper.paper)
        session.commit()
        return _paper_response(
            workspace_paper.paper,
            workspace_paper.library_item,
            principal,
            abstract_render_payload=(
                effective_block_render(
                    session,
                    workspace_id=principal.active_workspace_id,
                    block=abstract,
                ).payload
                if abstract is not None
                else None
            ),
        )


@router.post("/{paper_id}/retry", response_model=RetryResponse)
def retry_paper(paper_id: str, request: Request) -> RetryResponse:
    principal = require_request_permission(request, Permission.TASK_WRITE)
    with request.app.state.session_factory() as session:
        _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
    retried = request.app.state.task_queue.retry_failed(
        paper_id,
        requested_by_user_id=principal.user_id,
        visible_scope_keys=frozenset(
            {
                TaskScope.global_derived().scope_key,
                TaskScope.workspace_derived(
                    workspace_id=principal.active_workspace_id,
                    requested_by_user_id=principal.user_id,
                ).scope_key,
                TaskScope.user_private(
                    workspace_id=principal.active_workspace_id,
                    requested_by_user_id=principal.user_id,
                ).scope_key,
            }
        ),
    )
    with request.app.state.session_factory() as session:
        workspace_paper = _require_paper(
            session,
            request,
            paper_id,
            permission=Permission.TASK_WRITE,
        )
        if retried:
            workspace_paper.paper.processing_health = ProcessingHealth.PARTIAL
            session.commit()
        return RetryResponse(
            retried_tasks=retried,
            paper=_paper_response(
                workspace_paper.paper,
                workspace_paper.library_item,
                principal,
            ),
        )
