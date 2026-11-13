from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.application.runtime import DATABASE_SCHEMA_REVISION
from app.application.runtime_verification import SchemaRevisionInvariantError


@dataclass(frozen=True, slots=True)
class DatabaseSchemaRevisionInvariant:
    """Fail closed unless SQLite is at the one schema revision supported by this build."""

    session_factory: Any
    required_revision: str = DATABASE_SCHEMA_REVISION

    def verify(self) -> None:
        try:
            with self.session_factory() as session:
                revisions = list(
                    session.scalars(text("SELECT version_num FROM alembic_version")).all()
                )
        except SQLAlchemyError as error:
            raise SchemaRevisionInvariantError(
                "The application database revision cannot be verified."
            ) from error
        if revisions != [self.required_revision]:
            raise SchemaRevisionInvariantError(
                "The application database is not at the revision supported by this build."
            )
