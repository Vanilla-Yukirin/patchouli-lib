"""Authorization-scoped reads of current and historical Page file snapshots.

Both a legacy Markdown Revision and a file_set_v1 Revision are presented as
one complete flat file set. This service never interprets or renders content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field
from sqlalchemy import Connection, select

from patchouli_lib.auth.schemas import AuthenticatedCaller, CallerKind, CallerRecord, SectionAction
from patchouli_lib.auth.service import Clock, utc_microseconds
from patchouli_lib.content.file_manifest import FileManifest, build_file_manifest
from patchouli_lib.content.models import Revision, RevisionFileSet
from patchouli_lib.content.schemas import PageRecord, StrongPageETag
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.identifiers import validate_page_id, validate_revision_id
from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import (
    RevisionFileManifestView,
    RevisionFileRead,
    RevisionFileView,
)


class FileSetRevisionManifestView(RevisionFileManifestView):
    """One fully verified Revision snapshot, shared by single and multi-file Pages."""

    snapshot_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FileSetReadAuthenticationError(RuntimeError):
    """The authenticated credential or its caller is no longer active."""


class FileSetReadAuthorizationError(RuntimeError):
    """The caller lacks the requested Section read action."""


class FileSetReadNotFoundError(RuntimeError):
    """The resource does not exist in this caller-visible scope."""


class FileSetReadPersistenceError(RuntimeError):
    """The stored Revision cannot be returned as a complete verified snapshot."""


@dataclass(frozen=True, slots=True, repr=False)
class _VerifiedSnapshot:
    page_id: str
    revision_id: str
    revision_number: int
    manifest: FileManifest = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class FileSetCurrentRead:
    """Verified current manifest and its Page-state concurrency token."""

    manifest: FileSetRevisionManifestView
    etag: StrongPageETag


class FileSetReadService:
    """Read exact revisions while rechecking current caller and grant state."""

    def __init__(
        self,
        connection: Connection,
        authenticated: AuthenticatedCaller,
        *,
        clock: Clock = utc_microseconds,
    ) -> None:
        self._connection = connection
        self._repository = RetrievalRepository(connection)
        self._authenticated = authenticated
        self._clock = clock

    def list_files(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        revision_id: str,
    ) -> FileSetRevisionManifestView:
        snapshot = self._snapshot(library_id, section_id, page_id, revision_id)
        return self._manifest_view(snapshot)

    def current_files(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
    ) -> FileSetCurrentRead:
        """Read the current pointer, complete file set and ETag in one snapshot."""

        page = self._visible_page(library_id, section_id, page_id)
        try:
            snapshot = self._verified_snapshot(page, page.current_revision_id)
        except FileSetReadNotFoundError:
            # The Page exists, so a missing current Revision is storage corruption.
            raise FileSetReadPersistenceError from None
        if snapshot.revision_number != page.current_revision_number:
            raise FileSetReadPersistenceError
        try:
            etag = page_current_etag(
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                page.occurred_at,
                page.updated_at,
            )
        except ValueError:
            raise FileSetReadPersistenceError from None
        return FileSetCurrentRead(manifest=self._manifest_view(snapshot), etag=etag)

    @staticmethod
    def _manifest_view(snapshot: _VerifiedSnapshot) -> FileSetRevisionManifestView:
        return FileSetRevisionManifestView(
            page_id=snapshot.page_id,
            revision_id=snapshot.revision_id,
            revision_number=snapshot.revision_number,
            snapshot_sha256=snapshot.manifest.snapshot_sha256.hex(),
            files=[
                RevisionFileView(
                    filename=entry.name,
                    size_bytes=entry.content_size_bytes,
                    content_sha256=entry.content_sha256.hex(),
                )
                for entry in snapshot.manifest.files
            ],
        )

    def get_file(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        revision_id: str,
        filename: str,
    ) -> RevisionFileRead:
        snapshot = self._snapshot(library_id, section_id, page_id, revision_id)
        for entry in snapshot.manifest.files:
            if entry.name == filename:
                return RevisionFileRead(filename=entry.name, content=entry.content)
        raise FileSetReadNotFoundError

    def _snapshot(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        revision_id: str,
    ) -> _VerifiedSnapshot:
        page = self._visible_page(library_id, section_id, page_id)
        validate_revision_id(revision_id)
        return self._verified_snapshot(page, revision_id)

    def _visible_page(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
    ) -> PageRecord:
        caller = self._require_current_agent()
        if library_id != caller.library_id:
            raise FileSetReadNotFoundError
        actions = self._repository.section_actions(library_id, caller.id, section_id)
        if not actions:
            raise FileSetReadNotFoundError
        if SectionAction.PAGE_READ not in actions:
            raise FileSetReadAuthorizationError
        validate_page_id(page_id)
        page = self._repository.get_page(library_id, section_id, page_id)
        if page is None:
            raise FileSetReadNotFoundError
        return page

    def _verified_snapshot(self, page: PageRecord, revision_id: str) -> _VerifiedSnapshot:
        library_id = page.library_id
        revision = self._connection.execute(
            select(
                Revision.revision_id,
                Revision.revision_number,
                Revision.content_md,
                Revision.content_size_bytes,
                Revision.content_sha256,
                RevisionFileSet.storage_format,
                RevisionFileSet.file_count,
                RevisionFileSet.total_size_bytes,
                RevisionFileSet.snapshot_sha256,
            )
            .outerjoin(
                RevisionFileSet,
                (RevisionFileSet.library_id == Revision.library_id)
                & (RevisionFileSet.page_uid == Revision.page_uid)
                & (RevisionFileSet.revision_id == Revision.revision_id)
                & (RevisionFileSet.revision_number == Revision.revision_number),
            )
            .where(
                Revision.library_id == library_id,
                Revision.page_uid == page.page_uid,
                Revision.revision_id == revision_id,
            )
        ).one_or_none()
        if revision is None:
            raise FileSetReadNotFoundError
        revision_number = revision.revision_number
        if not self._repository.has_revision_file_seal(
            library_id, page.page_uid, revision_id, revision_number
        ):
            raise FileSetReadPersistenceError

        try:
            stored = self._repository.list_revision_files(
                library_id, page.page_uid, revision_id, revision_number
            )
            manifest = build_file_manifest((entry.name, entry.content) for entry in stored)
            if (
                len(stored) != len(manifest.files)
                or len(manifest.files) != revision.file_count
                or manifest.total_size_bytes != revision.total_size_bytes
                or any(
                    (entry.name, entry.content_size_bytes, entry.content_sha256)
                    != (original.name, original.size_bytes, original.content_sha256)
                    for entry, original in zip(manifest.files, stored, strict=True)
                )
            ):
                raise ValueError("Stored file metadata does not match file bytes.")
            if revision.storage_format == "legacy_markdown":
                if (
                    len(manifest.files) != 1
                    or manifest.files[0].name != "content.md"
                    or manifest.files[0].content != revision.content_md
                    or manifest.files[0].content_size_bytes != revision.content_size_bytes
                    or manifest.files[0].content_sha256 != revision.content_sha256
                    or revision.snapshot_sha256 is not None
                ):
                    raise ValueError("Legacy Markdown mirror does not match the file set.")
            elif revision.storage_format == "file_set_v1":
                if (
                    revision.content_md is not None
                    or revision.content_size_bytes is not None
                    or revision.content_sha256 is not None
                    or manifest.snapshot_sha256 != revision.snapshot_sha256
                ):
                    raise ValueError("File snapshot digest or format is invalid.")
            else:
                raise ValueError("Stored file snapshot format is not supported.")
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise FileSetReadPersistenceError from None
        return _VerifiedSnapshot(page.page_id, revision_id, revision_number, manifest)

    def _require_current_agent(self) -> CallerRecord:
        authenticated = self._authenticated
        identity = authenticated.caller
        credential = authenticated.credential
        if (
            identity.kind is not CallerKind.AGENT
            or credential.library_id != identity.library_id
            or credential.caller_id != identity.id
        ):
            raise FileSetReadAuthorizationError
        current = self._repository.get_caller(identity.library_id, identity.id)
        current_credential = self._repository.get_credential(
            identity.library_id, identity.id, credential.id
        )
        now = self._clock()
        if (
            current is None
            or current.kind is not CallerKind.AGENT
            or current.disabled_at is not None
            or current_credential is None
            or current_credential.revoked_at is not None
            or current_credential.rotated_at is not None
            or now < current_credential.created_at
            or now >= current_credential.expires_at
        ):
            raise FileSetReadAuthenticationError
        return current


__all__ = [
    "FileSetRevisionManifestView",
    "FileSetReadAuthenticationError",
    "FileSetReadAuthorizationError",
    "FileSetReadNotFoundError",
    "FileSetReadPersistenceError",
    "FileSetReadService",
]
