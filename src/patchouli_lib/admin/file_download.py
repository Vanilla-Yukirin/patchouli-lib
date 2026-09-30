"""Cookie-authorized reads of exact Page Revision files for the admin browser.

The router owns session verification and response headers. This service accepts
only a transaction-local authorization callback and returns verified raw bytes.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlalchemy import Connection, Engine, select

from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.content.file_manifest import normalize_file_name
from patchouli_lib.content.models import Revision
from patchouli_lib.identifiers import InvalidPageIdError, validate_page_id
from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.retrieval.file_set_read import (
    FileSetReadNotFoundError,
    FileSetReadPersistenceError,
    read_verified_revision_snapshot,
)
from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import RevisionFileRead

_OPAQUE_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_REVISION_NUMBER = (1 << 63) - 1


class AdminFileDownloadPersistenceError(RuntimeError):
    """A stored Revision could not be returned as a complete verified snapshot."""


class AdminFileDownloadService:
    """Resolve a browser path and verify all file bytes in one SQLite read view."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_file(
        self,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: int,
        filename: str,
        *,
        authorize: Callable[[Connection], bool],
    ) -> RevisionFileRead | None:
        """Return one exact file, or None for an unknown caller-visible path.

        ``authorize`` must check the current browser session on the supplied
        connection without ending its transaction. It runs before content or
        path inspection and must return False when the session is invalid.
        """

        with self._engine.connect() as connection:
            # With pysqlite, SQLAlchemy's implicit transaction does not start
            # a real read snapshot before the first SELECT.
            connection.exec_driver_sql("BEGIN")
            try:
                if not authorize(connection):
                    raise AuthenticationError
                return self._get_file(
                    connection,
                    library_id,
                    section_id,
                    book_id,
                    page_id,
                    revision_number,
                    filename,
                )
            finally:
                connection.rollback()

    @staticmethod
    def _get_file(
        connection: Connection,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: int,
        filename: str,
    ) -> RevisionFileRead | None:
        if (
            any(
                type(value) is not str or _OPAQUE_ID.fullmatch(value) is None
                for value in (library_id, section_id, book_id)
            )
            or type(revision_number) is not int
            or not 1 <= revision_number <= _MAX_REVISION_NUMBER
        ):
            return None
        try:
            validate_page_id(page_id)
            if normalize_file_name(filename) != filename:
                return None
        except (InvalidPageIdError, TypeError, ValueError, UnicodeError):
            return None

        if connection.scalar(select(Library.id).where(Library.id == library_id)) is None:
            return None
        if (
            connection.scalar(
                select(Section.id).where(Section.library_id == library_id, Section.id == section_id)
            )
            is None
        ):
            return None
        if (
            connection.scalar(
                select(Book.id).where(
                    Book.library_id == library_id,
                    Book.section_id == section_id,
                    Book.id == book_id,
                )
            )
            is None
        ):
            return None

        repository = RetrievalRepository(connection)
        page = repository.get_page(library_id, section_id, page_id)
        if (
            page is None
            or page.book_id != book_id
            or revision_number > page.current_revision_number
        ):
            return None
        revision_id = connection.scalar(
            select(Revision.revision_id).where(
                Revision.library_id == library_id,
                Revision.page_uid == page.page_uid,
                Revision.revision_number == revision_number,
            )
        )
        if revision_id is None:
            return None
        try:
            snapshot = read_verified_revision_snapshot(connection, page, revision_id)
        except (FileSetReadNotFoundError, FileSetReadPersistenceError):
            raise AdminFileDownloadPersistenceError from None
        if snapshot.revision_number != revision_number:
            raise AdminFileDownloadPersistenceError
        for entry in snapshot.manifest.files:
            if entry.name == filename:
                return RevisionFileRead(filename=entry.name, content=entry.content)
        return None


__all__ = ["AdminFileDownloadPersistenceError", "AdminFileDownloadService"]
