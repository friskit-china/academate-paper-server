from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    database: str | None = None
    storage: str | None = None


def get_db_session(request: Request) -> Session:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


@router.get("/live", response_model=HealthResponse)
def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
def readiness(
    request: Request, session: Annotated[Session, Depends(get_db_session)]
) -> HealthResponse:
    try:
        session.execute(text("SELECT 1"))
        request.app.state.storage.probe_writable()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "NOT_READY", "message": "Database or storage is unavailable."},
        ) from exc
    return HealthResponse(status="ok", database="ok", storage="ok")
