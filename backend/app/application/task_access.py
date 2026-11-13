"""Recheck durable task authority before execution and fenced publication.

These checks are intentionally independent of the submitting browser's old principal/session.
Role mappings, memberships, onboarding, grants and credential ownership are read anew from the
same transaction that will publish a handler result or an atomic storage mutation.
"""

from __future__ import annotations

from sqlalchemy import select

from app.application.libraries import has_current_library_access
from app.domain.auth import Permission
from app.domain.tasks import TaskScope, TaskVisibility
from app.infrastructure.db.library_models import LibraryEntryModel, LibraryGrantModel, LibraryModel
from app.infrastructure.db.models import (
    GroupMembershipModel,
    GroupPermissionModel,
    LibraryItemModel,
    UserLlmCredentialModel,
    UserModel,
    UserOnboardingStateModel,
    WorkspaceMembershipModel,
    WorkspaceRolePermissionModel,
)

_MODEL_TASKS = frozenset(
    {
        "TRANSLATE_BLOCK",
        "REPAIR_RENDER",
        "ANSWER_QUESTION",
        "PLAN_PAPER_MAP",
        "ANSWER_MAP_QUESTION",
    }
)
_OPERATION_PERMISSIONS = {
    "TRANSLATE_BLOCK": Permission.LIBRARY_WRITE,
    "REPAIR_RENDER": Permission.LIBRARY_WRITE,
    "ANSWER_QUESTION": Permission.ANNOTATION_WRITE,
    "PLAN_PAPER_MAP": Permission.PAPER_MAP_WRITE,
    "ANSWER_MAP_QUESTION": Permission.PAPER_MAP_WRITE,
    "RENDER_TABLE": Permission.LIBRARY_WRITE,
    "RENDER_FIGURE": Permission.LIBRARY_WRITE,
    "RENDER_LATEX_BLOCK": Permission.LIBRARY_WRITE,
    "RENDER_LATEX_FORMULA": Permission.LIBRARY_WRITE,
}


class TaskAccessRevoked(RuntimeError):
    """Fixed denial, kept separate from OS PermissionError so storage preserves its type."""


def _deny() -> None:
    raise TaskAccessRevoked("Task resource authorization is no longer active.")


def _effective_permissions(session, membership) -> set[str]:
    role_keys = set(
        session.scalars(
            select(WorkspaceRolePermissionModel.permission_key).where(
                WorkspaceRolePermissionModel.role == membership.role,
            )
        )
    )
    group_keys = set(
        session.scalars(
            select(GroupPermissionModel.permission_key)
            .join(
                GroupMembershipModel,
                GroupMembershipModel.group_id == GroupPermissionModel.group_id,
            )
            .where(GroupMembershipModel.user_id == membership.user_id)
        )
    )
    return role_keys | group_keys


def _global_reference_exists(session, paper_id: str) -> bool:
    candidates = session.scalars(
        select(WorkspaceMembershipModel)
        .select_from(LibraryItemModel)
        .join(LibraryEntryModel, LibraryEntryModel.library_item_id == LibraryItemModel.id)
        .join(LibraryModel, LibraryModel.id == LibraryEntryModel.library_id)
        .join(LibraryGrantModel, LibraryGrantModel.library_id == LibraryModel.id)
        .join(UserModel, UserModel.id == LibraryGrantModel.user_id)
        .join(UserOnboardingStateModel, UserOnboardingStateModel.user_id == UserModel.id)
        .join(
            WorkspaceMembershipModel,
            (
                (WorkspaceMembershipModel.user_id == UserModel.id)
                & (WorkspaceMembershipModel.workspace_id == LibraryItemModel.workspace_id)
            ),
        )
        .where(
            LibraryItemModel.paper_id == paper_id,
            LibraryModel.workspace_id == LibraryItemModel.workspace_id,
            UserModel.status == "ACTIVE",
            UserOnboardingStateModel.status == "COMPLETED",
            LibraryGrantModel.can_read.is_(True),
        )
        .distinct()
    ).all()
    return any(
        Permission.LIBRARY_READ.value in _effective_permissions(session, member)
        for member in candidates
    )


def require_task_access(session, task) -> None:
    # Validate persisted ownership even at publication. A client/user ID is never sufficient
    # evidence, and unknown/custom scoped visibility must not fall through as a global task.
    try:
        visibility = TaskVisibility(task.visibility)
        if visibility is TaskVisibility.SYSTEM or not task.paper_id:
            return
        scope = TaskScope(
            visibility,
            task.workspace_id,
            task.requested_by_user_id,
            task.credential_id,
        )
        if scope.scope_key != task.scope_key:
            _deny()
    except (TypeError, ValueError):
        _deny()
    if visibility is TaskVisibility.GLOBAL_DERIVED:
        if not _global_reference_exists(session, task.paper_id):
            _deny()
        return
    user = session.get(UserModel, task.requested_by_user_id)
    membership = session.get(
        WorkspaceMembershipModel, (task.workspace_id, task.requested_by_user_id)
    )
    onboarding = session.get(UserOnboardingStateModel, task.requested_by_user_id)
    if (
        user is None
        or user.status != "ACTIVE"
        or membership is None
        or onboarding is None
        or onboarding.status != "COMPLETED"
        or not has_current_library_access(
            session,
            user_id=user.id,
            workspace_id=task.workspace_id,
            paper_id=task.paper_id,
        )
    ):
        _deny()
    permissions = _effective_permissions(session, membership)
    required = {Permission.TASK_WRITE, Permission.LIBRARY_READ}
    if task.task_type in _OPERATION_PERMISSIONS:
        required.add(_OPERATION_PERMISSIONS[task.task_type])
    if task.task_type in _MODEL_TASKS:
        required.add(Permission.CREDENTIAL_READ)
        # M7 Server model jobs are user-credential bound. A deleted FK becoming NULL must
        # never turn a previously protected job into an environment-key fallback call.
        if not task.credential_id:
            _deny()
    if not {permission.value for permission in required} <= permissions:
        _deny()
    if task.credential_id:
        credential = session.get(UserLlmCredentialModel, task.credential_id)
        if credential is None or credential.user_id != task.requested_by_user_id:
            _deny()
