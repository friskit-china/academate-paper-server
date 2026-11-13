import argparse
import logging
import sys
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import uuid4

from app.application.coordination import StartupBackfillCoordinator
from app.application.credentials import (
    CredentialService,
    WorkerCredentialResolver,
    select_llm_task_credential,
)
from app.application.paper_maps import (
    ANSWER_MAP_QUESTION,
    PLAN_PAPER_MAP,
)
from app.application.qa import ANSWER_QUESTION
from app.application.server_maintenance import MaintenanceTick
from app.application.settings import (
    AUTOMATIC_TRANSLATION_ENABLED_KEY,
    compose_translation_system_prompt,
    enabled_user_workspace_preferences,
    load_translation_system_prompt,
)
from app.application.task_access import TaskAccessRevoked, require_task_access
from app.application.translations import (
    TRANSLATE_BLOCK,
    backfill_translation_tasks,
    schedule_paper_translations,
    translation_task_scope_for_base_prompt,
)
from app.application.worker_runtime import (
    ActiveTaskClaims,
    WorkerLifecycle,
    drain_provider_futures,
    wait_for_worker_readiness,
)
from app.config import RuntimeConfigurationError, Settings, get_settings
from app.domain.os_adapters import ResolvedToolchain
from app.domain.ports import QueuedTask, TaskQueue
from app.domain.tasks import TaskLeaseLost, TaskVisibility
from app.domain.worker_runtime import StopRequest, WorkerRuntimeState
from app.infrastructure.arxiv import ArxivClient
from app.infrastructure.coordination import DatabaseCoordinationLock
from app.infrastructure.credentials import (
    CredentialAdapterError,
    DatabaseCredentialRepository,
    build_credential_adapters,
)
from app.infrastructure.db import build_engine, build_session_factory
from app.infrastructure.latex import LatexFigureRenderer, LatexTableRenderer
from app.infrastructure.llm import (
    PaperMapProviderError,
    QAProviderError,
    TranslationProviderError,
    build_paper_map_generator,
    build_question_answerer,
    build_render_repair_client,
    build_translator,
)
from app.infrastructure.platform import (
    PlatformAdapterError,
    ToolchainResolutionError,
    build_storage_key_mapper,
    build_toolchain_resolver,
    resolve_macos_acceptance_root,
)
from app.infrastructure.runtime_dependencies import (
    RuntimeDependencyError,
    WorkerRuntimeReport,
    require_worker_runtime,
)
from app.infrastructure.runtime_installation import DatabaseWorkerReadinessProbe
from app.infrastructure.runtime_verification import toolchain_fingerprint_is_accepted
from app.infrastructure.storage import LocalFileStorage
from app.infrastructure.tasks import DatabaseTaskQueue
from app.infrastructure.tasks.fencing import claim_fenced_session_factory
from app.infrastructure.worker_runtime import (
    DatabaseWorkerHeartbeatStore,
    SignalStopRequest,
    SystemClock,
    WorkerHeartbeatPump,
)
from app.workers.credentials import TaskLlmProviderFactory, TaskLlmProviders
from app.workers.handlers import (
    FETCH_METADATA,
    PARSE_SOURCE,
    RENDER_FIGURE,
    RENDER_LATEX_BLOCK,
    RENDER_LATEX_FORMULA,
    RENDER_TABLE,
    TaskAuthorizationError,
    authorize_task_scope,
    enqueue_failed_table_render_repairs,
    enqueue_missing_figure_render_tasks,
    enqueue_missing_latex_block_render_tasks,
    enqueue_missing_parse_tasks,
    enqueue_missing_table_render_tasks,
    enqueue_stale_formula_render_tasks,
    handle_task,
    mark_paper_failure,
    mark_qa_failure,
    mark_translation_failure,
    reconcile_deterministic_formula_tasks,
    reconcile_pdf_only_papers,
    reconcile_repaired_table_tasks,
    reconcile_superseded_formula_render_tasks,
    refresh_paper_health,
    schedule_automatic_render_repair,
)
from app.workers.paper_maps import mark_paper_map_failure

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("academatepaper.worker")


@dataclass(frozen=True, slots=True)
class _VerifiedWorkerRuntime:
    toolchain: ResolvedToolchain
    table_renderer: LatexTableRenderer
    figure_renderer: LatexFigureRenderer
    report: WorkerRuntimeReport


def _wait_for_verified_worker_runtime(
    settings: Settings,
    stop_request: StopRequest,
    *,
    external_acceptance_roots: bool = False,
) -> _VerifiedWorkerRuntime | None:
    """Resolve and functionally verify before any backfill or task claim can run."""

    while not stop_request.requested:
        try:
            acceptance_root = (
                resolve_macos_acceptance_root(
                    external_tex_bin_root=settings.toolchain_external_tex_bin_root,
                    external_usr_local_root=settings.toolchain_external_usr_local_root,
                )
                if external_acceptance_roots
                else None
            )
            toolchain = build_toolchain_resolver(
                mode=settings.toolchain_mode,
                managed_root=settings.toolchain_managed_root,
                manifest_path=settings.toolchain_manifest_path,
                external_tex_bin_root=settings.toolchain_external_tex_bin_root,
                external_usr_local_root=settings.toolchain_external_usr_local_root,
                external_acceptance_roots=external_acceptance_roots,
                external_acceptance_root=acceptance_root,
            ).resolve()
        except (PlatformAdapterError, ToolchainResolutionError, ValueError) as error:
            if not getattr(error, "retryable", False):
                raise
            logger.warning(
                "worker_runtime_unavailable failure=TOOLCHAIN_RESOLUTION_FAILED retry_seconds=%s",
                settings.worker_runtime_retry_seconds,
            )
            if stop_request.wait(settings.worker_runtime_retry_seconds):
                return None
            continue

        if not toolchain_fingerprint_is_accepted(settings, toolchain):
            if not toolchain.retry_on_failure:
                raise ToolchainResolutionError(
                    "Resolved toolchain fingerprint does not match launch preflight"
                )
            logger.warning(
                "worker_runtime_unavailable failure=TOOLCHAIN_FINGERPRINT_CHANGED retry_seconds=%s",
                settings.worker_runtime_retry_seconds,
            )
            if stop_request.wait(settings.worker_runtime_retry_seconds):
                return None
            continue

        commands = toolchain.commands
        table_renderer = LatexTableRenderer(
            latex_command=commands.latex,
            pdf_latex_command=commands.pdflatex,
            dvisvgm_command=commands.dvisvgm,
            pdf_to_svg_command=commands.pdftocairo,
            pdf_info_command=commands.pdfinfo,
            kpsewhich_command=commands.kpsewhich,
            timeout_seconds=settings.table_render_timeout_seconds,
            max_output_bytes=settings.table_render_max_output_bytes,
        )
        figure_renderer = LatexFigureRenderer(
            latex_command=commands.latex,
            pdf_latex_command=commands.pdflatex,
            unicode_latex_command=commands.xelatex,
            dvisvgm_command=commands.dvisvgm,
            pdf_to_svg_command=commands.pdftocairo,
            kpsewhich_command=commands.kpsewhich,
            timeout_seconds=settings.table_render_timeout_seconds,
            max_output_bytes=max(settings.table_render_max_output_bytes, 10_000_000),
        )
        try:
            report = require_worker_runtime(
                table_renderer,
                figure_renderer,
                toolchain=toolchain,
            )
        except RuntimeDependencyError:
            if not toolchain.retry_on_failure:
                raise
            logger.warning(
                "worker_runtime_unavailable failure=RUNTIME_SMOKE_FAILED retry_seconds=%s",
                settings.worker_runtime_retry_seconds,
            )
            if stop_request.wait(settings.worker_runtime_retry_seconds):
                return None
            continue
        return _VerifiedWorkerRuntime(
            toolchain=toolchain,
            table_renderer=table_renderer,
            figure_renderer=figure_renderer,
            report=report,
        )
    return None


def _fail_task_authorization_denial(
    task: QueuedTask,
    error: Exception,
    *,
    queue: TaskQueue,
    worker_id: str,
) -> bool:
    """Fail an unauthorized task terminally without touching its claimed domain target."""

    if not isinstance(error, (TaskAuthorizationError, TaskAccessRevoked)):
        return False
    queue.fail(
        task.id,
        type(error).__name__,
        str(error),
        retryable=False,
        expected_worker_id=worker_id,
        expected_lease_generation=task.lease_generation,
    )
    return True


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the AcadematePaper task worker.")
    parser.add_argument(
        "--accept-external-toolchain-roots",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    arguments = parser.parse_args(() if argv is None else argv)
    settings = get_settings()
    runtime_profile = settings.validate_runtime_configuration()
    try:
        credential_adapters = build_credential_adapters(
            runtime_profile=runtime_profile,
            os_secret_store_mode=settings.os_secret_store_mode,
            local_installation_id=settings.local_installation_id,
            credential_installation_id=settings.credential_installation_id,
            credential_master_key=settings.credential_master_key_material,
        )
    except CredentialAdapterError as error:
        raise RuntimeConfigurationError(str(error)) from error
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    credential_service = (
        CredentialService(
            repository=DatabaseCredentialRepository(session_factory),
            secret_store=credential_adapters.secret_store,
            cipher=credential_adapters.cipher,
        )
        if credential_adapters is not None
        else None
    )
    credential_resolver = (
        WorkerCredentialResolver(credential_service) if credential_service is not None else None
    )
    stop_request = SignalStopRequest()
    stop_request.install()
    readiness = DatabaseWorkerReadinessProbe(session_factory, runtime_profile)
    if not wait_for_worker_readiness(
        readiness,
        stop_request,
        poll_interval_seconds=settings.worker_setup_poll_seconds,
    ):
        engine.dispose()
        logger.info("worker_stopped_before_setup_ready")
        return

    clock = SystemClock()
    worker_id = str(uuid4())
    lifecycle = WorkerLifecycle(
        instance_id=worker_id,
        stop_request=stop_request,
        clock=clock,
        heartbeat_store=DatabaseWorkerHeartbeatStore(session_factory),
        heartbeat_interval_seconds=settings.worker_heartbeat_seconds,
        task_capacity=settings.translation_concurrency,
    )
    lifecycle.transition(WorkerRuntimeState.STARTING)
    task_lease_seconds = max(
        120,
        settings.translation_timeout_seconds + 30,
        settings.qa_timeout_seconds + 30,
        settings.paper_map_timeout_seconds + 30,
        settings.table_render_timeout_seconds + 30,
    )
    queue = DatabaseTaskQueue(
        session_factory,
        lease_seconds=task_lease_seconds,
        enforce_access=True,
    )
    active_claims = ActiveTaskClaims()
    maintenance_tick = MaintenanceTick(session_factory)

    def renew_active_claims() -> None:
        for task_id, lease_generation in active_claims.snapshot():
            if not queue.renew_claim(
                task_id,
                expected_worker_id=worker_id,
                expected_lease_generation=lease_generation,
            ):
                active_claims.discard(task_id, lease_generation)
        maintenance_tick()

    heartbeat_pump = WorkerHeartbeatPump(
        lifecycle,
        tick_seconds=max(
            0.1,
            min(settings.worker_heartbeat_seconds / 2, task_lease_seconds / 3),
        ),
        on_tick=renew_active_claims,
    )
    heartbeat_pump.start()
    startup_backfills = StartupBackfillCoordinator(DatabaseCoordinationLock(session_factory))
    storage = LocalFileStorage(
        settings.storage_root,
        path_mapper=build_storage_key_mapper(settings.storage_root),
    )
    storage.initialize()
    verified_runtime = _wait_for_verified_worker_runtime(
        settings,
        stop_request,
        external_acceptance_roots=arguments.accept_external_toolchain_roots,
    )
    if verified_runtime is None:
        lifecycle.transition(WorkerRuntimeState.STOPPED)
        heartbeat_pump.stop()
        engine.dispose()
        logger.info("worker_stopped_before_runtime_ready")
        return
    toolchain = verified_runtime.toolchain
    commands = toolchain.commands
    table_renderer = verified_runtime.table_renderer
    figure_renderer = verified_runtime.figure_renderer
    runtime_report = verified_runtime.report
    logger.info(
        "runtime_dependencies_ready resolver=%s manifest=%s fingerprint=%s texlive_profile=%s "
        "texlive_release=%s "
        "texlive_package_smoke=%s texlive_compile_smoke=%s latex=%s dvisvgm=%s "
        "table_render_smoke=%s figure_render_smoke=%s capabilities=%s",
        runtime_report.resolver,
        runtime_report.manifest_path,
        runtime_report.toolchain_fingerprint,
        runtime_report.texlive_profile,
        runtime_report.texlive_release,
        runtime_report.texlive_package_smoke,
        runtime_report.texlive_compile_smoke,
        runtime_report.commands.get("latex"),
        runtime_report.commands.get("dvisvgm"),
        runtime_report.table_render_smoke,
        runtime_report.figure_render_smoke,
        runtime_report.capabilities,
    )

    def run_deterministic_backfills():
        return (
            reconcile_pdf_only_papers(
                session_factory,
                storage,
                toolchain_commands=commands,
            ),
            enqueue_missing_parse_tasks(session_factory),
            reconcile_deterministic_formula_tasks(session_factory),
            reconcile_superseded_formula_render_tasks(
                session_factory,
                toolchain_fingerprint=toolchain.fingerprint,
            ),
            enqueue_stale_formula_render_tasks(
                session_factory,
                toolchain_fingerprint=toolchain.fingerprint,
            ),
            reconcile_repaired_table_tasks(session_factory),
            enqueue_missing_table_render_tasks(
                session_factory,
                storage,
                toolchain_fingerprint=toolchain.fingerprint,
            ),
            enqueue_missing_figure_render_tasks(
                session_factory,
                toolchain_fingerprint=toolchain.fingerprint,
            ),
            enqueue_missing_latex_block_render_tasks(
                session_factory,
                toolchain_fingerprint=toolchain.fingerprint,
            ),
        )

    (
        reconciled_pdf_only,
        backfilled,
        reconciled_formula_repairs,
        reconciled_formula_renders,
        formula_render_backfill,
        reconciled_table_renders,
        table_render_backfill,
        figure_render_backfill,
        latex_block_render_backfill,
    ) = startup_backfills.run(
        name="worker-startup-backfills",
        owner_id=worker_id,
        operation=run_deterministic_backfills,
    )
    arxiv = ArxivClient(
        api_base=settings.arxiv_api_base,
        source_base=settings.arxiv_source_base,
        abstract_base=settings.arxiv_abstract_base,
        user_agent=settings.arxiv_user_agent,
        timeout_seconds=settings.arxiv_timeout_seconds,
        metadata_max_bytes=settings.arxiv_metadata_max_bytes,
        source_max_bytes=settings.arxiv_source_max_bytes,
    )
    environment_repairer = (
        build_render_repair_client(
            api_key=settings.selected_llm_api_key,
            model=settings.llm_model,
            api_base=settings.llm_api_base,
            provider=settings.llm_provider,
            reasoning_effort=settings.repair_render_reasoning_effort,
        )
        if settings.llm_configured
        else None
    )
    environment_translator = (
        build_translator(
            api_key=settings.selected_llm_api_key,
            model=settings.llm_model,
            api_base=settings.llm_api_base,
            provider=settings.llm_provider,
            timeout_seconds=settings.translation_timeout_seconds,
        )
        if settings.translation_configured
        else None
    )
    environment_answerer = (
        build_question_answerer(
            api_key=settings.selected_llm_api_key,
            model=settings.llm_model,
            api_base=settings.llm_api_base,
            provider=settings.llm_provider,
            timeout_seconds=settings.qa_timeout_seconds,
        )
        if settings.qa_configured
        else None
    )
    environment_paper_map_generator = (
        build_paper_map_generator(
            api_key=settings.selected_llm_api_key,
            model=settings.llm_model,
            api_base=settings.llm_api_base,
            provider=settings.llm_provider,
            timeout_seconds=settings.paper_map_timeout_seconds,
        )
        if settings.paper_map_configured
        else None
    )
    def before_provider_call(task):
        queue.validate_provider_access(
            task.id, expected_worker_id=worker_id,
            expected_lease_generation=task.lease_generation,
        )

    provider_factory = TaskLlmProviderFactory(
        credential_resolver=credential_resolver,
        before_provider_call=before_provider_call,
        environment_providers=TaskLlmProviders(
            repairer=environment_repairer,
            translator=environment_translator,
            answerer=environment_answerer,
            paper_map_generator=environment_paper_map_generator,
        ),
        translation_timeout_seconds=settings.translation_timeout_seconds,
        qa_timeout_seconds=settings.qa_timeout_seconds,
        paper_map_timeout_seconds=settings.paper_map_timeout_seconds,
        repair_reasoning_effort=settings.repair_render_reasoning_effort,
    )

    def user_llm_credential(user_id: str):
        return select_llm_task_credential(
            credential_service=credential_service,
            user_id=user_id,
            environment_configured=settings.llm_configured,
            environment_provider=settings.llm_provider,
            environment_api_base=settings.llm_api_base,
            environment_model=settings.llm_model,
        )

    def automatic_translation_preferences(
        *,
        paper_id: str | None = None,
        task_session_factory=session_factory,
    ):
        with task_session_factory() as session:
            return enabled_user_workspace_preferences(
                session,
                key=AUTOMATIC_TRANSLATION_ENABLED_KEY,
                paper_id=paper_id,
            )

    def run_provider_backfills():
        translation_totals = {"eligible": 0, "created": 0, "cached": 0}
        for preference in automatic_translation_preferences():
            credential = user_llm_credential(preference.user_id)
            if credential is not None:
                base_system_prompt = load_translation_system_prompt(
                    session_factory,
                    user_id=preference.user_id,
                )
                result = backfill_translation_tasks(
                    session_factory,
                    language=settings.translation_target_language,
                    provider=credential.provider,
                    model=credential.model,
                    max_attempts=settings.translation_max_attempts,
                    task_scope=translation_task_scope_for_base_prompt(
                        workspace_id=preference.workspace_id,
                        requested_by_user_id=preference.user_id,
                        base_system_prompt=base_system_prompt,
                        credential_id=credential.credential_id,
                    ),
                    system_prompt=compose_translation_system_prompt(base_system_prompt),
                )
                for key in translation_totals:
                    translation_totals[key] += result[key]
        return (
            enqueue_failed_table_render_repairs(
                session_factory,
                credential_selector=user_llm_credential,
            ),
            translation_totals,
        )

    automatic_render_repairs, translation_backfill = startup_backfills.run(
        name="worker-startup-backfills",
        owner_id=worker_id,
        operation=run_provider_backfills,
    )
    verified_capabilities = tuple(
        capability
        for capability in toolchain.capabilities
        if runtime_report.capabilities.get(capability.value) == "passed"
    )
    lifecycle.transition(
        WorkerRuntimeState.READY,
        runtime_capabilities=verified_capabilities,
    )
    logger.info("worker_started worker_id=%s", worker_id)
    if reconciled_pdf_only:
        logger.info("pdf_only_papers_reconciled count=%s", reconciled_pdf_only)
    if backfilled:
        logger.info("parse_tasks_backfilled count=%s", backfilled)
    if reconciled_table_renders:
        logger.info("repaired_table_tasks_reconciled count=%s", reconciled_table_renders)
    if reconciled_formula_repairs:
        logger.info(
            "deterministic_formula_tasks_reconciled count=%s",
            reconciled_formula_repairs,
        )
    if reconciled_formula_renders:
        logger.info(
            "superseded_formula_render_tasks_reconciled count=%s",
            reconciled_formula_renders,
        )
    if formula_render_backfill:
        logger.info("formula_render_tasks_backfilled count=%s", formula_render_backfill)
    if table_render_backfill:
        logger.info("table_render_tasks_backfilled count=%s", table_render_backfill)
    if figure_render_backfill:
        logger.info("figure_render_tasks_backfilled count=%s", figure_render_backfill)
    if latex_block_render_backfill:
        logger.info("latex_block_render_tasks_backfilled count=%s", latex_block_render_backfill)
    if automatic_render_repairs:
        logger.info("automatic_render_repairs_backfilled count=%s", automatic_render_repairs)
    if translation_backfill["created"] or translation_backfill["cached"]:
        logger.info(
            "translation_tasks_backfilled eligible=%s created=%s cached=%s",
            translation_backfill["eligible"],
            translation_backfill["created"],
            translation_backfill["cached"],
        )

    translation_lock_guard = Lock()
    translation_input_locks: dict[str, Lock] = {}

    def execute_task(task) -> dict[str, Any]:
        task_session_factory = claim_fenced_session_factory(
            session_factory,
            task_id=task.id,
            worker_id=worker_id,
            lease_generation=task.lease_generation,
            lease_seconds=task_lease_seconds,
            enforce_access=True,
        )

        def execute_claim_mutation(mutation):
            return queue.execute_claim_mutation(
                task.id,
                mutation,
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )

        task_storage = storage.with_mutation_guard(execute_claim_mutation)
        # Validate persisted task metadata first. Native providers are task-bound lazy proxies;
        # credential resolution remains deferred until the handler has validated its live
        # membership, workspace resource, generation/hash, and idempotent short-circuits.
        authorize_task_scope(task)
        with session_factory() as access_session:
            require_task_access(access_session, task)
        task_providers = provider_factory.for_task(task)
        input_lock = None
        if task.task_type == TRANSLATE_BLOCK:
            input_hash = str(task.payload.get("input_hash", ""))
            with translation_lock_guard:
                input_lock = translation_input_locks.setdefault(input_hash, Lock())
        if input_lock is None:
            result = handle_task(
                task,
                arxiv,
                task_session_factory,
                task_storage,
                task_providers.repairer,
                table_renderer,
                figure_renderer,
                task_providers.translator,
                task_providers.answerer,
                task_providers.paper_map_generator,
                settings.qa_context_token_budget,
                settings.qa_max_output_tokens,
                settings.paper_map_context_token_budget,
                settings.paper_map_max_output_tokens,
                settings.paper_map_max_attempts,
                toolchain_commands=commands,
                toolchain_fingerprint=toolchain.fingerprint,
            )
        else:
            with input_lock:
                result = handle_task(
                    task,
                    arxiv,
                    task_session_factory,
                    task_storage,
                    task_providers.repairer,
                    table_renderer,
                    figure_renderer,
                    task_providers.translator,
                    task_providers.answerer,
                    task_providers.paper_map_generator,
                    settings.qa_context_token_budget,
                    settings.qa_max_output_tokens,
                    settings.paper_map_context_token_budget,
                    settings.paper_map_max_output_tokens,
                    settings.paper_map_max_attempts,
                    toolchain_commands=commands,
                    toolchain_fingerprint=toolchain.fingerprint,
                )
        if (
            task.task_type in {FETCH_METADATA, PARSE_SOURCE}
            and task.visibility is TaskVisibility.GLOBAL_DERIVED
            and task.paper_id
        ):
            translation_totals = {"eligible": 0, "created": 0, "cached": 0}
            for preference in automatic_translation_preferences(
                paper_id=task.paper_id,
                task_session_factory=task_session_factory,
            ):
                credential = user_llm_credential(preference.user_id)
                if credential is None:
                    continue
                base_system_prompt = load_translation_system_prompt(
                    task_session_factory,
                    user_id=preference.user_id,
                )
                scheduled = schedule_paper_translations(
                    task_session_factory,
                    paper_id=task.paper_id,
                    language=settings.translation_target_language,
                    provider=credential.provider,
                    model=credential.model,
                    max_attempts=settings.translation_max_attempts,
                    task_scope=translation_task_scope_for_base_prompt(
                        workspace_id=preference.workspace_id,
                        requested_by_user_id=preference.user_id,
                        base_system_prompt=base_system_prompt,
                        credential_id=credential.credential_id,
                    ),
                    system_prompt=compose_translation_system_prompt(base_system_prompt),
                )
                for key in translation_totals:
                    translation_totals[key] += scheduled[key]
            result["translation"] = translation_totals
        return result

    def _finish_owned_task(
        task,
        *,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ):
        if error is None:
            queue.succeed(
                task.id,
                result,
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )
            if task.task_type == TRANSLATE_BLOCK:
                refresh_paper_health(task.paper_id, session_factory)
            if task.task_type == TRANSLATE_BLOCK and result is not None:
                logger.info(
                    "translation_completed task_id=%s block_id=%s cache_hit=%s "
                    "input_tokens=%s output_tokens=%s latency_ms=%s "
                    "discarded_entities=%s attempts=%s",
                    task.id,
                    result.get("block_id"),
                    result.get("cache_hit"),
                    result.get("input_tokens"),
                    result.get("output_tokens"),
                    result.get("latency_ms"),
                    result.get("discarded_entities", 0),
                    task.attempt_count,
                )
            logger.info("task_succeeded task_id=%s type=%s", task.id, task.task_type)
            return
        if _fail_task_authorization_denial(
            task,
            error,
            queue=queue,
            worker_id=worker_id,
        ):
            logger.warning(
                "task_authorization_denied task_id=%s type=%s retry=False error=%s",
                task.id,
                task.task_type,
                type(error).__name__,
            )
            return
        task_session_factory = claim_fenced_session_factory(
            session_factory,
            task_id=task.id,
            worker_id=worker_id,
            lease_generation=task.lease_generation,
            lease_seconds=task_lease_seconds,
            enforce_access=True,
        )
        if task.task_type == TRANSLATE_BLOCK:
            retryable = error.retryable if isinstance(error, TranslationProviderError) else False
            retry_delay = (
                error.retry_after_seconds if isinstance(error, TranslationProviderError) else None
            )
            error_code = getattr(error, "error_type", type(error).__name__)
            expected_retry = retryable and task.attempt_count < task.max_attempts
            mark_translation_failure(
                task,
                task_session_factory,
                error,
                will_retry=expected_retry,
            )
            will_retry = queue.fail(
                task.id,
                error_code,
                str(error),
                retryable=retryable,
                retry_delay_seconds=retry_delay,
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )
        elif task.task_type == ANSWER_QUESTION:
            retryable = error.retryable if isinstance(error, QAProviderError) else False
            retry_delay = error.retry_after_seconds if isinstance(error, QAProviderError) else None
            error_code = getattr(error, "error_type", type(error).__name__)
            expected_retry = retryable and task.attempt_count < task.max_attempts
            mark_qa_failure(
                task,
                task_session_factory,
                error,
                will_retry=expected_retry,
            )
            will_retry = queue.fail(
                task.id,
                error_code,
                str(error),
                retryable=retryable,
                retry_delay_seconds=retry_delay,
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )
        elif task.task_type in {PLAN_PAPER_MAP, ANSWER_MAP_QUESTION}:
            retryable = error.retryable if isinstance(error, PaperMapProviderError) else False
            retry_delay = (
                error.retry_after_seconds if isinstance(error, PaperMapProviderError) else None
            )
            error_code = getattr(error, "error_type", type(error).__name__)
            expected_retry = retryable and task.attempt_count < task.max_attempts
            mark_paper_map_failure(
                task,
                task_session_factory,
                error,
                will_retry=expected_retry,
            )
            will_retry = queue.fail(
                task.id,
                error_code,
                str(error),
                retryable=retryable,
                retry_delay_seconds=retry_delay,
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )
        elif task.task_type == RENDER_TABLE:
            error_code = type(error).__name__
            will_retry = queue.fail(
                task.id,
                error_code,
                str(error),
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )
            if not will_retry:
                repair_task_id, created = schedule_automatic_render_repair(
                    session_factory,
                    render_task_id=task.id,
                    error_message=str(error),
                    credential_selector=user_llm_credential,
                )
                if repair_task_id is not None:
                    logger.info(
                        "automatic_render_repair_queued render_task_id=%s repair_task_id=%s "
                        "created=%s",
                        task.id,
                        repair_task_id,
                        created,
                    )
        elif task.task_type in {RENDER_FIGURE, RENDER_LATEX_BLOCK, RENDER_LATEX_FORMULA}:
            error_code = type(error).__name__
            will_retry = queue.fail(
                task.id,
                error_code,
                str(error),
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )
        else:
            error_code = type(error).__name__
            retryable = getattr(error, "retryable", True)
            expected_retry = retryable and task.attempt_count < task.max_attempts
            if task.task_type not in {"REPAIR_RENDER"}:
                mark_paper_failure(
                    task.paper_id,
                    task_session_factory,
                    will_retry=expected_retry,
                )
            will_retry = queue.fail(
                task.id,
                error_code,
                str(error),
                retryable=retryable,
                retry_delay_seconds=getattr(error, "retry_after_seconds", None),
                expected_worker_id=worker_id,
                expected_lease_generation=task.lease_generation,
            )
        logger.warning(
            "task_failed task_id=%s type=%s retry=%s error=%s",
            task.id,
            task.task_type,
            will_retry,
            error_code,
        )

    def finish_task(
        task,
        *,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        try:
            _finish_owned_task(task, result=result, error=error)
        except TaskLeaseLost:
            logger.warning(
                "task_result_discarded_after_lease_reclaim task_id=%s type=%s",
                task.id,
                task.task_type,
            )
        except TaskAccessRevoked as revoked:
            # Revocation may win between handler completion and queue.succeed, or while an
            # ordinary provider failure tries to publish scoped failure state. Only finish the
            # task row; never retry or publish to the now-unauthorized domain target.
            try:
                _fail_task_authorization_denial(task, revoked, queue=queue, worker_id=worker_id)
            except TaskLeaseLost:
                logger.warning("revoked_task_claim_already_lost task_id=%s", task.id)
            else:
                logger.warning("task_authorization_revoked task_id=%s retry=False", task.id)
        finally:
            active_claims.discard(task.id, task.lease_generation)

    executor = ThreadPoolExecutor(
        max_workers=settings.translation_concurrency,
        thread_name_prefix="translation",
    )
    translation_futures: dict[Future[dict[str, Any]], Any] = {}

    def publish_shutdown_heartbeat(
        state: WorkerRuntimeState,
        *,
        active_tasks: int,
    ) -> None:
        try:
            lifecycle.transition(state, active_tasks=active_tasks)
        except Exception as error:
            logger.warning(
                "worker_heartbeat_publish_failed state=%s error=%s",
                state.value,
                type(error).__name__,
            )

    try:
        while lifecycle.accepting_tasks:
            for future in [item for item in translation_futures if item.done()]:
                task = translation_futures.pop(future)
                try:
                    finish_task(task, result=future.result())
                except Exception as error:
                    finish_task(task, error=error)
            lifecycle.heartbeat(active_tasks=len(translation_futures))
            if not lifecycle.accepting_tasks:
                break
            if len(translation_futures) >= settings.translation_concurrency:
                lifecycle.wait_for_stop(min(settings.worker_poll_seconds, 0.1))
                continue
            task = queue.claim(worker_id)
            if task is None:
                lifecycle.wait_for_stop(settings.worker_poll_seconds)
                continue
            if lifecycle.stop_requested:
                queue.release_claim(
                    task.id,
                    expected_worker_id=worker_id,
                    expected_lease_generation=task.lease_generation,
                )
                break
            active_claims.add(task.id, task.lease_generation)
            logger.info("task_started task_id=%s type=%s", task.id, task.task_type)
            if task.task_type in {
                TRANSLATE_BLOCK,
                ANSWER_QUESTION,
                PLAN_PAPER_MAP,
                ANSWER_MAP_QUESTION,
            }:
                translation_futures[executor.submit(execute_task, task)] = task
                lifecycle.heartbeat(active_tasks=len(translation_futures))
                continue
            lifecycle.heartbeat(active_tasks=len(translation_futures) + 1)
            try:
                result = execute_task(task)
            except Exception as error:
                finish_task(task, error=error)
            else:
                finish_task(task, result=result)
            finally:
                lifecycle.heartbeat(active_tasks=len(translation_futures))
    finally:
        publish_shutdown_heartbeat(
            WorkerRuntimeState.DRAINING,
            active_tasks=len(translation_futures),
        )

        def publish_drain_progress(pending: int) -> None:
            try:
                lifecycle.heartbeat(active_tasks=pending)
            except Exception as error:
                logger.warning(
                    "worker_heartbeat_publish_failed state=%s error=%s",
                    WorkerRuntimeState.DRAINING.value,
                    type(error).__name__,
                )

        drain_result = drain_provider_futures(
            translation_futures,
            clock=clock,
            timeout_seconds=settings.worker_drain_timeout_seconds,
            on_progress=publish_drain_progress,
        )
        for future, task in drain_result.completed:
            try:
                finish_task(task, result=future.result())
            except Exception as error:
                finish_task(task, error=error)
        for future, _task in drain_result.abandoned:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        if drain_result.abandoned:
            publish_shutdown_heartbeat(
                WorkerRuntimeState.DRAIN_TIMEOUT,
                active_tasks=len(drain_result.abandoned),
            )
            logger.warning(
                "worker_drain_timeout abandoned_tasks=%s; leases remain reclaimable",
                len(drain_result.abandoned),
            )
        else:
            publish_shutdown_heartbeat(WorkerRuntimeState.STOPPED, active_tasks=0)
        heartbeat_pump.stop()
        engine.dispose()
        logger.info("worker_stopped")


if __name__ == "__main__":
    main(sys.argv[1:])
