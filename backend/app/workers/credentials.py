from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any, cast

from app.application.credentials import (
    CredentialReentryRequiredError,
    WorkerCredentialResolver,
)
from app.domain.paper_map import PaperMapGenerator
from app.domain.ports import QueuedTask
from app.domain.qa import QuestionAnswerer
from app.domain.translation import Translator
from app.infrastructure.llm import (
    RenderRepairClient,
    build_paper_map_generator,
    build_question_answerer,
    build_render_repair_client,
    build_translator,
)
from app.infrastructure.llm.render_repair import ReasoningEffort

_TRANSLATION_TASK = "TRANSLATE_BLOCK"
_QA_TASK = "ANSWER_QUESTION"
_PAPER_MAP_TASKS = frozenset({"PLAN_PAPER_MAP", "ANSWER_MAP_QUESTION"})
_REPAIR_TASK = "REPAIR_RENDER"
_LLM_TASKS = frozenset({_TRANSLATION_TASK, _QA_TASK, _REPAIR_TASK, *_PAPER_MAP_TASKS})
_UNRESOLVED = object()


class _LazyTaskProvider:
    """Resolve one task-owned credential only when its provider is actually used.

    Worker handlers validate current membership, workspace ownership, resource identity, and
    generation before their first provider method call. Keeping resolution behind this proxy makes
    those checks the credential-decryption boundary while retaining the existing handler rechecks
    before publication.
    """

    def __init__(
        self,
        factory: Callable[[], object],
        before_call: Callable[[], None] | None = None,
    ) -> None:
        self._factory = factory
        self._before_call = before_call
        self._provider: object = _UNRESOLVED
        self._lock = Lock()

    def _resolve(self) -> object:
        if self._provider is _UNRESOLVED:
            with self._lock:
                if self._provider is _UNRESOLVED:
                    self._provider = self._factory()
        return self._provider

    def __getattr__(self, name: str) -> Any:
        if self._before_call is not None:
            self._before_call()  # Refuse before credential decryption/provider construction.
        attribute = getattr(self._resolve(), name)
        if not callable(attribute) or self._before_call is None:
            return attribute

        def invoke(*args: Any, **kwargs: Any) -> Any:
            # A caller may retain this bound method; cached providers still recheck each call.
            self._before_call()
            return attribute(*args, **kwargs)

        return invoke

    def __repr__(self) -> str:
        state = "unresolved" if self._provider is _UNRESOLVED else "resolved"
        return f"_LazyTaskProvider(state={state!r})"


@dataclass(frozen=True, slots=True)
class TaskLlmProviders:
    repairer: RenderRepairClient | None = None
    translator: Translator | None = None
    answerer: QuestionAnswerer | None = None
    paper_map_generator: PaperMapGenerator | None = None


@dataclass(frozen=True, slots=True)
class TaskLlmProviderFactory:
    """Build task-bound providers without decrypting credentials before resource validation."""

    credential_resolver: WorkerCredentialResolver | None
    environment_providers: TaskLlmProviders
    translation_timeout_seconds: int
    qa_timeout_seconds: int
    paper_map_timeout_seconds: int
    repair_reasoning_effort: ReasoningEffort
    before_provider_call: Callable[[QueuedTask], None] | None = None

    def for_task(self, task: QueuedTask) -> TaskLlmProviders:
        if task.task_type not in _LLM_TASKS:
            return TaskLlmProviders()
        if self.credential_resolver is None:
            if task.credential_id is not None:
                # Compose has no protected-store adapter and may only consume its legacy
                # environment-key tasks, which intentionally carry no credential reference.
                raise CredentialReentryRequiredError
            if self.before_provider_call is None:
                return self.environment_providers

            def wrap(provider):
                return (
                    None
                    if provider is None
                    else _LazyTaskProvider(
                        lambda: provider,
                        lambda: self.before_provider_call(task),
                    )
                )

            return TaskLlmProviders(
                repairer=wrap(self.environment_providers.repairer),
                translator=wrap(self.environment_providers.translator),
                answerer=wrap(self.environment_providers.answerer),
                paper_map_generator=wrap(self.environment_providers.paper_map_generator),
            )

        guard = (lambda: self.before_provider_call(task)) if self.before_provider_call else None
        lazy_provider = _LazyTaskProvider(lambda: self._build_native_provider(task), guard)
        if task.task_type == _TRANSLATION_TASK:
            return TaskLlmProviders(translator=cast(Translator, lazy_provider))
        if task.task_type == _QA_TASK:
            return TaskLlmProviders(answerer=cast(QuestionAnswerer, lazy_provider))
        if task.task_type in _PAPER_MAP_TASKS:
            return TaskLlmProviders(paper_map_generator=cast(PaperMapGenerator, lazy_provider))
        return TaskLlmProviders(repairer=cast(RenderRepairClient, lazy_provider))

    def _build_native_provider(self, task: QueuedTask) -> object:
        if self.credential_resolver is None:  # pragma: no cover - guarded by for_task
            raise CredentialReentryRequiredError
        resolved = self.credential_resolver.resolve(task)
        provider = resolved.provider.strip().lower()
        api_base = (resolved.api_base or "").strip()
        model = (resolved.model or "").strip()
        if not provider or not api_base or not model:
            raise CredentialReentryRequiredError
        api_key = resolved.api_key.reveal()

        if task.task_type == _TRANSLATION_TASK:
            return build_translator(
                api_key=api_key,
                model=model,
                api_base=api_base,
                provider=provider,
                timeout_seconds=self.translation_timeout_seconds,
            )
        if task.task_type == _QA_TASK:
            return build_question_answerer(
                api_key=api_key,
                model=model,
                api_base=api_base,
                provider=provider,
                timeout_seconds=self.qa_timeout_seconds,
            )
        if task.task_type in _PAPER_MAP_TASKS:
            return build_paper_map_generator(
                api_key=api_key,
                model=model,
                api_base=api_base,
                provider=provider,
                timeout_seconds=self.paper_map_timeout_seconds,
            )
        return build_render_repair_client(
            api_key=api_key,
            model=model,
            api_base=api_base,
            provider=provider,
            reasoning_effort=self.repair_reasoning_effort,
        )
