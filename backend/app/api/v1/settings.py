from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.api.llm_credentials import request_llm_task_credential
from app.application.paper_map_questions import (
    add_user_question,
    all_question_definitions,
    delete_question,
    update_question,
    user_question_definitions,
)
from app.application.settings import (
    TRANSLATION_SYSTEM_PROMPT_MAX_LENGTH,
    TRANSLATION_SYSTEM_PROMPT_MIN_LENGTH,
    get_automatic_ai_repair_enabled,
    get_automatic_translation_enabled,
    get_translation_system_prompt,
    reset_translation_system_prompt,
    save_automatic_ai_repair_enabled,
    save_automatic_translation_enabled,
    save_translation_system_prompt,
)
from app.domain.auth import Permission

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(resolve_request_principal)],
)


class SettingsStatusResponse(BaseModel):
    environment: str
    storage_backend: str
    llm_configured: bool
    llm_provider: str | None
    llm_model: str | None
    repair_render_reasoning_effort: str
    translation_configured: bool
    translation_target_language: str
    translation_max_attempts: int
    qa_configured: bool
    qa_max_attempts: int
    translation_system_prompt: str
    automatic_ai_repair_enabled: bool
    automatic_translation_enabled: bool


class AutomaticAiRepairUpdateRequest(BaseModel):
    enabled: bool


class AutomaticAiRepairResponse(BaseModel):
    automatic_ai_repair_enabled: bool


class AutomaticTranslationUpdateRequest(BaseModel):
    enabled: bool


class AutomaticTranslationResponse(BaseModel):
    automatic_translation_enabled: bool


class TranslationPromptUpdateRequest(BaseModel):
    system_prompt: str = Field(
        min_length=TRANSLATION_SYSTEM_PROMPT_MIN_LENGTH,
        max_length=TRANSLATION_SYSTEM_PROMPT_MAX_LENGTH,
    )


class TranslationPromptResponse(BaseModel):
    system_prompt: str


class PaperMapQuestionLibraryItem(BaseModel):
    key: str
    text: str
    dimension: str
    paper_types: list[str]
    user_defined: bool


class PaperMapQuestionLibraryResponse(BaseModel):
    questions: list[PaperMapQuestionLibraryItem]


class PaperMapQuestionCreateRequest(BaseModel):
    text: str = Field(min_length=8, max_length=500)


class PaperMapQuestionUpdateRequest(BaseModel):
    text: str = Field(min_length=8, max_length=500)


def _question_library_response(
    session,
    *,
    user_id: str,
) -> PaperMapQuestionLibraryResponse:
    user_keys = {item.key for item in user_question_definitions(session, user_id=user_id)}
    return PaperMapQuestionLibraryResponse(
        questions=[
            PaperMapQuestionLibraryItem(
                key=item.key,
                text=item.text,
                dimension=item.dimension,
                paper_types=list(item.paper_types),
                user_defined=item.key in user_keys,
            )
            for item in all_question_definitions(session, user_id=user_id)
        ]
    )


@router.get("/status", response_model=SettingsStatusResponse)
def settings_status(request: Request) -> SettingsStatusResponse:
    principal = require_request_permission(request, Permission.SETTINGS_READ)
    settings = request.app.state.settings
    llm_credential = request_llm_task_credential(
        request,
        user_id=principal.user_id,
        required=False,
    )
    with request.app.state.session_factory() as session:
        return SettingsStatusResponse(
            environment=settings.app_env,
            storage_backend=settings.storage_backend,
            llm_configured=llm_credential is not None,
            llm_provider=llm_credential.provider if llm_credential is not None else None,
            llm_model=llm_credential.model if llm_credential is not None else None,
            repair_render_reasoning_effort=settings.repair_render_reasoning_effort,
            translation_configured=llm_credential is not None,
            translation_target_language=settings.translation_target_language,
            translation_max_attempts=settings.translation_max_attempts,
            qa_configured=llm_credential is not None,
            qa_max_attempts=settings.qa_max_attempts,
            translation_system_prompt=get_translation_system_prompt(
                session,
                user_id=principal.user_id,
            ),
            automatic_ai_repair_enabled=get_automatic_ai_repair_enabled(
                session,
                user_id=principal.user_id,
            ),
            automatic_translation_enabled=get_automatic_translation_enabled(
                session,
                user_id=principal.user_id,
            ),
        )


@router.put("/automatic-ai-repair", response_model=AutomaticAiRepairResponse)
def update_automatic_ai_repair(
    payload: AutomaticAiRepairUpdateRequest,
    request: Request,
) -> AutomaticAiRepairResponse:
    principal = require_request_permission(request, Permission.SETTINGS_WRITE)
    with request.app.state.session_factory() as session:
        enabled = save_automatic_ai_repair_enabled(
            session,
            payload.enabled,
            user_id=principal.user_id,
        )
        session.commit()
        return AutomaticAiRepairResponse(automatic_ai_repair_enabled=enabled)


@router.put("/automatic-translation", response_model=AutomaticTranslationResponse)
def update_automatic_translation(
    payload: AutomaticTranslationUpdateRequest,
    request: Request,
) -> AutomaticTranslationResponse:
    principal = require_request_permission(request, Permission.SETTINGS_WRITE)
    with request.app.state.session_factory() as session:
        enabled = save_automatic_translation_enabled(
            session,
            payload.enabled,
            user_id=principal.user_id,
        )
        session.commit()
        return AutomaticTranslationResponse(automatic_translation_enabled=enabled)


@router.put("/translation-prompt", response_model=TranslationPromptResponse)
def update_translation_prompt(
    payload: TranslationPromptUpdateRequest,
    request: Request,
) -> TranslationPromptResponse:
    principal = require_request_permission(request, Permission.SETTINGS_WRITE)
    with request.app.state.session_factory() as session:
        prompt = save_translation_system_prompt(
            session,
            payload.system_prompt,
            user_id=principal.user_id,
        )
        session.commit()
        return TranslationPromptResponse(system_prompt=prompt)


@router.post(
    "/translation-prompt/reset",
    response_model=TranslationPromptResponse,
)
def restore_default_translation_prompt(request: Request) -> TranslationPromptResponse:
    principal = require_request_permission(request, Permission.SETTINGS_WRITE)
    with request.app.state.session_factory() as session:
        prompt = reset_translation_system_prompt(session, user_id=principal.user_id)
        session.commit()
        return TranslationPromptResponse(system_prompt=prompt)


@router.get("/paper-map-questions", response_model=PaperMapQuestionLibraryResponse)
def get_paper_map_questions(request: Request) -> PaperMapQuestionLibraryResponse:
    principal = require_request_permission(request, Permission.SETTINGS_READ)
    with request.app.state.session_factory() as session:
        return _question_library_response(session, user_id=principal.user_id)


@router.post(
    "/paper-map-questions",
    response_model=PaperMapQuestionLibraryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_paper_map_question(
    payload: PaperMapQuestionCreateRequest,
    request: Request,
) -> PaperMapQuestionLibraryResponse:
    principal = require_request_permission(request, Permission.SETTINGS_WRITE)
    with request.app.state.session_factory() as session:
        try:
            add_user_question(session, payload.text, user_id=principal.user_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        session.commit()
        return _question_library_response(session, user_id=principal.user_id)


@router.delete(
    "/paper-map-questions/{question_key}",
    response_model=PaperMapQuestionLibraryResponse,
)
def remove_paper_map_question(
    question_key: str,
    request: Request,
) -> PaperMapQuestionLibraryResponse:
    principal = require_request_permission(request, Permission.SETTINGS_WRITE)
    with request.app.state.session_factory() as session:
        try:
            deleted = delete_question(
                session,
                question_key,
                user_id=principal.user_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="Question not found.")
        session.commit()
        return _question_library_response(session, user_id=principal.user_id)


@router.put(
    "/paper-map-questions/{question_key}",
    response_model=PaperMapQuestionLibraryResponse,
)
def edit_paper_map_question(
    question_key: str,
    payload: PaperMapQuestionUpdateRequest,
    request: Request,
) -> PaperMapQuestionLibraryResponse:
    principal = require_request_permission(request, Permission.SETTINGS_WRITE)
    with request.app.state.session_factory() as session:
        try:
            updated = update_question(
                session,
                question_key,
                payload.text,
                user_id=principal.user_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if updated is None:
            raise HTTPException(status_code=404, detail="Question not found.")
        session.commit()
        return _question_library_response(session, user_id=principal.user_id)
