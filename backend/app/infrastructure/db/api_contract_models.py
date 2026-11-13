"""Bounded command receipt metadata; raw requests and credential responses never belong here."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base


class ApiIdempotencyModel(Base):
    __tablename__ = "api_idempotency"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING','COMPLETE','NON_REPLAYABLE')", name="api_receipt_state"
        ),
        CheckConstraint(
            "(state = 'COMPLETE' AND response_json IS NOT NULL AND status_code >= 200 "
            "AND status_code < 300) OR (state <> 'COMPLETE' AND response_json IS NULL "
            "AND status_code IS NULL)",
            name="api_receipt_complete_response",
        ),
        Index("ix_api_receipts_expiry", "expires_at"),
        Index("ix_api_receipts_subject", "user_id", "workspace_id", "created_at"),
    )
    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_json: Mapped[str | None] = mapped_column(Text)
    response_headers_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
