"""Verified current-Page text projection over the application's real tables.

This is an internal, unindexed building block, not an accepted persistent text
classification or public search contract. The caller must authorize the scope
and begin a concrete SQLite read transaction before calling it. Every file in
the sealed current snapshot must be labelled explicitly as text or opaque;
missing and extra labels fail closed instead of silently omitting content.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import Connection, select

from patchouli_lib.content.models import Page
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import PageRecord
from patchouli_lib.search.projection_v2 import LiteralPageProjection, project_page_text
from patchouli_lib.tags.models import PageTag, Tag

FileIndexingKind = Literal["text", "opaque"]


class ProjectionTransactionRequiredError(RuntimeError):
    """The caller has not pinned a concrete SQLite read snapshot."""


@dataclass(frozen=True, slots=True)
class ProjectionFile:
    """Source identity of one file in the verified current snapshot."""

    name: str
    indexing_kind: FileIndexingKind
    size_bytes: int
    content_sha256: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProjectionTag:
    """A Tag's composite Library identity; its name is not indexed as text."""

    library_id: str
    tag_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class CurrentPageProjection:
    """Verified source identities and literal terms for one live current Page."""

    library_id: str
    section_id: str
    book_id: str
    page_id: str
    page_uid: bytes = field(repr=False)
    revision_id: str
    revision_number: int
    title: str = field(repr=False)
    occurred_at: int
    updated_at: int
    snapshot_sha256: bytes = field(repr=False)
    classification_version: str
    files: tuple[ProjectionFile, ...]
    tags: tuple[ProjectionTag, ...] = field(repr=False)
    terms: LiteralPageProjection = field(repr=False)


def _require_read_snapshot(connection: Connection) -> None:
    if not connection.in_transaction():
        raise ProjectionTransactionRequiredError("A concrete SQLite read transaction is required.")
    raw = connection.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
        raise ProjectionTransactionRequiredError("A concrete SQLite read transaction is required.")


def _page_tags(connection: Connection, page: PageRecord) -> tuple[ProjectionTag, ...]:
    rows = connection.execute(
        select(PageTag.tag_id, Tag.id, Tag.display_name)
        .outerjoin(
            Tag,
            (Tag.library_id == PageTag.library_id) & (Tag.id == PageTag.tag_id),
        )
        .where(PageTag.library_id == page.library_id, PageTag.page_uid == page.page_uid)
        .order_by(PageTag.tag_id)
    )
    tags: list[ProjectionTag] = []
    for tag_id, found_id, display_name in rows:
        if found_id != tag_id or type(display_name) is not str:
            raise RuntimeError("Current Page Tag relationship is inconsistent.")
        tags.append(ProjectionTag(page.library_id, tag_id, display_name))
    return tuple(tags)


def load_current_page_projection(
    connection: Connection,
    *,
    library_id: str,
    page_uid: bytes,
    file_classification: Mapping[str, FileIndexingKind],
    classification_version: str,
) -> CurrentPageProjection | None:
    """Project a live Page's exact current Revision, never history or tombstones.

    The explicit file classification is only an input to this experiment; it
    does not choose or approve a durable MIME/extension policy. Even an opaque
    file contributes its name, but never its bytes. A declared text file must
    be complete, valid UTF-8 within the projection budget or this raises; no
    partial terms are returned. Authorization belongs to the caller and must
    be evaluated in the same read transaction before exposing this result.
    """

    _require_read_snapshot(connection)
    if type(classification_version) is not str or not classification_version.strip():
        raise ValueError("A nonempty text classification version is required.")
    if not isinstance(file_classification, Mapping):
        raise TypeError("Each current file requires an explicit indexing kind.")

    row = (
        connection.execute(
            select(Page.__table__).where(
                Page.library_id == library_id,
                Page.page_uid == page_uid,
                Page.deleted_at.is_(None),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    page = PageRecord.model_validate(dict(row))
    manifest = ContentRepository(connection).get_current_file_manifest(page)
    classifications = dict(file_classification)
    if classifications.keys() != {entry.name for entry in manifest.files} or any(
        kind not in ("text", "opaque") or type(kind) is not str for kind in classifications.values()
    ):
        raise ValueError("Every current file needs exactly one valid indexing kind.")

    text_files = tuple(
        (entry.name, entry.content)
        for entry in manifest.files
        if classifications[entry.name] == "text"
    )
    opaque_names = tuple(
        entry.name for entry in manifest.files if classifications[entry.name] == "opaque"
    )
    terms = project_page_text(
        title=page.title,
        body=None,
        text_files=text_files,
        opaque_file_names=opaque_names,
    )
    return CurrentPageProjection(
        library_id=page.library_id,
        section_id=page.section_id,
        book_id=page.book_id,
        page_id=page.page_id,
        page_uid=page.page_uid,
        revision_id=page.current_revision_id,
        revision_number=page.current_revision_number,
        title=page.title,
        occurred_at=page.occurred_at,
        updated_at=page.updated_at,
        snapshot_sha256=manifest.snapshot_sha256,
        classification_version=classification_version,
        files=tuple(
            ProjectionFile(
                entry.name,
                classifications[entry.name],
                entry.content_size_bytes,
                entry.content_sha256,
            )
            for entry in manifest.files
        ),
        tags=_page_tags(connection, page),
        terms=terms,
    )


__all__ = [
    "CurrentPageProjection",
    "FileIndexingKind",
    "ProjectionFile",
    "ProjectionTag",
    "ProjectionTransactionRequiredError",
    "load_current_page_projection",
]
