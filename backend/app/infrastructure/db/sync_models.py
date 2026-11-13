"""Persistent v2 sync, reading state and scoped integration credentials."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.models import new_uuid, now_utc


class ResourceChangeModel(Base):
    __tablename__ = "resource_changes"
    __table_args__ = (
        Index("ix_resource_changes_scope", "workspace_id", "user_id", "id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(36))
    user_id: Mapped[str | None] = mapped_column(String(36))
    paper_id: Mapped[str | None] = mapped_column(String(36))
    revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class SyncStateModel(Base):
    __tablename__ = "sync_state"
    singleton_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    retained_after: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ReadingStateModel(Base):
    __tablename__ = "user_reading_states"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="reading_state_positive_revision"),
        CheckConstraint("progress >= 0 AND progress <= 1", name="reading_state_progress_range"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    library_item_id: Mapped[str] = mapped_column(
        ForeignKey("library_items.id", ondelete="CASCADE"), primary_key=True
    )
    block_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="SET NULL")
    )
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class JobCancellationModel(Base):
    __tablename__ = "job_cancellations"
    task_id: Mapped[str] = mapped_column(
        ForeignKey("processing_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationTokenModel(Base):
    __tablename__ = "integration_tokens"
    __table_args__ = (Index("ix_integration_tokens_user", "user_id", "workspace_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    installation_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    digest: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
