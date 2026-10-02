"""Restore exact historical file bytes through the existing master revision path."""

from __future__ import annotations

from collections.abc import Callable
from time import time

from pydantic import Field, field_validator
from sqlalchemy import Engine, select

from patchouli_lib.admin.file_set_service import (
    MasterFileSetNotFoundError,
    MasterFileSetResult,
    MasterFileSetService,
)
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.api.request_ids import generate_request_id
from patchouli_lib.auth.service import AuthenticationError, utc_microseconds
from patchouli_lib.content.file_set_write_service import FileSetAppendCommand
from patchouli_lib.content.models import Revision
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    ContentSchema,
    OpaqueId,
    PageId,
    StrongPageETag,
)
from patchouli_lib.identifiers import MAX_REVISION_NUMBER, validate_page_id
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.retrieval.file_set_read import (
    FileSetReadPersistenceError,
    read_verified_revision_snapshot,
)


class MasterRevisionRestoreCommand(ContentSchema):
    """Restore one complete source snapshot without changing its immutable history."""

    library_id: OpaqueId
    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    source_revision_number: int = Field(ge=1, le=MAX_REVISION_NUMBER)
    expected_etag: StrongPageETag = Field(repr=False)

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)


class MasterRevisionRestoreService:
    """Read a verified source, then let the revision service own the write transaction."""

    def __init__(self, engine: Engine, *, clock: Callable[[], int] = utc_microseconds) -> None:
        self._engine = engine
        self._clock = clock

    def restore_revision(
        self,
        command: MasterRevisionRestoreCommand,
        idempotency: ArchiveIdempotencyKey,
        *,
        master_session: MasterAdminSession,
    ) -> MasterFileSetResult:
        with self._engine.connect() as connection:
            # pysqlite's SELECT autobegin does not establish a concrete snapshot.
            connection.exec_driver_sql("BEGIN")
            try:
                if (
                    type(master_session) is not MasterAdminSession
                    or master_session.expires_at <= int(time())
                    or not MasterTokenRepository(connection).is_session_generation_current(
                        master_session.identity_id, master_session.session_generation
                    )
                ):
                    raise AuthenticationError
                book = LibraryRepository(connection).get_book(
                    command.library_id, command.section_id, command.book_id
                )
                page = ContentRepository(connection).get_page(command.library_id, command.page_id)
                if (
                    book is None
                    or page is None
                    or command.source_revision_number > page.current_revision_number
                ):
                    raise MasterFileSetNotFoundError("Source version is not available.")
                revision_id = connection.scalar(
                    select(Revision.revision_id).where(
                        Revision.library_id == page.library_id,
                        Revision.page_uid == page.page_uid,
                        Revision.revision_number == command.source_revision_number,
                    )
                )
                if revision_id is None:
                    raise MasterFileSetNotFoundError("Source version is not available.")
                snapshot = read_verified_revision_snapshot(connection, page, revision_id)
                if snapshot.revision_number != command.source_revision_number:
                    raise FileSetReadPersistenceError
            finally:
                connection.rollback()

        # Resolve the immutable source by stable Page identity. Do not reject a
        # subsequent move, deletion or stale ETag in the read phase. The locked
        # service authenticates again and checks a prior success before deciding
        # whether a new write is allowed. It also closes the read/write race.
        append = FileSetAppendCommand(
            library_id=command.library_id,
            section_id=command.section_id,
            page_id=command.page_id,
            expected_etag=command.expected_etag,
            files=tuple((entry.name, entry.content) for entry in snapshot.manifest.files),
            source=ArchiveSourceInput(kind="revision_restore", locator=snapshot.revision_id),
            request_id=generate_request_id(),
        )
        return MasterFileSetService(self._engine, clock=self._clock).revise_page(
            append, command.book_id, idempotency, master_session=master_session
        )
