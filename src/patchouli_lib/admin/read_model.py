"""Read-only, scoped projections for the administration browser."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from time import time_ns
from typing import Any

from sqlalchemy import Connection, Engine, and_, func, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql import Select

from patchouli_lib.admin.master_audit import grant_revisions_for_credentials
from patchouli_lib.auth.models import (
    AgentTokenValue,
    AuditEvent,
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
    MasterAuditEvent,
    SectionGrant,
)
from patchouli_lib.content.models import Page, Revision, RevisionFile
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.tags.models import PageTag, Tag

_MAX_FILE_SET_PREVIEW_BYTES = 64 * 1024
_PAGE_HISTORY_SIZE = 20


def _lower_hex_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


@dataclass(frozen=True)
class LibraryItem:
    id: str
    name: str
    description: str
    created_at: int
    page_count: int
    updated_at: int


@dataclass(frozen=True)
class SectionItem:
    id: str
    name: str
    description: str
    updated_at: int


@dataclass(frozen=True)
class BookItem:
    id: str
    name: str
    summary: str
    updated_at: int


@dataclass(frozen=True)
class PageItem:
    id: str
    title: str
    page_type: str
    occurred_at: int
    revision_number: int


@dataclass(frozen=True)
class TrashItem:
    id: str
    title: str
    page_type: str
    section_id: str
    section_name: str
    book_id: str
    book_name: str
    occurred_at: int
    deleted_at: int
    revision_number: int


@dataclass(frozen=True)
class TrashDirectoryView:
    library: LibraryItem
    section: SectionItem | None
    pages: tuple[TrashItem, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class TrashPageView:
    library: LibraryItem
    section: SectionItem
    page: TrashItem
    restore_etag: str | None


@dataclass(frozen=True)
class TagItem:
    id: str
    name: str
    created_at: int
    page_count: int


@dataclass(frozen=True)
class TaggedPageItem:
    id: str
    title: str
    section_id: str
    book_id: str
    occurred_at: int


@dataclass(frozen=True)
class PageTagChoice:
    id: str
    name: str
    attached: bool


@dataclass(frozen=True)
class TagDirectoryView:
    library: LibraryItem
    tags: tuple[TagItem, ...]


@dataclass(frozen=True)
class TagView:
    library: LibraryItem
    tag: TagItem
    pages: tuple[TaggedPageItem, ...]


@dataclass(frozen=True)
class LibraryView:
    library: LibraryItem
    sections: tuple[SectionItem, ...]


@dataclass(frozen=True)
class SectionView:
    library: LibraryItem
    section: SectionItem
    books: tuple[BookItem, ...]


@dataclass(frozen=True)
class BookView:
    library: LibraryItem
    section: SectionItem
    book: BookItem
    pages: tuple[PageItem, ...]


@dataclass(frozen=True)
class PageView:
    library: LibraryItem
    section: SectionItem
    book: BookItem
    page: PageItem
    markdown: str | None
    selected_revision_number: int
    selected_revision_created_at: int
    files: tuple[RevisionFileItem, ...]
    revisions: tuple[RevisionItem, ...]
    history_before_revision_number: int
    older_revisions_before: int | None
    history_is_latest: bool
    tag_choices: tuple[PageTagChoice, ...]


@dataclass(frozen=True)
class RevisionFileItem:
    name: str
    size_bytes: int
    sha256_hex: str


@dataclass(frozen=True)
class RevisionItem:
    number: int
    created_at: int


@dataclass(frozen=True)
class ContentActivityItem:
    library_id: str
    actor_home_library_id: str | None
    actor_id: str | None
    actor_name: str
    action: str
    occurred_at: int
    page_title: str | None
    section_id: str | None
    book_id: str | None
    page_id: str | None
    revision_number: int | None
    page_deleted: bool
    tag_id: str | None
    tag_name: str | None


@dataclass(frozen=True)
class CallerView:
    library_id: str
    id: str
    name: str
    description: str
    kind: str
    disabled_at: int | None
    updated_at: int
    credentials: tuple[CredentialItem, ...]
    section_grants: tuple[SectionGrantItem, ...]


@dataclass(frozen=True)
class CredentialItem:
    id: str
    created_at: int
    expires_at: int
    last_used_at: int | None
    revoked_at: int | None
    rotated_at: int | None
    token_tail: str | None
    library_grants_policy: bool = False
    library_grants: tuple[CredentialLibraryGrantItem, ...] = ()
    grant_revisions: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class CredentialLibraryGrantItem:
    library_id: str
    library_name: str
    action: str


@dataclass(frozen=True)
class SectionGrantItem:
    section_id: str
    section_name: str
    action: str


@dataclass(frozen=True)
class CallerItem:
    library_id: str
    library_name: str
    id: str
    name: str
    kind: str
    created_at: int
    disabled_at: int | None


class AdminReadModel:
    """Never issues DML and does not authenticate callers; the router does that first."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_libraries(self) -> tuple[LibraryItem, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(_library_summary_query()).mappings().all()
            return tuple(_library_item(row) for row in rows)

    def list_trash(
        self, library_id: str, *, section_id: str | None = None, before: str | None = None
    ) -> TrashDirectoryView | None:
        cursor = _parse_trash_cursor(before)
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            if library is None:
                return None
            section = (
                None if section_id is None else _get_section(connection, library_id, section_id)
            )
            if section_id is not None and section is None:
                return None
            statement = _trash_query(library_id).where(Page.deleted_at.is_not(None))
            if section_id is not None:
                statement = statement.where(Page.section_id == section_id)
            if cursor is not None:
                deleted_at, page_id = cursor
                statement = statement.where(
                    or_(
                        Page.deleted_at < deleted_at,
                        and_(Page.deleted_at == deleted_at, Page.page_id > page_id),
                    )
                )
            rows = (
                connection.execute(
                    statement.order_by(Page.deleted_at.desc(), Page.page_id).limit(21)
                )
                .mappings()
                .all()
            )
            items = tuple(_trash_item(row) for row in rows[:20])
            next_cursor = f"{items[-1].deleted_at}:{items[-1].id}" if len(rows) > 20 else None
            return TrashDirectoryView(library, section, items, next_cursor)

    def get_trash_page(
        self, library_id: str, section_id: str, page_id: str
    ) -> TrashPageView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            if library is None or section is None:
                return None
            row = (
                connection.execute(
                    _trash_query(library_id)
                    .add_columns(Page.page_uid, Page.current_revision_id, Page.updated_at)
                    .where(
                        Page.section_id == section_id,
                        Page.page_id == page_id,
                        Page.deleted_at.is_not(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            etag = (
                page_current_etag(
                    row["page_uid"],
                    row["current_revision_id"],
                    row["current_revision_number"],
                    row["occurred_at"],
                    row["updated_at"],
                )
                if row["page_type"] == "archive"
                else None
            )
            return TrashPageView(library, section, _trash_item(row), etag)

    def list_callers(self) -> tuple[CallerItem, ...]:
        """List identity metadata, never credential verifiers or raw tokens."""

        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        Caller.library_id,
                        Library.name.label("library_name"),
                        Caller.id,
                        Caller.name,
                        Caller.kind,
                        Caller.created_at,
                        Caller.disabled_at,
                    )
                    .join(Library, Library.id == Caller.library_id)
                    .order_by(Caller.created_at.desc(), Caller.id.desc())
                )
                .mappings()
                .all()
            )
            return tuple(CallerItem(**row) for row in rows)

    def recent_content_activity(self, *, limit: int = 50) -> tuple[ContentActivityItem, ...]:
        """Show successful content changes only; this is not an API request log."""

        if not 1 <= limit <= 50:
            raise ValueError("Activity limit must be between 1 and 50.")
        with self._engine.connect() as connection:
            # Keep the two audit streams and their linked content in one snapshot.
            connection.exec_driver_sql("BEGIN")
            events = (
                connection.execute(
                    select(
                        AuditEvent.id,
                        AuditEvent.library_id,
                        AuditEvent.actor_home_library_id,
                        AuditEvent.actor_caller_id,
                        Caller.name.label("actor_name"),
                        AuditEvent.action,
                        AuditEvent.resource_type,
                        AuditEvent.resource_id,
                        AuditEvent.occurred_at,
                    )
                    .join(
                        Caller,
                        and_(
                            Caller.library_id == AuditEvent.actor_home_library_id,
                            Caller.id == AuditEvent.actor_caller_id,
                        ),
                    )
                    .where(
                        AuditEvent.outcome == "succeeded",
                        AuditEvent.action.in_(
                            (
                                "content.archive.create",
                                "content.archive.revise",
                                "content.page.file_set.create",
                                "content.page.file_set.revise",
                                "content.archive.correct_occurrence",
                                "content.archive.delete",
                                "content.archive.restore",
                                "tag.create",
                                "tag.page.attach",
                                "tag.page.detach",
                            )
                        ),
                    )
                    .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            items: list[tuple[int, str, ContentActivityItem]] = []
            for event in events:
                action = event["action"]
                resource_type = event["resource_type"]
                page_id: str | None = None
                tag_id: str | None = None
                revision_number: int | None = None
                page = None
                if (
                    action
                    in (
                        "content.archive.revise",
                        "content.page.file_set.revise",
                    )
                    and resource_type == "revision"
                ):
                    page = (
                        connection.execute(
                            select(
                                Page.section_id,
                                Page.book_id,
                                Page.page_id,
                                Page.title,
                                Page.deleted_at,
                                Revision.revision_number,
                            )
                            .join(
                                Revision,
                                and_(
                                    Revision.library_id == Page.library_id,
                                    Revision.page_uid == Page.page_uid,
                                ),
                            )
                            .where(
                                Revision.library_id == event["library_id"],
                                Revision.revision_id == event["resource_id"],
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    revision_number = None if page is None else page["revision_number"]
                elif action == "tag.create" and resource_type == "tag":
                    tag_id = event["resource_id"]
                elif action in ("tag.page.attach", "tag.page.detach"):
                    if resource_type == "page_tag":
                        parts = event["resource_id"].split(":")
                        if len(parts) == 2 and all(parts):
                            page_id, tag_id = parts
                elif resource_type == "page":
                    page_id = event["resource_id"]
                    if action in ("content.archive.create", "content.page.file_set.create"):
                        revision_number = 1

                if page is None and page_id is not None:
                    page = (
                        connection.execute(
                            select(
                                Page.section_id,
                                Page.book_id,
                                Page.page_id,
                                Page.title,
                                Page.deleted_at,
                            ).where(
                                Page.library_id == event["library_id"],
                                Page.page_id == page_id,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                tag = (
                    connection.execute(
                        select(Tag.id, Tag.display_name).where(
                            Tag.library_id == event["library_id"], Tag.id == tag_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                    if tag_id is not None
                    else None
                )
                items.append(
                    (
                        event["occurred_at"],
                        event["id"],
                        ContentActivityItem(
                            library_id=event["library_id"],
                            actor_home_library_id=event["actor_home_library_id"],
                            actor_id=event["actor_caller_id"],
                            actor_name=event["actor_name"],
                            action=action,
                            occurred_at=event["occurred_at"],
                            page_title=None if page is None else page["title"],
                            section_id=None if page is None else page["section_id"],
                            book_id=None if page is None else page["book_id"],
                            page_id=None if page is None else page["page_id"],
                            revision_number=None if page is None else revision_number,
                            page_deleted=page is not None and page["deleted_at"] is not None,
                            tag_id=None if tag is None else tag["id"],
                            tag_name=None if tag is None else tag["display_name"],
                        ),
                    )
                )
            master_events = (
                connection.execute(
                    select(
                        MasterAuditEvent.id,
                        MasterAuditEvent.action,
                        MasterAuditEvent.target_type,
                        MasterAuditEvent.target_id,
                        MasterAuditEvent.occurred_at,
                    )
                    .where(
                        MasterAuditEvent.action.in_(
                            (
                                "content.archive.restore",
                                "tag.create",
                                "tag.page.attach",
                                "tag.page.detach",
                            )
                        )
                    )
                    .order_by(MasterAuditEvent.occurred_at.desc(), MasterAuditEvent.id.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            for event in master_events:
                parts = event["target_id"].split(":")
                if event["action"] == "content.archive.restore":
                    if event["target_type"] != "page" or len(parts) != 2:
                        continue
                    library_id, page_uid_hex = parts
                    if not all(_lower_hex_id(value) for value in parts):
                        continue
                    tag_id = None
                    page = (
                        connection.execute(
                            select(
                                Page.section_id,
                                Page.book_id,
                                Page.page_id,
                                Page.title,
                                Page.deleted_at,
                            ).where(
                                Page.library_id == library_id,
                                Page.page_uid == bytes.fromhex(page_uid_hex),
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                elif event["action"] == "tag.create":
                    if event["target_type"] != "tag" or len(parts) != 2:
                        continue
                    library_id, tag_id = parts
                    page = None
                else:
                    if event["target_type"] != "page_tag" or len(parts) != 3:
                        continue
                    library_id, page_uid_hex, tag_id = parts
                    if not all(
                        _lower_hex_id(value) for value in (library_id, page_uid_hex, tag_id)
                    ):
                        continue
                    page = (
                        connection.execute(
                            select(
                                Page.section_id,
                                Page.book_id,
                                Page.page_id,
                                Page.title,
                                Page.deleted_at,
                            ).where(
                                Page.library_id == library_id,
                                Page.page_uid == bytes.fromhex(page_uid_hex),
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                if not _lower_hex_id(library_id) or (
                    tag_id is not None and not _lower_hex_id(tag_id)
                ):
                    continue
                tag = (
                    connection.execute(
                        select(Tag.id, Tag.display_name).where(
                            Tag.library_id == library_id, Tag.id == tag_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                    if tag_id is not None
                    else None
                )
                items.append(
                    (
                        event["occurred_at"],
                        event["id"],
                        ContentActivityItem(
                            library_id=library_id,
                            actor_home_library_id=None,
                            actor_id=None,
                            actor_name="Administrator",
                            action=event["action"],
                            occurred_at=event["occurred_at"],
                            page_title=None if page is None else page["title"],
                            section_id=None if page is None else page["section_id"],
                            book_id=None if page is None else page["book_id"],
                            page_id=None if page is None else page["page_id"],
                            revision_number=None,
                            page_deleted=page is not None and page["deleted_at"] is not None,
                            tag_id=None if tag is None else tag["id"],
                            tag_name=None if tag is None else tag["display_name"],
                        ),
                    )
                )
            items.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
            return tuple(item for _, _, item in items[:limit])

    def get_caller(self, library_id: str, caller_id: str) -> CallerView | None:
        with self._engine.connect() as connection:
            # Keep grant state and its audit-backed version in one SQLite snapshot.
            connection.exec_driver_sql("BEGIN")
            now = time_ns() // 1_000
            row = (
                connection.execute(
                    select(
                        Caller.id,
                        Caller.library_id,
                        Caller.name,
                        Caller.description,
                        Caller.kind,
                        Caller.disabled_at,
                        Caller.updated_at,
                    ).where(Caller.library_id == library_id, Caller.id == caller_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            credential_rows = (
                connection.execute(
                    select(
                        Credential.id,
                        Credential.created_at,
                        Credential.expires_at,
                        Credential.last_used_at,
                        Credential.revoked_at,
                        Credential.rotated_at,
                        func.substr(AgentTokenValue.token_value, -4, 4).label("token_tail"),
                    )
                    .outerjoin(
                        AgentTokenValue,
                        and_(
                            AgentTokenValue.credential_id == Credential.id,
                            Credential.created_at <= now,
                            Credential.expires_at > now,
                            Credential.revoked_at.is_(None),
                            Credential.rotated_at.is_(None),
                        ),
                    )
                    .where(Credential.library_id == library_id, Credential.caller_id == caller_id)
                    .order_by(Credential.created_at.desc(), Credential.id.desc())
                )
                .mappings()
                .all()
            )
            policy_ids = set(
                connection.scalars(
                    select(CredentialLibraryPolicy.credential_id).where(
                        CredentialLibraryPolicy.home_library_id == library_id,
                        CredentialLibraryPolicy.caller_id == caller_id,
                    )
                )
            )
            library_grant_rows = connection.execute(
                select(
                    CredentialLibraryGrant.credential_id,
                    CredentialLibraryGrant.target_library_id.label("library_id"),
                    Library.name.label("library_name"),
                    CredentialLibraryGrant.action,
                )
                .join(Library, Library.id == CredentialLibraryGrant.target_library_id)
                .where(
                    CredentialLibraryGrant.home_library_id == library_id,
                    CredentialLibraryGrant.caller_id == caller_id,
                )
                .order_by(
                    CredentialLibraryGrant.credential_id,
                    Library.name,
                    CredentialLibraryGrant.action,
                )
            ).mappings()
            grants_by_credential: dict[str, list[CredentialLibraryGrantItem]] = {}
            for grant in library_grant_rows:
                grants_by_credential.setdefault(grant["credential_id"], []).append(
                    CredentialLibraryGrantItem(
                        library_id=grant["library_id"],
                        library_name=grant["library_name"],
                        action=grant["action"],
                    )
                )
            grant_revisions = grant_revisions_for_credentials(
                connection, (credential["id"] for credential in credential_rows)
            )
            grant_rows = (
                connection.execute(
                    select(
                        SectionGrant.section_id,
                        Section.name.label("section_name"),
                        SectionGrant.action,
                    )
                    .join(
                        Section,
                        and_(
                            Section.id == SectionGrant.section_id,
                            Section.library_id == SectionGrant.library_id,
                        ),
                    )
                    .where(
                        SectionGrant.library_id == library_id,
                        SectionGrant.caller_id == caller_id,
                    )
                    .order_by(Section.name, SectionGrant.section_id, SectionGrant.action)
                )
                .mappings()
                .all()
            )
            return CallerView(
                row["library_id"],
                row["id"],
                row["name"],
                row["description"],
                row["kind"],
                row["disabled_at"],
                row["updated_at"],
                tuple(
                    CredentialItem(
                        **credential,
                        library_grants_policy=credential["id"] in policy_ids,
                        library_grants=tuple(grants_by_credential.get(credential["id"], ())),
                        grant_revisions=tuple(
                            sorted(
                                (target_id, revision)
                                for (
                                    version_credential_id,
                                    target_id,
                                ), revision in grant_revisions.items()
                                if version_credential_id == credential["id"]
                            )
                        ),
                    )
                    for credential in credential_rows
                ),
                tuple(SectionGrantItem(**grant) for grant in grant_rows),
            )

    def get_library(self, library_id: str) -> LibraryView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            if library is None:
                return None
            rows = connection.execute(
                select(Section.id, Section.name, Section.description, Section.updated_at)
                .where(Section.library_id == library_id)
                .order_by(Section.name, Section.id)
            ).mappings()
            sections = tuple(_section_item(row) for row in rows)
            return LibraryView(library, sections)

    def list_library_tags(self, library_id: str) -> TagDirectoryView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            if library is None:
                return None
            rows = connection.execute(_tag_summary_query(library_id)).mappings()
            return TagDirectoryView(library, tuple(_tag_item(row) for row in rows))

    def get_library_tag(self, library_id: str, tag_id: str) -> TagView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            if library is None:
                return None
            tag_row = (
                connection.execute(_tag_summary_query(library_id).where(Tag.id == tag_id))
                .mappings()
                .one_or_none()
            )
            if tag_row is None:
                return None
            page_rows = connection.execute(
                select(
                    Page.page_id,
                    Page.title,
                    Page.section_id,
                    Page.book_id,
                    Page.occurred_at,
                )
                .join(
                    PageTag,
                    and_(
                        PageTag.library_id == Page.library_id,
                        PageTag.page_uid == Page.page_uid,
                    ),
                )
                .where(
                    Page.library_id == library_id,
                    PageTag.tag_id == tag_id,
                    Page.deleted_at.is_(None),
                )
                .order_by(Page.occurred_at.desc(), Page.page_id)
            ).mappings()
            pages = tuple(
                TaggedPageItem(
                    row["page_id"],
                    row["title"],
                    row["section_id"],
                    row["book_id"],
                    row["occurred_at"],
                )
                for row in page_rows
            )
            return TagView(library, _tag_item(tag_row), pages)

    def get_section(self, library_id: str, section_id: str) -> SectionView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            if library is None or section is None:
                return None
            rows = connection.execute(
                select(Book.id, Book.name, Book.summary, Book.updated_at)
                .where(Book.library_id == library_id, Book.section_id == section_id)
                .order_by(Book.name, Book.id)
            ).mappings()
            books = tuple(_book_item(row) for row in rows)
            return SectionView(library, section, books)

    def get_book(self, library_id: str, section_id: str, book_id: str) -> BookView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            book = _get_book(connection, library_id, section_id, book_id)
            if library is None or section is None or book is None:
                return None
            rows = connection.execute(
                select(
                    Page.page_id,
                    Page.title,
                    Page.page_type,
                    Page.occurred_at,
                    Page.current_revision_number,
                )
                .where(
                    Page.library_id == library_id,
                    Page.section_id == section_id,
                    Page.book_id == book_id,
                    Page.deleted_at.is_(None),
                )
                .order_by(Page.occurred_at.desc(), Page.page_id)
            ).mappings()
            pages = tuple(_page_item(row) for row in rows)
            return BookView(library, section, book, pages)

    def get_page(
        self,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: int | None = None,
        before_revision_number: int | None = None,
    ) -> PageView | None:
        if revision_number is not None and not 1 <= revision_number <= (1 << 63) - 1:
            return None
        if before_revision_number is not None and not 2 <= before_revision_number <= 1 << 63:
            return None
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            book = _get_book(connection, library_id, section_id, book_id)
            if library is None or section is None or book is None:
                return None
            revision_match = (
                and_(
                    Revision.revision_number == Page.current_revision_number,
                    Revision.revision_id == Page.current_revision_id,
                )
                if revision_number is None
                else Revision.revision_number == revision_number
            )
            row = (
                connection.execute(
                    select(
                        Page.page_id,
                        Page.page_uid,
                        Page.title,
                        Page.page_type,
                        Page.occurred_at,
                        Page.current_revision_number,
                        Revision.content_md,
                        Revision.revision_id.label("selected_revision_id"),
                        Revision.revision_number.label("selected_revision_number"),
                        Revision.created_at.label("selected_revision_created_at"),
                    )
                    .join(
                        Revision,
                        and_(
                            Revision.library_id == Page.library_id,
                            Revision.page_uid == Page.page_uid,
                            revision_match,
                        ),
                    )
                    .where(
                        Page.library_id == library_id,
                        Page.section_id == section_id,
                        Page.book_id == book_id,
                        Page.page_id == page_id,
                        Page.deleted_at.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            current_revision_number = row["current_revision_number"]
            if (
                before_revision_number is not None
                and before_revision_number > current_revision_number + 1
            ):
                return None
            files = tuple(
                RevisionFileItem(
                    item["filename"], item["size_bytes"], bytes(item["content_sha256"]).hex()
                )
                for item in connection.execute(
                    select(
                        RevisionFile.filename,
                        RevisionFile.size_bytes,
                        RevisionFile.content_sha256,
                    )
                    .where(
                        RevisionFile.library_id == library_id,
                        RevisionFile.page_uid == row["page_uid"],
                        RevisionFile.revision_id == row["selected_revision_id"],
                        RevisionFile.revision_number == row["selected_revision_number"],
                    )
                    .order_by(RevisionFile.filename)
                ).mappings()
            )
            markdown: str | None
            if row["content_md"] is not None:
                # Preserve the existing legacy Archive preview, including its size limit.
                markdown = bytes(row["content_md"]).decode("utf-8")
            else:
                # A file-set Revision has no legacy Markdown mirror. Preview only
                # a small, verified content.md as escaped plain text in the page.
                preview = (
                    connection.execute(
                        select(
                            RevisionFile.content_bytes,
                            RevisionFile.size_bytes,
                            RevisionFile.content_sha256,
                        ).where(
                            RevisionFile.library_id == library_id,
                            RevisionFile.page_uid == row["page_uid"],
                            RevisionFile.revision_id == row["selected_revision_id"],
                            RevisionFile.revision_number == row["selected_revision_number"],
                            RevisionFile.filename == "content.md",
                            RevisionFile.size_bytes <= _MAX_FILE_SET_PREVIEW_BYTES,
                            func.length(RevisionFile.content_bytes) <= _MAX_FILE_SET_PREVIEW_BYTES,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                markdown = None
                if preview is not None:
                    content = bytes(preview["content_bytes"])
                    if (
                        len(content) == preview["size_bytes"]
                        and sha256(content).digest() == bytes(preview["content_sha256"])
                        and b"\x00" not in content
                    ):
                        with suppress(UnicodeDecodeError):
                            markdown = content.decode("utf-8", errors="strict")
            history_before = before_revision_number or current_revision_number + 1
            if before_revision_number is None and (
                current_revision_number - row["selected_revision_number"] >= _PAGE_HISTORY_SIZE
            ):
                history_before = row["selected_revision_number"] + 1
            history_filter = (
                Revision.revision_number <= (1 << 63) - 1
                if history_before == 1 << 63
                else Revision.revision_number < history_before
            )
            history_rows = tuple(
                connection.execute(
                    select(Revision.revision_number, Revision.created_at)
                    .where(
                        Revision.library_id == library_id,
                        Revision.page_uid == row["page_uid"],
                        history_filter,
                    )
                    .order_by(Revision.revision_number.desc())
                    .limit(_PAGE_HISTORY_SIZE + 1)
                ).mappings()
            )
            revisions = tuple(
                RevisionItem(item["revision_number"], item["created_at"])
                for item in history_rows[:_PAGE_HISTORY_SIZE]
            )
            older_revisions_before = (
                revisions[-1].number if len(history_rows) > _PAGE_HISTORY_SIZE else None
            )
            tag_choices = tuple(
                PageTagChoice(item["id"], item["display_name"], item["tag_id"] is not None)
                for item in connection.execute(
                    select(Tag.id, Tag.display_name, PageTag.tag_id)
                    .outerjoin(
                        PageTag,
                        and_(
                            PageTag.library_id == Tag.library_id,
                            PageTag.tag_id == Tag.id,
                            PageTag.page_uid == row["page_uid"],
                        ),
                    )
                    .where(Tag.library_id == library_id)
                    .order_by(Tag.match_key, Tag.id)
                ).mappings()
            )
            return PageView(
                library,
                section,
                book,
                _page_item(row),
                markdown,
                row["selected_revision_number"],
                row["selected_revision_created_at"],
                files,
                revisions,
                history_before,
                older_revisions_before,
                history_before == current_revision_number + 1,
                tag_choices,
            )


def _library_summary_query() -> Select[Any]:
    return (
        select(
            Library.id,
            Library.name,
            Library.description,
            Library.created_at,
            Library.updated_at,
            func.count(Page.page_uid).label("page_count"),
        )
        .outerjoin(
            Page,
            and_(Page.library_id == Library.id, Page.deleted_at.is_(None)),
        )
        .group_by(
            Library.id, Library.name, Library.description, Library.created_at, Library.updated_at
        )
        .order_by(Library.name, Library.id)
    )


def _trash_query(library_id: str) -> Select[Any]:
    # Deliberately omit Revision, RevisionFile and credential columns. This projection
    # must remain metadata-only even when the deleted Page has private body text.
    return (
        select(
            Page.page_id,
            Page.title,
            Page.page_type,
            Page.section_id,
            Section.name.label("section_name"),
            Page.book_id,
            Book.name.label("book_name"),
            Page.occurred_at,
            Page.deleted_at,
            Page.current_revision_number,
        )
        .select_from(Page)
        .join(
            Section,
            and_(Section.library_id == Page.library_id, Section.id == Page.section_id),
        )
        .join(
            Book,
            and_(
                Book.library_id == Page.library_id,
                Book.section_id == Page.section_id,
                Book.id == Page.book_id,
            ),
        )
        .where(Page.library_id == library_id)
    )


def _trash_item(row: RowMapping) -> TrashItem:
    return TrashItem(
        id=row["page_id"],
        title=row["title"],
        page_type=row["page_type"],
        section_id=row["section_id"],
        section_name=row["section_name"],
        book_id=row["book_id"],
        book_name=row["book_name"],
        occurred_at=row["occurred_at"],
        deleted_at=row["deleted_at"],
        revision_number=row["current_revision_number"],
    )


def _parse_trash_cursor(value: str | None) -> tuple[int, str] | None:
    if value is None:
        return None
    if len(value) > 100:
        raise ValueError("Invalid trash cursor.")
    timestamp, separator, page_id = value.partition(":")
    if (
        separator != ":"
        or not timestamp.isascii()
        or not timestamp.isdecimal()
        or not 0 <= int(timestamp) <= (1 << 63) - 1
        or not 1 <= len(page_id) <= 80
        or not page_id.isascii()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in page_id)
    ):
        raise ValueError("Invalid trash cursor.")
    return int(timestamp), page_id


def _tag_summary_query(library_id: str) -> Select[Any]:
    return (
        select(
            Tag.id,
            Tag.display_name,
            Tag.created_at,
            func.count(Page.page_uid).label("page_count"),
        )
        .outerjoin(
            PageTag,
            and_(PageTag.library_id == Tag.library_id, PageTag.tag_id == Tag.id),
        )
        .outerjoin(
            Page,
            and_(
                Page.library_id == PageTag.library_id,
                Page.page_uid == PageTag.page_uid,
                Page.deleted_at.is_(None),
            ),
        )
        .where(Tag.library_id == library_id)
        .group_by(Tag.id, Tag.display_name, Tag.created_at)
        .order_by(Tag.match_key, Tag.id)
    )


def _get_library(connection: Connection, library_id: str) -> LibraryItem | None:
    row = (
        connection.execute(_library_summary_query().where(Library.id == library_id))
        .mappings()
        .one_or_none()
    )
    return None if row is None else _library_item(row)


def _get_section(connection: Connection, library_id: str, section_id: str) -> SectionItem | None:
    row = (
        connection.execute(
            select(Section.id, Section.name, Section.description, Section.updated_at).where(
                Section.library_id == library_id,
                Section.id == section_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _section_item(row)


def _get_book(
    connection: Connection, library_id: str, section_id: str, book_id: str
) -> BookItem | None:
    row = (
        connection.execute(
            select(Book.id, Book.name, Book.summary, Book.updated_at).where(
                Book.library_id == library_id,
                Book.section_id == section_id,
                Book.id == book_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _book_item(row)


def _library_item(row: RowMapping) -> LibraryItem:
    return LibraryItem(
        row["id"],
        row["name"],
        row["description"],
        row["created_at"],
        row["page_count"],
        row["updated_at"],
    )


def _section_item(row: RowMapping) -> SectionItem:
    return SectionItem(row["id"], row["name"], row["description"], row["updated_at"])


def _book_item(row: RowMapping) -> BookItem:
    return BookItem(row["id"], row["name"], row["summary"], row["updated_at"])


def _page_item(row: RowMapping) -> PageItem:
    return PageItem(
        row["page_id"],
        row["title"],
        row["page_type"],
        row["occurred_at"],
        row["current_revision_number"],
    )


def _tag_item(row: RowMapping) -> TagItem:
    return TagItem(row["id"], row["display_name"], row["created_at"], row["page_count"])
