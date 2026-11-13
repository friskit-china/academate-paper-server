from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.application.libraries import ensure_default_library, library_item_access_predicate
from app.domain.auth import RequestPrincipal
from app.infrastructure.db.models import LibraryItemModel, PaperModel


@dataclass(frozen=True, slots=True)
class WorkspacePaper:
    paper: PaperModel
    library_item: LibraryItemModel


def load_workspace_paper(
    session: Any,
    principal: RequestPrincipal,
    paper_id: str,
    *,
    paper_options: tuple[Any, ...] = (),
) -> WorkspacePaper | None:
    """Load a canonical paper only through the active workspace's library membership."""

    ensure_default_library(session, principal)
    library_item = session.scalar(
        select(LibraryItemModel).where(
            library_item_access_predicate(principal),
            LibraryItemModel.paper_id == paper_id,
        )
    )
    if library_item is None:
        return None
    paper = session.scalar(
        select(PaperModel).options(*paper_options).where(PaperModel.id == paper_id)
    )
    if paper is None:
        # A valid database cannot contain this row because library_items.paper_id is a FK.  Fail
        # closed if a partially recovered/corrupt database is opened.
        return None
    return WorkspacePaper(paper=paper, library_item=library_item)


def has_workspace_paper(
    session: Any,
    principal: RequestPrincipal,
    paper_id: str,
) -> bool:
    ensure_default_library(session, principal)
    return (
        session.scalar(
            select(LibraryItemModel.id).where(
                library_item_access_predicate(principal),
                LibraryItemModel.paper_id == paper_id,
            )
        )
        is not None
    )
