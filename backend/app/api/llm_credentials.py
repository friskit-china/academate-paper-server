from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.application.credentials import LlmTaskCredential, select_llm_task_credential


def request_llm_task_credential(
    request: Request,
    *,
    user_id: str,
    required: bool = True,
    allow_unconfigured_environment: bool = False,
) -> LlmTaskCredential | None:
    """Select the authenticated user's LLM credential without trusting browser identifiers."""

    settings = request.app.state.settings
    selected = select_llm_task_credential(
        credential_service=getattr(request.app.state, "credential_service", None),
        user_id=user_id,
        environment_configured=settings.llm_configured,
        environment_provider=settings.llm_provider,
        environment_api_base=settings.llm_api_base,
        environment_model=settings.llm_model,
    )
    if (
        selected is None
        and allow_unconfigured_environment
        and getattr(request.app.state, "credential_service", None) is None
        and settings.llm_provider.strip()
        and settings.llm_api_base.strip()
        and settings.llm_model.strip()
    ):
        # Compatibility for legacy Compose endpoints that historically allowed durable queueing
        # before an environment key was injected. Packaged native runtimes never take this path.
        selected = LlmTaskCredential(
            credential_id=None,
            provider=settings.llm_provider.strip().lower(),
            api_base=settings.llm_api_base.strip(),
            model=settings.llm_model.strip(),
        )
    if selected is None and required:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A usable LLM credential is not configured for this owner.",
        )
    return selected
