"""Workspace-bounded library collections, explicit grants and durable import receipts."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, delete, exists, func, select, update
from sqlalchemy.orm import Session

from app.domain.arxiv import ArxivIdentifier, normalize_arxiv_identifier
from app.domain.auth import Permission, RequestPrincipal
from app.domain.enums import TaskStatus
from app.domain.tasks import TaskVisibility
from app.infrastructure.arxiv.versions import ArxivVersionIndex
from app.infrastructure.db.library_models import (
    LibraryEntryModel,
    LibraryGrantModel,
    LibraryImportKeyModel,
    LibraryModel,
)
from app.infrastructure.db.models import (
    LibraryItemModel,
    PaperModel,
    ProcessingTaskModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
    now_utc,
)

LibraryAction = Literal["read", "import", "manage", "remove"]
ACTIONS = ("read", "import", "manage", "remove")


class LibraryError(RuntimeError):
    def __init__(self, code: str, status: int = 404) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def _member_predicate(principal: RequestPrincipal):
    return exists(
        select(WorkspaceMembershipModel.user_id)
        .join(
            UserModel,
            UserModel.id == WorkspaceMembershipModel.user_id,
        )
        .where(
            WorkspaceMembershipModel.workspace_id == principal.active_workspace_id,
            WorkspaceMembershipModel.user_id == principal.user_id,
            UserModel.status == "ACTIVE",
        )
    )


def library_access_predicate(principal: RequestPrincipal, action: LibraryAction = "read"):
    if action not in ACTIONS:
        raise ValueError("Unsupported library action")
    granted = exists(
        select(LibraryGrantModel.library_id).where(
            LibraryGrantModel.library_id == LibraryModel.id,
            LibraryGrantModel.user_id == principal.user_id,
            getattr(LibraryGrantModel, "can_" + action).is_(True),
        )
    )
    return and_(
        LibraryModel.workspace_id == principal.active_workspace_id,
        _member_predicate(principal),
        granted,
    )


def library_item_access_predicate(principal: RequestPrincipal, action: LibraryAction = "read"):
    return and_(
        LibraryItemModel.workspace_id == principal.active_workspace_id,
        exists(
            select(LibraryEntryModel.library_item_id)
            .join(
                LibraryModel,
                LibraryModel.id == LibraryEntryModel.library_id,
            )
            .where(
                LibraryEntryModel.library_item_id == LibraryItemModel.id,
                LibraryModel.workspace_id == LibraryItemModel.workspace_id,
                library_access_predicate(principal, action),
            )
        ),
    )


def paper_access_predicate(principal: RequestPrincipal, action: LibraryAction = "read"):
    return exists(
        select(LibraryItemModel.id).where(
            LibraryItemModel.paper_id == PaperModel.id,
            library_item_access_predicate(principal, action),
        )
    )


def load_accessible_library_item(
    session: Session,
    principal: RequestPrincipal,
    item_id: str,
    *,
    action: LibraryAction = "read",
) -> LibraryItemModel | None:
    if not principal.has(Permission.LIBRARY_READ) or principal.onboarding_required:
        return None
    return session.scalar(
        select(LibraryItemModel).where(
            LibraryItemModel.id == item_id,
            library_item_access_predicate(principal, action),
        )
    )


def ensure_default_library(session: Session, principal: RequestPrincipal) -> LibraryModel:
    """Only bootstrap a missing workspace default once; never regenerate revoked grants.

    The caller owns commit/rollback. This also handles a new workspace created after migration.
    Existing defaults are left unchanged even if the caller has since lost every grant.
    """
    library = session.scalar(
        select(LibraryModel).where(
            LibraryModel.workspace_id == principal.active_workspace_id,
            LibraryModel.key == "default",
        )
    )
    if library is not None:
        return library
    if not session.scalar(select(_member_predicate(principal))):
        raise LibraryError("LIBRARY_NOT_FOUND")
    workspace = session.get(WorkspaceModel, principal.active_workspace_id)
    members = (
        session.execute(
            select(WorkspaceMembershipModel).where(
                WorkspaceMembershipModel.workspace_id == principal.active_workspace_id,
            )
        )
        .scalars()
        .all()
    )
    members.sort(key=lambda member: (member.role != "OWNER", member.user_id))
    if not workspace or not members:
        raise LibraryError("LIBRARY_NOT_FOUND")
    library = LibraryModel(
        id=str(uuid5(NAMESPACE_URL, "academatepaper:default-library:" + workspace.id)),
        workspace_id=workspace.id,
        owner_user_id=members[0].user_id,
        key="default",
        name=workspace.name,
    )
    session.add(library)
    session.flush()
    for member in members:
        session.add(
            LibraryGrantModel(
                library_id=library.id,
                user_id=member.user_id,
                grant_source="WORKSPACE_BOOTSTRAP",
                granted_by_user_id=library.owner_user_id,
                can_read=True,
                can_import=True,
                can_manage=member.role == "OWNER",
                can_remove=True,
            )
        )
    for item_id in session.scalars(
        select(LibraryItemModel.id).where(
            LibraryItemModel.workspace_id == workspace.id,
        )
    ):
        session.add(LibraryEntryModel(library_id=library.id, library_item_id=item_id))
    session.flush()
    return library


def require_library(
    session: Session,
    principal: RequestPrincipal,
    library_id: str,
    action: LibraryAction = "read",
) -> LibraryModel:
    permission = Permission.LIBRARY_READ if action == "read" else Permission.LIBRARY_WRITE
    if not principal.has(permission) or principal.onboarding_required:
        raise LibraryError("LIBRARY_ACCESS_DENIED", 403)
    library = session.scalar(
        select(LibraryModel).where(
            LibraryModel.id == library_id,
            library_access_predicate(principal, action),
        )
    )
    if library is None:
        raise LibraryError("LIBRARY_NOT_FOUND")
    return library


def create_library(session: Session, principal: RequestPrincipal, name: str) -> LibraryModel:
    if not principal.has(Permission.LIBRARY_WRITE):
        raise LibraryError("LIBRARY_ACCESS_DENIED", 403)
    if not session.scalar(select(_member_predicate(principal))):
        raise LibraryError("LIBRARY_NOT_FOUND")
    library = LibraryModel(
        workspace_id=principal.active_workspace_id,
        owner_user_id=principal.user_id,
        key=str(uuid4()),
        name=name,
    )
    session.add(library)
    session.flush()
    session.add(
        LibraryGrantModel(
            library_id=library.id,
            user_id=principal.user_id,
            grant_source="LIBRARY_CREATION",
            granted_by_user_id=principal.user_id,
            can_read=True,
            can_import=True,
            can_manage=True,
            can_remove=True,
        )
    )
    session.flush()
    return library


def advance_revision(session: Session, library: LibraryModel, expected: int) -> None:
    result = session.execute(
        update(LibraryModel)
        .where(
            LibraryModel.id == library.id,
            LibraryModel.revision == expected,
        )
        .values(revision=expected + 1, updated_at=now_utc()),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise LibraryError("REVISION_CONFLICT", 409)
    session.expire(library)
    session.refresh(library)


def set_grant(
    session: Session,
    principal: RequestPrincipal,
    library_id: str,
    user_id: str,
    permissions: dict[str, bool],
    *,
    expected_revision: int,
) -> LibraryModel:
    library = require_library(session, principal, library_id, "manage")
    member = session.scalar(
        select(WorkspaceMembershipModel)
        .join(
            UserModel,
            UserModel.id == WorkspaceMembershipModel.user_id,
        )
        .where(
            WorkspaceMembershipModel.workspace_id == library.workspace_id,
            WorkspaceMembershipModel.user_id == user_id,
            UserModel.status == "ACTIVE",
        )
    )
    if member is None:
        raise LibraryError("MEMBER_NOT_FOUND")
    if set(permissions) != set(ACTIONS) or any(
        type(value) is not bool for value in permissions.values()
    ):
        raise LibraryError("GRANT_INVALID", 422)
    if not permissions["read"] and (permissions["import"] or permissions["remove"]):
        raise LibraryError("GRANT_REQUIRES_READ", 422)
    if library.owner_user_id == user_id and not permissions["manage"]:
        raise LibraryError("LIBRARY_OWNER_GRANT_REQUIRED", 409)
    advance_revision(session, library, expected_revision)
    grant = session.get(LibraryGrantModel, (library_id, user_id))
    if grant is None:
        grant = LibraryGrantModel(library_id=library_id, user_id=user_id)
        session.add(grant)
    grant.grant_source = "USER_POLICY"
    grant.granted_by_user_id = principal.user_id
    for action, enabled in permissions.items():
        setattr(grant, "can_" + action, enabled)
    session.flush()
    return library


def revoke_grant(
    session: Session,
    principal: RequestPrincipal,
    library_id: str,
    user_id: str,
    *,
    expected_revision: int,
) -> LibraryModel:
    library = require_library(session, principal, library_id, "manage")
    if library.owner_user_id == user_id:
        raise LibraryError("LIBRARY_OWNER_GRANT_REQUIRED", 409)
    advance_revision(session, library, expected_revision)
    session.execute(
        delete(LibraryGrantModel).where(
            LibraryGrantModel.library_id == library_id,
            LibraryGrantModel.user_id == user_id,
        )
    )
    return library


def _load_import_receipt(
    session: Session,
    principal: RequestPrincipal,
    library_id: str,
    key_hash: str | None,
    request_hash: str,
) -> LibraryItemModel | None:
    if not key_hash:
        return None
    previous = session.get(LibraryImportKeyModel, (library_id, principal.user_id, key_hash))
    if previous is None:
        return None
    if previous.request_hash != request_hash:
        raise LibraryError("IDEMPOTENCY_CONFLICT", 409)
    item = session.get(LibraryItemModel, previous.library_item_id)
    if item is None:
        raise LibraryError("LIBRARY_ITEM_NOT_FOUND")
    return item


def has_library_import_receipt(
    session: Session,
    principal: RequestPrincipal,
    library_id: str,
    identifier: ArxivIdentifier,
    idempotency_key: str,
) -> bool:
    require_library(session, principal, library_id, "import")
    return _load_import_receipt(
        session,
        principal,
        library_id,
        hashlib.sha256(idempotency_key.encode()).hexdigest(),
        hashlib.sha256(identifier.canonical.encode()).hexdigest(),
    ) is not None


def resolve_arxiv_import_version(identifier: ArxivIdentifier, versions: ArxivVersionIndex) -> str:
    if identifier.version:
        return identifier.version
    latest = versions.lookup(identifier.arxiv_id, check=True, require_fresh=True)
    if latest is None:
        raise LibraryError("ARXIV_VERSION_UNAVAILABLE", 503)
    return latest.version


def import_into_library(
    session: Session,
    principal: RequestPrincipal,
    library_id: str,
    source: str,
    *,
    idempotency_key: str | None = None,
    resolved_version: str | None = None,
) -> tuple[LibraryItemModel, bool]:
    require_library(session, principal, library_id, "import")
    identifier = normalize_arxiv_identifier(source)
    request_hash = hashlib.sha256(identifier.canonical.encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    previous = _load_import_receipt(session, principal, library_id, key_hash, request_hash)
    if previous is not None:
        # A replay never re-adds an entry removed after the original request.
        return previous, False
    if not identifier.version and resolved_version:
        identifier = ArxivIdentifier(arxiv_id=identifier.arxiv_id, version=resolved_version)
    paper = session.scalar(
        select(PaperModel).where(
            func.lower(PaperModel.arxiv_id) == identifier.arxiv_id.lower(),
            PaperModel.arxiv_version == identifier.version,
        ).order_by((PaperModel.arxiv_id == identifier.arxiv_id).desc(), PaperModel.id)
    )
    if paper is None:
        paper = PaperModel(
            arxiv_id=identifier.arxiv_id,
            arxiv_version=identifier.version,
            title=f"arXiv {identifier.canonical}",
        )
        session.add(paper)
        session.flush()
    item = session.scalar(
        select(LibraryItemModel).where(
            LibraryItemModel.workspace_id == principal.active_workspace_id,
            LibraryItemModel.paper_id == paper.id,
        )
    )
    if item is None:
        item = LibraryItemModel(workspace_id=principal.active_workspace_id, paper_id=paper.id)
        session.add(item)
        session.flush()
    created = session.get(LibraryEntryModel, (library_id, item.id)) is None
    if created:
        session.add(LibraryEntryModel(library_id=library_id, library_item_id=item.id))
    # Shared application scheduling contract; neither API version runs a task handler.
    source_ready = any(
        source.source_type in {"ARXIV_TARBALL", "ARXIV_PDF"} for source in paper.sources
    )
    _ensure_import_task(
        session, paper=paper, task_type="FETCH_METADATA", priority=10, source_ready=False,
    )
    _ensure_import_task(
        session, paper=paper, task_type="FETCH_SOURCE", priority=20, source_ready=source_ready
    )
    if key_hash:
        session.add(
            LibraryImportKeyModel(
                library_id=library_id,
                user_id=principal.user_id,
                key_hash=key_hash,
                request_hash=request_hash,
                library_item_id=item.id,
            )
        )
    session.flush()
    return item, created


def remove_entry(
    session: Session,
    principal: RequestPrincipal,
    library_id: str,
    item_id: str,
    *,
    expected_revision: int,
) -> LibraryModel:
    library = require_library(session, principal, library_id, "remove")
    item = session.scalar(
        select(LibraryItemModel)
        .join(
            LibraryEntryModel,
            LibraryEntryModel.library_item_id == LibraryItemModel.id,
        )
        .where(
            LibraryEntryModel.library_id == library_id,
            LibraryItemModel.id == item_id,
            LibraryItemModel.workspace_id == library.workspace_id,
        )
    )
    if item is None:
        raise LibraryError("LIBRARY_ITEM_NOT_FOUND")
    advance_revision(session, library, expected_revision)
    session.execute(
        delete(LibraryEntryModel).where(
            LibraryEntryModel.library_id == library_id,
            LibraryEntryModel.library_item_id == item_id,
        )
    )
    # The workspace item, canonical paper, other-library references and private state survive.
    return library


def grant_new_workspace_member_defaults(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    granted_by_user_id: str | None,
    source: str,
) -> None:
    """Call only in the transaction creating a new membership, never on login/onboarding.

    Private libraries are never included, and an existing explicit/revoked member is never
    repaired here. Registration calls once after the new member is flushed.
    """
    principal = RequestPrincipal(user_id=user_id, active_workspace_id=workspace_id)
    library = ensure_default_library(session, principal)
    grant = session.get(LibraryGrantModel, (library.id, user_id))
    if grant is None:
        grant = LibraryGrantModel(library_id=library.id, user_id=user_id)
        session.add(grant)
    elif grant.grant_source != "WORKSPACE_BOOTSTRAP":
        return
    membership = session.get(WorkspaceMembershipModel, (workspace_id, user_id))
    grant.can_read = True
    grant.can_import = True
    grant.can_manage = membership.role == "OWNER"
    grant.can_remove = membership.role == "OWNER"
    grant.grant_source = source
    grant.granted_by_user_id = granted_by_user_id
    session.flush()


def has_current_library_access(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    paper_id: str,
    action: LibraryAction = "read",
) -> bool:
    """Execution-time ACL check for scoped queued work; never seeds/regrants permissions.

    Worker callers must additionally retain their existing task-type/user/credential permission
    checks. Global deterministic derivations are not attributed to an unrelated user's grant.
    """
    principal = RequestPrincipal(user_id=user_id, active_workspace_id=workspace_id)
    return (
        session.scalar(
            select(LibraryItemModel.id)
            .where(
                LibraryItemModel.paper_id == paper_id,
                library_item_access_predicate(principal, action),
            )
            .limit(1)
        )
        is not None
    )


def _ensure_import_task(
    session,
    *,
    paper: PaperModel,
    task_type: str,
    priority: int,
    source_ready: bool,
) -> None:
    idempotency_key = f"paper:{paper.id}:{task_type.lower()}"
    task = session.scalar(
        select(ProcessingTaskModel).where(
            ProcessingTaskModel.scope_key == "global",
            ProcessingTaskModel.idempotency_key == idempotency_key,
        )
    )
    if task is None:
        session.add(
            ProcessingTaskModel(
                paper_id=paper.id,
                task_type=task_type,
                priority=priority,
                idempotency_key=idempotency_key,
                visibility=TaskVisibility.GLOBAL_DERIVED,
                scope_key="global",
                workspace_id=None,
                requested_by_user_id=None,
                credential_id=None,
                payload_json={
                    "arxiv_id": paper.arxiv_id,
                    "arxiv_version": paper.arxiv_version,
                },
            )
        )
    elif task.status == TaskStatus.FAILED and not source_ready:
        task.status = TaskStatus.QUEUED
        task.attempt_count = 0
        task.available_at = now_utc()
        task.started_at = None
        task.finished_at = None
        task.error_code = None
        task.error_message = None


