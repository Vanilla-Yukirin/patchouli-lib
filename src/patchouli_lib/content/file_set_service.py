"""Internal, transaction-neutral append of a complete Page file snapshot.

This is not a public mutation route. A caller must own BEGIN IMMEDIATE, authorize
the Page, and persist audit/idempotency data before committing its transaction.
"""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import Connection

from patchouli_lib.content.file_manifest import FileManifest, build_file_manifest
from patchouli_lib.content.presentation import (
    Presentation,
    presentation_for,
    read_presentation,
    save_presentation,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import NewPageSource, PageRecord
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.identifiers import MAX_REVISION_NUMBER, canonical_utc_wire, validate_revision_id


class FileSetPageNotFoundError(RuntimeError):
    """The target Page is absent or has been soft-deleted."""


class FileSetPreconditionFailedError(RuntimeError):
    """The supplied ETag no longer identifies the target Page state."""


@dataclass(frozen=True, slots=True)
class FileSetAppendResult:
    changed: bool
    page: PageRecord
    etag: str
    manifest: FileManifest = field(repr=False)


class FileSetRevisionService:
    """Append only after the outer caller has authorized and begun a write transaction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._repository = ContentRepository(connection)

    def append_existing_page(
        self,
        *,
        library_id: str,
        page_id: str,
        expected_etag: str,
        files: Iterable[tuple[str, bytes]],
        revision_id: str,
        revision_at: int,
        source: NewPageSource,
        primary_file: str | None = None,
        presentation_override: Presentation | None = None,
    ) -> FileSetAppendResult:
        """Write one full flat snapshot or return an exact no-op.

        The new Revision, file set, Source and Page advance are a savepoint
        within the caller's write transaction. No audit/idempotency action is
        performed here, and a successful return must not be committed alone.
        """

        if not self._connection.in_transaction():
            raise RuntimeError("An active caller-owned SQLite write transaction is required.")
        raw = self._connection.connection.driver_connection
        if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
            # SQLAlchemy's autobegin after a SELECT does not necessarily issue
            # a real SQLite BEGIN. Starting a top-level SAVEPOINT in that state
            # would commit this append when RELEASE runs, despite a later
            # caller rollback. Require a concrete outer SQLite transaction.
            raise RuntimeError("An active caller-owned SQLite write transaction is required.")
        if type(expected_etag) is not str:
            raise TypeError("A current strong Page ETag is required.")
        manifest = build_file_manifest(files)
        presentation = presentation_for(manifest, primary_file)
        if presentation_override is not None:
            if (
                presentation_override.primary_file != primary_file
                or not presentation_override.display_title.strip()
                or "\x00" in presentation_override.display_title
            ):
                raise ValueError("Restored presentation must identify the same entry file.")
            presentation = presentation_override
        page = self._repository.get_page(library_id, page_id)
        if page is None or page.deleted_at is not None:
            raise FileSetPageNotFoundError("Page is not available.")
        current_etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
        if not hmac.compare_digest(expected_etag, current_etag):
            raise FileSetPreconditionFailedError("Page current ETag does not match.")
        current_manifest = self._repository.get_current_file_manifest(page)
        current_presentation = read_presentation(
            self._connection, page.library_id, page.page_uid, page.current_revision_id
        )
        if manifest == current_manifest and presentation == current_presentation:
            return FileSetAppendResult(
                changed=False,
                page=page,
                etag=current_etag,
                manifest=current_manifest,
            )

        validate_revision_id(revision_id)
        if page.current_revision_number >= MAX_REVISION_NUMBER:
            raise ValueError("Revision number is exhausted.")
        if type(revision_at) is not int or revision_at <= page.updated_at:
            raise ValueError("Revision time must strictly advance the Page clock.")
        canonical_utc_wire(revision_at)
        if type(source) is not NewPageSource or (
            source.library_id != page.library_id
            or source.page_uid != page.page_uid
            or source.revision_id != revision_id
            or source.revision_number != page.current_revision_number + 1
        ):
            raise ValueError("Source must identify the exact new Revision.")

        with self._connection.begin_nested():
            self._repository.add_file_set_revision(
                page,
                revision_id=revision_id,
                created_at=revision_at,
                manifest=manifest,
            )
            self._repository.add_source(source)
            save_presentation(
                self._connection,
                library_id=page.library_id,
                page_uid=page.page_uid,
                revision_id=revision_id,
                revision_number=page.current_revision_number + 1,
                presentation=presentation,
            )
            advanced = self._repository.advance_file_set_current_revision(
                page,
                revision_id=revision_id,
                updated_at=revision_at,
            )
            if advanced is None:
                raise FileSetPreconditionFailedError("Page changed during file set append.")
            readback = self._repository.get_current_file_manifest(advanced)
            if readback != manifest:
                raise RuntimeError("Persisted Revision differs from the supplied file snapshot.")

        return FileSetAppendResult(
            changed=True,
            page=advanced,
            etag=page_current_etag(
                advanced.page_uid,
                advanced.current_revision_id,
                advanced.current_revision_number,
                advanced.occurred_at,
                advanced.updated_at,
            ),
            manifest=readback,
        )


__all__ = [
    "FileSetAppendResult",
    "FileSetPageNotFoundError",
    "FileSetPreconditionFailedError",
    "FileSetRevisionService",
]
