from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.domain.enums import (
    AnnotationResolutionStatus,
    AnnotationTarget,
    AnnotationType,
    PaperMapQuestionStatus,
    PaperMapStatus,
    ProcessingHealth,
    ProcessingStage,
    ReadingStatus,
    TaskStatus,
    TranslationStatus,
)
from app.domain.tasks import (
    redact_task_error_message,
    require_secret_free_task_data,
    sanitize_diagnostic_identifier,
)
from app.infrastructure.db.base import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(UTC)


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_status", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    auth_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    group_memberships: Mapped[list[GroupMembershipModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    workspace_memberships: Mapped[list[WorkspaceMembershipModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    settings: Mapped[list[UserSettingModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    llm_credentials: Mapped[list[UserLlmCredentialModel]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    onboarding_state: Mapped[UserOnboardingStateModel | None] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    owned_installation: Mapped[InstallationStateModel | None] = relationship(
        back_populates="owner", uselist=False
    )
    created_annotations: Mapped[list[AnnotationModel]] = relationship(
        back_populates="creator",
        foreign_keys="AnnotationModel.creator_user_id",
        passive_deletes=True,
    )
    created_annotation_comments: Mapped[list[AnnotationCommentModel]] = relationship(
        back_populates="creator",
        foreign_keys="AnnotationCommentModel.creator_user_id",
        passive_deletes="all",
    )
    created_bookmarks: Mapped[list[BlockBookmarkModel]] = relationship(
        back_populates="creator",
        foreign_keys="BlockBookmarkModel.creator_user_id",
        passive_deletes="all",
    )
    requested_translations: Mapped[list[TranslationModel]] = relationship(
        back_populates="requester",
        foreign_keys="TranslationModel.requested_by_user_id",
        passive_deletes="all",
    )
    requested_paper_maps: Mapped[list[PaperMapModel]] = relationship(
        back_populates="requester",
        foreign_keys="PaperMapModel.requested_by_user_id",
        passive_deletes="all",
    )
    requested_tasks: Mapped[list[ProcessingTaskModel]] = relationship(
        back_populates="requester",
        foreign_keys="ProcessingTaskModel.requested_by_user_id",
        passive_deletes="all",
    )
    requested_render_overlays: Mapped[list[WorkspaceBlockRenderOverlayModel]] = relationship(
        back_populates="requester",
        foreign_keys="WorkspaceBlockRenderOverlayModel.requested_by_user_id",
        passive_deletes="all",
    )


class GroupModel(Base):
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    memberships: Mapped[list[GroupMembershipModel]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    permission_mappings: Mapped[list[GroupPermissionModel]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class WorkspaceModel(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    memberships: Mapped[list[WorkspaceMembershipModel]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    library_items: Mapped[list[LibraryItemModel]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    translations: Mapped[list[TranslationModel]] = relationship(
        back_populates="workspace", passive_deletes=True
    )
    annotations: Mapped[list[AnnotationModel]] = relationship(
        back_populates="workspace", passive_deletes=True
    )
    annotation_comments: Mapped[list[AnnotationCommentModel]] = relationship(
        back_populates="workspace", passive_deletes=True
    )
    block_bookmarks: Mapped[list[BlockBookmarkModel]] = relationship(
        back_populates="workspace", passive_deletes=True
    )
    paper_maps: Mapped[list[PaperMapModel]] = relationship(
        back_populates="workspace", passive_deletes=True
    )
    tasks: Mapped[list[ProcessingTaskModel]] = relationship(
        back_populates="workspace", passive_deletes=True
    )
    block_render_overlays: Mapped[list[WorkspaceBlockRenderOverlayModel]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    invitations: Mapped[list[WorkspaceInvitationModel]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class PermissionModel(Base):
    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    group_mappings: Mapped[list[GroupPermissionModel]] = relationship(
        back_populates="permission", cascade="all, delete-orphan"
    )
    workspace_role_mappings: Mapped[list[WorkspaceRolePermissionModel]] = relationship(
        back_populates="permission", cascade="all, delete-orphan"
    )


class GroupMembershipModel(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (Index("ix_group_memberships_group", "group_id"),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    user: Mapped[UserModel] = relationship(back_populates="group_memberships")
    group: Mapped[GroupModel] = relationship(back_populates="memberships")


class WorkspaceMembershipModel(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        Index("ix_workspace_memberships_user", "user_id", "workspace_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="memberships")
    user: Mapped[UserModel] = relationship(back_populates="workspace_memberships")


class GroupPermissionModel(Base):
    __tablename__ = "group_permissions"
    __table_args__ = (
        Index("ix_group_permissions_permission", "permission_key"),
    )

    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )

    group: Mapped[GroupModel] = relationship(back_populates="permission_mappings")
    permission: Mapped[PermissionModel] = relationship(back_populates="group_mappings")


class WorkspaceRolePermissionModel(Base):
    __tablename__ = "workspace_role_permissions"
    __table_args__ = (
        Index("ix_workspace_role_permissions_permission", "permission_key"),
    )

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )

    permission: Mapped[PermissionModel] = relationship(
        back_populates="workspace_role_mappings"
    )


class InstallationStateModel(Base):
    __tablename__ = "installation_state"
    __table_args__ = (
        CheckConstraint(
            "singleton_key = 'installation'",
            name="installation_state_singleton_key",
        ),
        CheckConstraint(
            "state_version >= 0",
            name="installation_state_nonnegative_version",
        ),
        CheckConstraint(
            "installation_mode IS NULL OR "
            "installation_mode IN ('SINGLE_USER', 'MULTI_USER')",
            name="installation_state_valid_installation_mode",
        ),
    )

    singleton_key: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="installation"
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    edition: Mapped[str] = mapped_column(String(16), nullable=False)
    installation_mode: Mapped[str | None] = mapped_column(String(32))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    owner: Mapped[UserModel | None] = relationship(back_populates="owned_installation")


class BootstrapTokenModel(Base):
    __tablename__ = "bootstrap_tokens"
    __table_args__ = (
        CheckConstraint(
            "singleton_key = 'owner-bootstrap'",
            name="bootstrap_tokens_singleton_key",
        ),
        CheckConstraint(
            "(consumed_at IS NULL AND request_digest IS NULL "
            " AND owner_user_id IS NULL AND workspace_id IS NULL) OR "
            "(consumed_at IS NOT NULL AND request_digest IS NOT NULL "
            " AND owner_user_id IS NOT NULL AND workspace_id IS NOT NULL)",
            name="bootstrap_tokens_complete_consumption",
        ),
        UniqueConstraint("token_digest", name="uq_bootstrap_tokens_digest"),
        Index("ix_bootstrap_tokens_expiry", "expires_at"),
    )

    singleton_key: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="owner-bootstrap"
    )
    token_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_digest: Mapped[str | None] = mapped_column(String(128))
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class LocalLaunchTokenModel(Base):
    __tablename__ = "local_launch_tokens"
    __table_args__ = (
        CheckConstraint(
            "singleton_key = 'browser-launch'",
            name="local_launch_tokens_singleton_key",
        ),
        UniqueConstraint("token_digest", name="uq_local_launch_tokens_digest"),
        Index("ix_local_launch_tokens_expiry", "expires_at"),
    )

    singleton_key: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="browser-launch"
    )
    token_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class LocalSessionModel(Base):
    __tablename__ = "local_sessions"
    __table_args__ = (
        CheckConstraint(
            "installation_state_version >= 0",
            name="local_sessions_nonnegative_installation_version",
        ),
        UniqueConstraint("token_digest", name="uq_local_sessions_digest"),
        Index("ix_local_sessions_expiry", "expires_at"),
        Index("ix_local_sessions_active", "revoked_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    token_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    installation_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class WebSessionModel(Base):
    __tablename__ = "web_sessions"
    __table_args__ = (
        CheckConstraint(
            "installation_state_version >= 0",
            name="web_sessions_nonnegative_installation_version",
        ),
        UniqueConstraint("token_digest", name="uq_web_sessions_digest"),
        ForeignKeyConstraint(
            ["active_workspace_id", "user_id"],
            [
                "workspace_memberships.workspace_id",
                "workspace_memberships.user_id",
            ],
            ondelete="CASCADE",
        ),
        Index("ix_web_sessions_expiry", "expires_at"),
        Index("ix_web_sessions_active", "revoked_at", "expires_at"),
        Index(
            "ix_web_sessions_user_workspace",
            "user_id",
            "active_workspace_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    token_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    active_workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    installation_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class WorkspaceInvitationModel(Base):
    __tablename__ = "workspace_invitations"
    __table_args__ = (
        CheckConstraint(
            "issuer_channel IN ('WEB_ADMIN', 'OPERATOR_CLI')",
            name="workspace_invitations_valid_issuer_channel",
        ),
        CheckConstraint(
            "(issuer_channel = 'WEB_ADMIN' AND created_by_user_id IS NOT NULL) OR "
            "(issuer_channel = 'OPERATOR_CLI' AND created_by_user_id IS NULL)",
            name="workspace_invitations_issuer_identity",
        ),
        CheckConstraint(
            "installation_state_version >= 0",
            name="workspace_invitations_nonnegative_installation_version",
        ),
        CheckConstraint(
            "length(trim(masked_hint)) BETWEEN 1 AND 32",
            name="workspace_invitations_nonempty_masked_hint",
        ),
        CheckConstraint(
            "(redeemed_at IS NULL AND redeemed_by_user_id IS NULL) OR "
            "(redeemed_at IS NOT NULL AND redeemed_by_user_id IS NOT NULL)",
            name="workspace_invitations_complete_redemption",
        ),
        CheckConstraint(
            "redeemed_at IS NULL OR revoked_at IS NULL",
            name="workspace_invitations_not_redeemed_and_revoked",
        ),
        UniqueConstraint(
            "token_digest",
            name="uq_workspace_invitations_token_digest",
        ),
        Index(
            "ix_workspace_invitations_workspace_created",
            "workspace_id",
            "created_at",
        ),
        Index(
            "ix_workspace_invitations_workspace_lifecycle",
            "workspace_id",
            "redeemed_at",
            "revoked_at",
            "expires_at",
        ),
        Index("ix_workspace_invitations_created_by", "created_by_user_id"),
        Index("ix_workspace_invitations_redeemed_by", "redeemed_by_user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    token_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    masked_hint: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    issuer_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    installation_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeemed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="invitations")


class SiteSettingModel(Base):
    __tablename__ = "site_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class UserSettingModel(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    user: Mapped[UserModel] = relationship(back_populates="settings")


class UserOnboardingStateModel(Base):
    __tablename__ = "user_onboarding_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'COMPLETED')",
            name="user_onboarding_states_valid_status",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND completed_at IS NULL) OR "
            "(status = 'COMPLETED' AND completed_at IS NOT NULL)",
            name="user_onboarding_states_completion_metadata",
        ),
        Index("ix_user_onboarding_states_status", "status"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    user: Mapped[UserModel] = relationship(back_populates="onboarding_state")


class UserLlmCredentialModel(Base):
    __tablename__ = "user_llm_credentials"
    __table_args__ = (
        Index("ix_user_llm_credentials_user_provider", "user_id", "provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    api_base: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    user: Mapped[UserModel] = relationship(back_populates="llm_credentials")
    tasks: Mapped[list[ProcessingTaskModel]] = relationship(
        back_populates="credential", passive_deletes=True
    )


class PaperModel(Base):
    __tablename__ = "papers"
    __table_args__ = (
        UniqueConstraint("arxiv_id", "arxiv_version"),
        Index("ix_papers_favorite", "is_favorite"),
        Index("ix_papers_pinned", "pinned_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    arxiv_id: Mapped[str] = mapped_column(String(32), nullable=False)
    arxiv_version: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_stage: Mapped[str] = mapped_column(String(32), default=ProcessingStage.REGISTERED)
    processing_health: Mapped[str] = mapped_column(String(16), default=ProcessingHealth.OK)
    reading_status: Mapped[str] = mapped_column(String(16), default=ReadingStatus.UNREAD)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_read_block_id: Mapped[str | None] = mapped_column(String(36))
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    authors: Mapped[list[PaperAuthorModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="PaperAuthorModel.position"
    )
    sources: Mapped[list[PaperSourceModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    blocks: Mapped[list[ContentBlockModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[ProcessingTaskModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    annotations: Mapped[list[AnnotationModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    maps: Mapped[list[PaperMapModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    bibliography: Mapped[PaperBibliographyModel | None] = relationship(
        back_populates="paper", cascade="all, delete-orphan", uselist=False
    )
    citation_occurrences: Mapped[list[CitationOccurrenceModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    block_bookmarks: Mapped[list[BlockBookmarkModel]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    library_items: Mapped[list[LibraryItemModel]] = relationship(
        back_populates="paper", passive_deletes=True
    )


class PaperAuthorModel(Base):
    __tablename__ = "paper_authors"
    __table_args__ = (UniqueConstraint("paper_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    affiliation_text: Mapped[str | None] = mapped_column(Text)
    orcid: Mapped[str | None] = mapped_column(String(64))

    paper: Mapped[PaperModel] = relationship(back_populates="authors")


class PaperSourceModel(Base):
    __tablename__ = "paper_sources"
    __table_args__ = (
        UniqueConstraint("paper_id", "source_type"),
        Index("ix_paper_sources_paper", "paper_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    paper: Mapped[PaperModel] = relationship(back_populates="sources")


class ContentBlockModel(Base):
    __tablename__ = "content_blocks"
    __table_args__ = (
        Index("ix_content_blocks_paper_ordinal", "paper_id", "ordinal_path"),
        Index("ix_content_blocks_parent", "parent_id"),
        Index("ix_content_blocks_type", "block_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="SET NULL")
    )
    block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal_path: Mapped[str] = mapped_column(String(255), nullable=False)
    heading_level: Mapped[int | None] = mapped_column(Integer)
    source_text: Mapped[str | None] = mapped_column(Text)
    source_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    render_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_locator_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    paper: Mapped[PaperModel] = relationship(back_populates="blocks")
    translations: Mapped[list[TranslationModel]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )
    annotations: Mapped[list[AnnotationModel]] = relationship(back_populates="block")
    citation_occurrences: Mapped[list[CitationOccurrenceModel]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )
    bookmarks: Mapped[list[BlockBookmarkModel]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )
    last_read_library_items: Mapped[list[LibraryItemModel]] = relationship(
        back_populates="last_read_block", passive_deletes=True
    )
    workspace_render_overlays: Mapped[list[WorkspaceBlockRenderOverlayModel]] = relationship(
        back_populates="block", cascade="all, delete-orphan"
    )


class WorkspaceBlockRenderOverlayModel(Base):
    """One workspace's derived view of a canonical block render payload.

    The base hash makes stale overlays inert after a canonical parser revision.  The payload is a
    complete render view (rather than a JSON merge patch), which keeps reads deterministic and
    avoids accidentally inheriting a repaired field from another workspace.
    """

    __tablename__ = "workspace_block_render_overlays"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "block_id",
            "base_content_hash",
            name="uq_workspace_block_render_overlays_base",
        ),
        CheckConstraint(
            "length(base_content_hash) = 64",
            name="workspace_block_render_overlays_hash_length",
        ),
        Index(
            "ix_workspace_block_render_overlays_lookup",
            "workspace_id",
            "block_id",
            "base_content_hash",
        ),
        Index(
            "ix_workspace_block_render_overlays_requester",
            "requested_by_user_id",
            "workspace_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    block_id: Mapped[str] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    base_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    render_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="block_render_overlays")
    block: Mapped[ContentBlockModel] = relationship(back_populates="workspace_render_overlays")
    requester: Mapped[UserModel] = relationship(
        back_populates="requested_render_overlays",
        foreign_keys=[requested_by_user_id],
    )


class BlockBookmarkModel(Base):
    __tablename__ = "block_bookmarks"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "creator_user_id",
            "paper_id",
            "block_id",
            name="uq_block_bookmarks_workspace_creator_paper_block",
        ),
        Index(
            "ix_block_bookmarks_workspace_paper_created",
            "workspace_id",
            "paper_id",
            "created_at",
        ),
        Index("ix_block_bookmarks_creator", "creator_user_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    creator_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    block_id: Mapped[str] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="block_bookmarks")
    creator: Mapped[UserModel] = relationship(
        back_populates="created_bookmarks", foreign_keys=[creator_user_id]
    )
    paper: Mapped[PaperModel] = relationship(back_populates="block_bookmarks")
    block: Mapped[ContentBlockModel] = relationship(back_populates="bookmarks")


class LibraryItemModel(Base):
    __tablename__ = "library_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "paper_id"),
        Index(
            "ix_library_items_workspace_status",
            "workspace_id",
            "reading_status",
            "created_at",
        ),
        Index("ix_library_items_paper", "paper_id"),
        Index(
            "ix_library_items_workspace_favorite",
            "workspace_id",
            "is_favorite",
        ),
        Index("ix_library_items_workspace_pinned", "workspace_id", "pinned_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="RESTRICT"), nullable=False
    )
    reading_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReadingStatus.UNREAD
    )
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_read_block_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="SET NULL")
    )
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="library_items")
    paper: Mapped[PaperModel] = relationship(back_populates="library_items")
    last_read_block: Mapped[ContentBlockModel | None] = relationship(
        back_populates="last_read_library_items"
    )


class PaperBibliographyModel(Base):
    __tablename__ = "paper_bibliographies"
    __table_args__ = (UniqueConstraint("paper_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="EMPTY")
    requested_style: Mapped[str | None] = mapped_column(Text)
    resolved_style: Mapped[str | None] = mapped_column(Text)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_files_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    paper: Mapped[PaperModel] = relationship(back_populates="bibliography")
    entries: Mapped[list[BibliographyEntryModel]] = relationship(
        back_populates="bibliography",
        cascade="all, delete-orphan",
        order_by="BibliographyEntryModel.ordinal",
    )


class BibliographyEntryModel(Base):
    __tablename__ = "bibliography_entries"
    __table_args__ = (
        UniqueConstraint("bibliography_id", "citation_key_normalized"),
        Index("ix_bibliography_entries_order", "bibliography_id", "ordinal"),
        Index("ix_bibliography_entries_doi", "doi"),
        Index("ix_bibliography_entries_eprint", "archive_prefix", "eprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    bibliography_id: Mapped[str] = mapped_column(
        ForeignKey("paper_bibliographies.id", ondelete="CASCADE"), nullable=False
    )
    citation_key: Mapped[str] = mapped_column(Text, nullable=False)
    citation_key_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    raw_bibtex: Mapped[str | None] = mapped_column(Text)
    fields_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    title: Mapped[str | None] = mapped_column(Text)
    container_title: Mapped[str | None] = mapped_column(Text)
    publication_year: Mapped[str | None] = mapped_column(String(32))
    doi: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    eprint: Mapped[str | None] = mapped_column(Text)
    archive_prefix: Mapped[str | None] = mapped_column(String(64))
    isbn: Mapped[str | None] = mapped_column(Text)
    issn: Mapped[str | None] = mapped_column(Text)
    ordinal: Mapped[int | None] = mapped_column(Integer)
    display_label: Mapped[str | None] = mapped_column(Text)
    citation_author: Mapped[str | None] = mapped_column(Text)
    citation_year: Mapped[str | None] = mapped_column(String(32))
    formatted_latex: Mapped[str | None] = mapped_column(Text)
    formatted_text: Mapped[str | None] = mapped_column(Text)
    render_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    bibliography: Mapped[PaperBibliographyModel] = relationship(back_populates="entries")
    persons: Mapped[list[BibliographyPersonModel]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="BibliographyPersonModel.role, BibliographyPersonModel.position",
    )
    citation_occurrences: Mapped[list[CitationOccurrenceModel]] = relationship(
        back_populates="entry"
    )


class BibliographyPersonModel(Base):
    __tablename__ = "bibliography_persons"
    __table_args__ = (
        UniqueConstraint("entry_id", "role", "position"),
        Index("ix_bibliography_persons_name", "display_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    entry_id: Mapped[str] = mapped_column(
        ForeignKey("bibliography_entries.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    given_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    family_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    prefix_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    suffix_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    initials_json: Mapped[list[str]] = mapped_column(JSON, default=list)

    entry: Mapped[BibliographyEntryModel] = relationship(back_populates="persons")


class CitationOccurrenceModel(Base):
    __tablename__ = "citation_occurrences"
    __table_args__ = (
        UniqueConstraint("block_id", "cluster_path", "item_index"),
        Index("ix_citation_occurrences_paper", "paper_id", "block_id"),
        Index("ix_citation_occurrences_entry", "bibliography_entry_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    block_id: Mapped[str] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="CASCADE"), nullable=False
    )
    bibliography_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("bibliography_entries.id", ondelete="SET NULL")
    )
    cluster_path: Mapped[str] = mapped_column(String(512), nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    citation_key: Mapped[str] = mapped_column(Text, nullable=False)
    citation_key_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    command: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str | None] = mapped_column(Text)
    suffix: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    paper: Mapped[PaperModel] = relationship(back_populates="citation_occurrences")
    block: Mapped[ContentBlockModel] = relationship(back_populates="citation_occurrences")
    entry: Mapped[BibliographyEntryModel | None] = relationship(
        back_populates="citation_occurrences"
    )


class TranslationModel(Base):
    __tablename__ = "translations"
    __table_args__ = (
        Index(
            "uq_translations_workspace_shared_input",
            "workspace_id",
            "block_id",
            "language",
            "input_hash",
            unique=True,
            sqlite_where=text("prompt_kind = 'global'"),
            postgresql_where=text("prompt_kind = 'global'"),
        ),
        Index(
            "uq_translations_workspace_private_input",
            "workspace_id",
            "requested_by_user_id",
            "block_id",
            "language",
            "input_hash",
            unique=True,
            sqlite_where=text("prompt_kind != 'global'"),
            postgresql_where=text("prompt_kind != 'global'"),
        ),
        Index(
            "ix_translations_workspace_block_language_status",
            "workspace_id",
            "block_id",
            "language",
            "status",
        ),
        Index(
            "ix_translations_workspace_cache",
            "workspace_id",
            "input_hash",
            "status",
        ),
        Index("ix_translations_requester", "requested_by_user_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    block_id: Mapped[str] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TranslationStatus.PENDING
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    translated_text: Mapped[str | None] = mapped_column(Text)
    translated_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_source_translation_id: Mapped[str | None] = mapped_column(
        ForeignKey("translations.id", ondelete="SET NULL")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="translations")
    requester: Mapped[UserModel] = relationship(
        back_populates="requested_translations",
        foreign_keys=[requested_by_user_id],
    )
    block: Mapped[ContentBlockModel] = relationship(back_populates="translations")


class AnnotationModel(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "parent_annotation_id",
            "qa_generation",
            name="uq_annotations_workspace_parent_generation",
        ),
        CheckConstraint(
            "creator_type <> 'USER' OR creator_user_id IS NOT NULL",
            name="annotations_user_creator",
        ),
        Index(
            "ix_annotations_workspace_paper_active",
            "workspace_id",
            "paper_id",
            "deleted_at",
            "created_at",
        ),
        Index("ix_annotations_workspace_block", "workspace_id", "block_id"),
        Index("ix_annotations_workspace_parent", "workspace_id", "parent_annotation_id"),
        Index("ix_annotations_workspace_anchor", "workspace_id", "anchor_annotation_id"),
        Index("ix_annotations_creator", "creator_user_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    creator_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    block_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_blocks.id", ondelete="SET NULL")
    )
    parent_annotation_id: Mapped[str | None] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE")
    )
    anchor_annotation_id: Mapped[str | None] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE")
    )
    annotation_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default=AnnotationType.HIGHLIGHT
    )
    target: Mapped[str] = mapped_column(String(16), nullable=False, default=AnnotationTarget.SOURCE)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_highlighted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    creator_type: Mapped[str] = mapped_column(String(8), nullable=False, default="USER")
    selector_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    target_translation_id: Mapped[str | None] = mapped_column(
        ForeignKey("translations.id", ondelete="SET NULL")
    )
    target_translation_input_hash: Mapped[str | None] = mapped_column(String(64))
    resolution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AnnotationResolutionStatus.NOT_APPLICABLE
    )
    resolution_reason: Mapped[str | None] = mapped_column(String(96))
    resolved_start_offset: Mapped[int | None] = mapped_column(Integer)
    resolved_end_offset: Mapped[int | None] = mapped_column(Integer)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    qa_status: Mapped[str | None] = mapped_column(String(16))
    qa_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qa_error_type: Mapped[str | None] = mapped_column(String(64))
    qa_error_code: Mapped[str | None] = mapped_column(String(128))
    qa_error_message: Mapped[str | None] = mapped_column(Text)
    qa_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="annotations")
    creator: Mapped[UserModel | None] = relationship(
        back_populates="created_annotations",
        foreign_keys=[creator_user_id],
    )
    paper: Mapped[PaperModel] = relationship(back_populates="annotations")
    block: Mapped[ContentBlockModel | None] = relationship(back_populates="annotations")
    parent: Mapped[AnnotationModel | None] = relationship(
        remote_side="AnnotationModel.id",
        back_populates="children",
        foreign_keys=[parent_annotation_id],
    )
    children: Mapped[list[AnnotationModel]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys=[parent_annotation_id],
    )
    anchor: Mapped[AnnotationModel | None] = relationship(
        remote_side="AnnotationModel.id",
        back_populates="questions",
        foreign_keys=[anchor_annotation_id],
    )
    questions: Mapped[list[AnnotationModel]] = relationship(
        back_populates="anchor",
        cascade="all, delete-orphan",
        foreign_keys=[anchor_annotation_id],
    )
    comments: Mapped[list[AnnotationCommentModel]] = relationship(
        back_populates="annotation",
        cascade="all, delete-orphan",
        order_by="AnnotationCommentModel.created_at",
    )


class AnnotationCommentModel(Base):
    __tablename__ = "annotation_comments"
    __table_args__ = (
        Index(
            "ix_annotation_comments_workspace_active",
            "workspace_id",
            "annotation_id",
            "deleted_at",
            "created_at",
        ),
        Index("ix_annotation_comments_creator", "creator_user_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    creator_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    annotation_id: Mapped[str] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="annotation_comments")
    creator: Mapped[UserModel] = relationship(
        back_populates="created_annotation_comments",
        foreign_keys=[creator_user_id],
    )
    annotation: Mapped[AnnotationModel] = relationship(back_populates="comments")


class PaperMapModel(Base):
    __tablename__ = "paper_maps"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "paper_id",
            "generation",
            name="uq_paper_maps_workspace_paper_generation",
        ),
        Index(
            "ix_paper_maps_workspace_current",
            "workspace_id",
            "paper_id",
            "generation",
            "status",
        ),
        Index("ix_paper_maps_requester", "requested_by_user_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PaperMapStatus.QUEUED)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    question_library_version: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_type: Mapped[str | None] = mapped_column(String(32))
    secondary_types_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    type_confidence: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped[WorkspaceModel] = relationship(back_populates="paper_maps")
    requester: Mapped[UserModel] = relationship(
        back_populates="requested_paper_maps",
        foreign_keys=[requested_by_user_id],
    )
    paper: Mapped[PaperModel] = relationship(back_populates="maps")
    questions: Mapped[list[PaperMapQuestionModel]] = relationship(
        back_populates="paper_map",
        cascade="all, delete-orphan",
        order_by="PaperMapQuestionModel.ordinal",
    )
    nodes: Mapped[list[PaperMapNodeModel]] = relationship(
        back_populates="paper_map", cascade="all, delete-orphan"
    )
    edges: Mapped[list[PaperMapEdgeModel]] = relationship(
        back_populates="paper_map", cascade="all, delete-orphan"
    )


class PaperMapQuestionModel(Base):
    __tablename__ = "paper_map_questions"
    __table_args__ = (
        UniqueConstraint("map_id", "question_key"),
        Index("ix_paper_map_questions_map_status", "map_id", "status", "ordinal"),
        Index("ix_paper_map_questions_anchor", "anchor_node_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    map_id: Mapped[str] = mapped_column(
        ForeignKey("paper_maps.id", ondelete="CASCADE"), nullable=False
    )
    anchor_node_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_map_nodes.id", ondelete="SET NULL")
    )
    is_initial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    question_key: Mapped[str] = mapped_column(String(64), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_block_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaperMapQuestionStatus.NOT_REQUESTED
    )
    answer_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answer_status: Mapped[str | None] = mapped_column(String(32))
    answer_text: Mapped[str | None] = mapped_column(Text)
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    answer_prompt_version: Mapped[str | None] = mapped_column(String(64))
    answer_prompt_hash: Mapped[str | None] = mapped_column(String(64))
    answer_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paper_map: Mapped[PaperMapModel] = relationship(back_populates="questions")
    nodes: Mapped[list[PaperMapNodeModel]] = relationship(
        back_populates="question",
        foreign_keys="PaperMapNodeModel.question_id",
    )


class PaperMapNodeModel(Base):
    __tablename__ = "paper_map_nodes"
    __table_args__ = (
        UniqueConstraint("map_id", "node_key"),
        Index("ix_paper_map_nodes_map_type", "map_id", "node_type"),
        Index("ix_paper_map_nodes_question", "question_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    map_id: Mapped[str] = mapped_column(
        ForeignKey("paper_maps.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_map_questions.id", ondelete="CASCADE")
    )
    node_key: Mapped[str] = mapped_column(String(128), nullable=False)
    node_type: Mapped[str] = mapped_column(String(24), nullable=False)
    concept_type: Mapped[str | None] = mapped_column(String(24))
    label: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str | None] = mapped_column(String(32))
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    paper_map: Mapped[PaperMapModel] = relationship(back_populates="nodes")
    question: Mapped[PaperMapQuestionModel | None] = relationship(
        back_populates="nodes",
        foreign_keys=[question_id],
    )


class PaperMapEdgeModel(Base):
    __tablename__ = "paper_map_edges"
    __table_args__ = (
        UniqueConstraint("map_id", "source_node_id", "target_node_id", "edge_type"),
        Index("ix_paper_map_edges_map", "map_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    map_id: Mapped[str] = mapped_column(
        ForeignKey("paper_maps.id", ondelete="CASCADE"), nullable=False
    )
    source_node_id: Mapped[str] = mapped_column(
        ForeignKey("paper_map_nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_node_id: Mapped[str] = mapped_column(
        ForeignKey("paper_map_nodes.id", ondelete="CASCADE"), nullable=False
    )
    edge_type: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    is_tree: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    paper_map: Mapped[PaperMapModel] = relationship(back_populates="edges")


class ApplicationSettingModel(Base):
    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_text: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ProcessingTaskModel(Base):
    __tablename__ = "processing_tasks"
    __table_args__ = (
        UniqueConstraint(
            "scope_key",
            "idempotency_key",
            name="uq_processing_tasks_scope_idempotency",
        ),
        CheckConstraint(
            "length(trim(scope_key)) > 0 AND ("
            "(visibility = 'GLOBAL_DERIVED' AND scope_key = 'global' "
            " AND workspace_id IS NULL AND requested_by_user_id IS NULL "
            " AND credential_id IS NULL) OR "
            "(visibility = 'WORKSPACE_DERIVED' "
            " AND scope_key = 'workspace:' || workspace_id "
            " AND workspace_id IS NOT NULL AND requested_by_user_id IS NOT NULL) OR "
            "(visibility = 'USER_PRIVATE' "
            " AND scope_key = 'user:' || workspace_id || ':' || requested_by_user_id "
            " AND workspace_id IS NOT NULL AND requested_by_user_id IS NOT NULL) OR "
            "(visibility = 'SYSTEM' AND scope_key LIKE 'system:%' "
            " AND workspace_id IS NULL AND requested_by_user_id IS NULL "
            " AND credential_id IS NULL))",
            name="processing_tasks_visibility_scope",
        ),
        Index("ix_processing_tasks_claim", "status", "priority", "available_at"),
        Index("ix_processing_tasks_paper", "paper_id"),
        Index(
            "ix_processing_tasks_visibility_scope",
            "visibility",
            "scope_key",
            "status",
        ),
        Index(
            "ix_processing_tasks_workspace",
            "workspace_id",
            "status",
            "created_at",
        ),
        Index("ix_processing_tasks_requester", "requested_by_user_id", "workspace_id"),
        Index("ix_processing_tasks_credential", "credential_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    visibility: Mapped[str] = mapped_column(
        String(32), nullable=False, default="SYSTEM", server_default="SYSTEM"
    )
    scope_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="system:compatibility",
        server_default="system:compatibility",
    )
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    credential_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_llm_credentials.id", ondelete="SET NULL")
    )
    paper_id: Mapped[str | None] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TaskStatus.QUEUED)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped[WorkspaceModel | None] = relationship(back_populates="tasks")
    requester: Mapped[UserModel | None] = relationship(
        back_populates="requested_tasks",
        foreign_keys=[requested_by_user_id],
    )
    credential: Mapped[UserLlmCredentialModel | None] = relationship(back_populates="tasks")
    paper: Mapped[PaperModel | None] = relationship(back_populates="tasks")


@event.listens_for(Session, "before_flush")
def _enforce_diagnostic_secret_boundaries(
    session: Session,
    _flush_context: object,
    _instances: object,
) -> None:
    """Cover direct ORM writes as well as application and queue adapter writes."""

    for candidate in session.new.union(session.dirty):
        if isinstance(candidate, ProcessingTaskModel):
            require_secret_free_task_data(candidate.payload_json, field_name="payload")
            require_secret_free_task_data(candidate.result_json, field_name="result")
            candidate.error_code = sanitize_diagnostic_identifier(
                candidate.error_code,
                max_chars=64,
            )
            if candidate.error_message is not None:
                candidate.error_message = redact_task_error_message(candidate.error_message)
        elif isinstance(
            candidate,
            (TranslationModel, PaperMapModel, PaperMapQuestionModel),
        ):
            candidate.provider_request_id = sanitize_diagnostic_identifier(
                candidate.provider_request_id,
                max_chars=255,
            )
            candidate.error_type = sanitize_diagnostic_identifier(
                candidate.error_type,
                max_chars=64,
            )
            candidate.error_code = sanitize_diagnostic_identifier(
                candidate.error_code,
                max_chars=128,
            )
            if candidate.error_message is not None:
                candidate.error_message = redact_task_error_message(candidate.error_message)
        elif isinstance(candidate, AnnotationModel):
            candidate.provider_request_id = sanitize_diagnostic_identifier(
                candidate.provider_request_id,
                max_chars=255,
            )
            candidate.qa_error_type = sanitize_diagnostic_identifier(
                candidate.qa_error_type,
                max_chars=64,
            )
            candidate.qa_error_code = sanitize_diagnostic_identifier(
                candidate.qa_error_code,
                max_chars=128,
            )
            if candidate.qa_error_message is not None:
                candidate.qa_error_message = redact_task_error_message(
                    candidate.qa_error_message
                )
