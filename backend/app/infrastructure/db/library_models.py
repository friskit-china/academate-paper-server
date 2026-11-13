"""Library ACLs keep canonical workspace LibraryItem IDs stable across collections."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.models import new_uuid, now_utc


class LibraryModel(Base):
    __tablename__ = "libraries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key"),
        CheckConstraint("revision >= 1", name="libraries_positive_revision"),
        Index("ix_libraries_workspace", "workspace_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc)


class LibraryGrantModel(Base):
    __tablename__ = "library_grants"
    library_id: Mapped[str] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    grant_source: Mapped[str] = mapped_column(String(32), nullable=False, default="USER_POLICY")
    granted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    can_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_import: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_remove: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(default=now_utc, onupdate=now_utc)


class LibraryEntryModel(Base):
    __tablename__ = "library_entries"
    __table_args__ = (Index("ix_library_entries_item", "library_item_id"),)
    library_id: Mapped[str] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    library_item_id: Mapped[str] = mapped_column(
        ForeignKey("library_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(default=now_utc)


class LibraryImportKeyModel(Base):
    __tablename__ = "library_import_keys"
    library_id: Mapped[str] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    library_item_id: Mapped[str] = mapped_column(
        ForeignKey("library_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(default=now_utc)
