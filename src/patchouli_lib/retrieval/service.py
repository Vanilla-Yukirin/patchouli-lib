"""Authorization-aware application service for exact non-search reads."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from patchouli_lib.api.contracts import Citation, build_api_v1_path
from patchouli_lib.auth.schemas import (
    AuthenticatedCaller,
    CallerKind,
    CallerRecord,
    SectionAction,
)
from patchouli_lib.auth.service import Clock, utc_microseconds
from patchouli_lib.content import page_current_etag
from patchouli_lib.content.schemas import PageRecord, RevisionRecord
from patchouli_lib.identifiers import (
    canonical_utc_wire,
    validate_page_id,
    validate_revision_number,
)
from patchouli_lib.retrieval.repository import RetrievalRepository, StoredDocument
from patchouli_lib.retrieval.schemas import (
    BookView,
    CurrentPageRead,
    KeysetPage,
    PageDocument,
    PageMetadata,
    PageView,
    ReadWindow,
    RevisionView,
    SectionView,
)


class RetrievalAuthorizationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Required retrieval authorization is not available.")


class RetrievalAuthenticationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Authenticated caller state is no longer active.")


class RetrievalNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Requested retrieval resource was not found.")


class RetrievalPersistenceError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Stored retrieval state is inconsistent.")


class RetrievalService:
    """Return current scoped reads without rendering or interpreting Markdown."""

    def __init__(
        self,
        repository: RetrievalRepository,
        authenticated: AuthenticatedCaller,
        *,
        clock: Clock = utc_microseconds,
    ) -> None:
        self._repository = repository
        self._authenticated = authenticated
        self._clock = clock

    def list_sections(self, window: ReadWindow | None = None) -> KeysetPage[SectionView]:
        caller = self._require_current_agent()
        resolved_window = window or ReadWindow()
        stored = self._repository.list_queryable_sections(
            caller.library_id,
            caller.id,
            resolved_window,
        )
        return self._map_page(
            stored,
            lambda section: SectionView(section_id=section.id, name=section.name),
        )

    def list_books(
        self,
        section_id: str,
        window: ReadWindow | None = None,
    ) -> KeysetPage[BookView]:
        caller = self._require_action(section_id, SectionAction.QUERY)
        if self._repository.get_section(caller.library_id, section_id) is None:
            raise RetrievalNotFoundError
        stored = self._repository.list_books(
            caller.library_id,
            section_id,
            window or ReadWindow(),
        )
        return self._map_page(
            stored,
            lambda book: BookView(
                section_id=book.section_id,
                book_id=book.id,
                title=book.name,
            ),
        )

    def list_pages(
        self,
        section_id: str,
        window: ReadWindow | None = None,
    ) -> KeysetPage[PageMetadata]:
        caller = self._require_action(section_id, SectionAction.QUERY)
        if self._repository.get_section(caller.library_id, section_id) is None:
            raise RetrievalNotFoundError
        stored = self._repository.list_pages(
            caller.library_id,
            section_id,
            window or ReadWindow(),
        )
        return self._map_page(stored, self._metadata)

    def get_current_page(self, section_id: str, page_id: str) -> CurrentPageRead:
        caller = self._require_action(section_id, SectionAction.PAGE_READ)
        validate_page_id(page_id)
        try:
            stored = self._repository.get_current_document(
                caller.library_id,
                section_id,
                page_id,
            )
        except RuntimeError:
            raise RetrievalPersistenceError from None
        if stored is None:
            raise RetrievalNotFoundError
        document = self._document(stored)
        if (
            document.revision.revision_id != document.page.current_revision_id
            or document.revision.revision_number != document.page.current_revision_number
        ):
            raise RetrievalPersistenceError
        return CurrentPageRead(
            document=document,
            etag=page_current_etag(
                stored.page.page_uid,
                stored.revision.revision_id,
                stored.revision.revision_number,
            ),
        )

    def get_revision(
        self,
        section_id: str,
        page_id: str,
        revision_number: int,
    ) -> PageDocument:
        caller = self._require_action(section_id, SectionAction.PAGE_READ)
        validate_page_id(page_id)
        validate_revision_number(revision_number)
        page = self._repository.get_page(caller.library_id, section_id, page_id)
        if page is None:
            raise RetrievalNotFoundError
        revision = self._repository.get_revision(
            caller.library_id,
            page.page_uid,
            revision_number,
        )
        if revision is None:
            raise RetrievalNotFoundError
        return self._document(StoredDocument(page=page, revision=revision))

    def _require_current_agent(self) -> CallerRecord:
        authenticated = self._authenticated
        identity = authenticated.caller
        credential = authenticated.credential
        if (
            identity.kind is not CallerKind.AGENT
            or credential.library_id != identity.library_id
            or credential.caller_id != identity.id
        ):
            raise RetrievalAuthorizationError
        current = self._repository.get_caller(identity.library_id, identity.id)
        current_credential = self._repository.get_credential(
            identity.library_id,
            identity.id,
            authenticated.credential.id,
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
            raise RetrievalAuthenticationError
        return current

    def _require_action(
        self,
        section_id: str,
        action: SectionAction,
    ) -> CallerRecord:
        caller = self._require_current_agent()
        actions = self._repository.section_actions(
            caller.library_id,
            caller.id,
            section_id,
        )
        if not actions:
            raise RetrievalNotFoundError
        if action not in actions:
            raise RetrievalAuthorizationError
        return caller

    @staticmethod
    def _map_page[StoredT, ViewT](
        stored: KeysetPage[StoredT],
        mapper: Callable[[StoredT], ViewT],
    ) -> KeysetPage[ViewT]:
        return KeysetPage(
            items=tuple(mapper(item) for item in stored.items),
            next_key=stored.next_key,
        )

    @classmethod
    def _metadata(cls, page: PageRecord) -> PageMetadata:
        view = cls._page_view(page)
        return PageMetadata(
            page=view,
            citation=cls._citation(page, page.current_revision_id, page.current_revision_number),
        )

    @classmethod
    def _document(cls, stored: StoredDocument) -> PageDocument:
        page = cls._page_view(stored.page)
        revision = cls._revision_view(stored.page, stored.revision)
        return PageDocument(
            page=page,
            revision=revision,
            citation=cls._citation(
                stored.page,
                stored.revision.revision_id,
                stored.revision.revision_number,
            ),
        )

    @staticmethod
    def _page_view(page: PageRecord) -> PageView:
        return PageView(
            section_id=page.section_id,
            book_id=page.book_id,
            page_id=page.page_id,
            title=page.title,
            type=page.page_type,
            occurred_at=canonical_utc_wire(page.occurred_at),
            current_revision_id=page.current_revision_id,
            current_revision_number=page.current_revision_number,
        )

    @staticmethod
    def _revision_view(page: PageRecord, revision: RevisionRecord) -> RevisionView:
        if not hmac.compare_digest(page.page_uid, revision.page_uid):
            raise RetrievalPersistenceError
        try:
            content = revision.content_md.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise RetrievalPersistenceError from None
        return RevisionView(
            page_id=page.page_id,
            revision_id=revision.revision_id,
            revision_number=revision.revision_number,
            created_at=canonical_utc_wire(revision.created_at),
            content_sha256=revision.content_sha256.hex(),
            content=content,
        )

    @staticmethod
    def _citation(
        page: PageRecord,
        revision_id: str,
        revision_number: int,
    ) -> Citation:
        return Citation(
            section_id=page.section_id,
            page_id=page.page_id,
            revision_id=revision_id,
            revision_number=revision_number,
            href=build_api_v1_path(
                "sections",
                page.section_id,
                "pages",
                page.page_id,
                "revisions",
                str(revision_number),
            ),
        )


__all__ = [
    "RetrievalAuthenticationError",
    "RetrievalAuthorizationError",
    "RetrievalNotFoundError",
    "RetrievalPersistenceError",
    "RetrievalService",
]
