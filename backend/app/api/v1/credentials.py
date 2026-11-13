from __future__ import annotations

from datetime import datetime
from typing import Never

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, SecretStr

from app.api.dependencies import require_request_permission, resolve_request_principal
from app.application.credentials import (
    CredentialApplicationError,
    CredentialMetadata,
    CredentialNotFoundError,
    CredentialService,
    CredentialStoreUnavailableError,
    CredentialWrite,
)
from app.domain.auth import Permission
from app.domain.credentials import SensitiveText

router = APIRouter(
    prefix="/credentials",
    tags=["credentials"],
    dependencies=[Depends(resolve_request_principal)],
)


class CredentialWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Length and format checks happen after SecretStr construction so validation responses cannot
    # echo a rejected plaintext key in Pydantic's ``input`` field.
    provider: str
    api_key: SecretStr
    api_base: str | None = None
    model: str | None = None


class CredentialMetadataResponse(BaseModel):
    id: str
    provider: str
    api_base: str | None
    model: str | None
    configured: bool
    masked_suffix: str | None
    requires_reentry: bool
    updated_at: datetime


class CredentialListResponse(BaseModel):
    credentials: list[CredentialMetadataResponse]


class DefaultCredentialResponse(BaseModel):
    storage_mode: str
    credential: CredentialMetadataResponse | None


def _credential_service(request: Request) -> CredentialService:
    service = getattr(request.app.state, "credential_service", None)
    if service is None:
        # No fallback to environment API keys: packaged adapters must be wired explicitly.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Protected credential storage is not available in this runtime.",
        )
    return service


def _write(payload: CredentialWriteRequest) -> CredentialWrite:
    return CredentialWrite(
        provider=payload.provider,
        api_key=SensitiveText(payload.api_key.get_secret_value()),
        api_base=payload.api_base,
        model=payload.model,
    )


def _response(metadata: CredentialMetadata) -> CredentialMetadataResponse:
    return CredentialMetadataResponse(
        id=metadata.id,
        provider=metadata.provider,
        api_base=metadata.api_base,
        model=metadata.model,
        configured=metadata.configured,
        masked_suffix=metadata.masked_suffix,
        requires_reentry=metadata.requires_reentry,
        updated_at=metadata.updated_at,
    )


def _raise_write_error(error: CredentialApplicationError) -> Never:
    if isinstance(error, CredentialNotFoundError):
        raise HTTPException(status_code=404, detail="Credential not found.") from error
    if isinstance(error, CredentialStoreUnavailableError) or error.requires_reentry:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Protected credential storage requires recovery before saving this value.",
        ) from error
    raise HTTPException(status_code=422, detail="The credential could not be saved.") from error


@router.get("", response_model=CredentialListResponse)
def list_credentials(request: Request) -> CredentialListResponse:
    principal = require_request_permission(request, Permission.CREDENTIAL_READ)
    service = _credential_service(request)
    return CredentialListResponse(
        credentials=[_response(item) for item in service.list_for_user(principal.user_id)]
    )


@router.get("/default", response_model=DefaultCredentialResponse)
def get_default_credential(request: Request) -> DefaultCredentialResponse:
    principal = require_request_permission(request, Permission.CREDENTIAL_READ)
    service = getattr(request.app.state, "credential_service", None)
    if service is None:
        return DefaultCredentialResponse(storage_mode="environment", credential=None)
    metadata = service.default_for_user(principal.user_id)
    return DefaultCredentialResponse(
        storage_mode="protected",
        credential=_response(metadata) if metadata is not None else None,
    )


@router.put("/default", response_model=CredentialMetadataResponse)
def replace_default_credential(
    payload: CredentialWriteRequest,
    request: Request,
) -> CredentialMetadataResponse:
    principal = require_request_permission(request, Permission.CREDENTIAL_WRITE)
    service = _credential_service(request)
    try:
        return _response(service.upsert_default_for_user(principal.user_id, _write(payload)))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except CredentialApplicationError as error:
        _raise_write_error(error)


@router.get("/{credential_id}", response_model=CredentialMetadataResponse)
def get_credential(credential_id: str, request: Request) -> CredentialMetadataResponse:
    principal = require_request_permission(request, Permission.CREDENTIAL_READ)
    service = _credential_service(request)
    try:
        return _response(service.get_for_user(credential_id, principal.user_id))
    except CredentialNotFoundError as error:
        raise HTTPException(status_code=404, detail="Credential not found.") from error


@router.post("", response_model=CredentialMetadataResponse, status_code=status.HTTP_201_CREATED)
def create_credential(
    payload: CredentialWriteRequest,
    request: Request,
) -> CredentialMetadataResponse:
    principal = require_request_permission(request, Permission.CREDENTIAL_WRITE)
    service = _credential_service(request)
    try:
        return _response(service.create_for_user(principal.user_id, _write(payload)))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except CredentialApplicationError as error:
        _raise_write_error(error)


@router.put("/{credential_id}", response_model=CredentialMetadataResponse)
def replace_credential(
    credential_id: str,
    payload: CredentialWriteRequest,
    request: Request,
) -> CredentialMetadataResponse:
    principal = require_request_permission(request, Permission.CREDENTIAL_WRITE)
    service = _credential_service(request)
    try:
        return _response(
            service.replace_for_user(
                credential_id,
                principal.user_id,
                _write(payload),
            )
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except CredentialApplicationError as error:
        _raise_write_error(error)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(credential_id: str, request: Request) -> Response:
    principal = require_request_permission(request, Permission.CREDENTIAL_WRITE)
    service = _credential_service(request)
    try:
        service.delete_for_user(credential_id, principal.user_id)
    except CredentialNotFoundError as error:
        raise HTTPException(status_code=404, detail="Credential not found.") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
