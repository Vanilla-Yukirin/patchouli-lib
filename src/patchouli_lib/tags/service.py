"""Transaction-owned Tag operations with legacy Section-grant authorization."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Connection

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuditOutcome,
    AuthenticatedCaller,
    CallerKind,
    NewAuditEvent,
    SectionAction,
)
from patchouli_lib.auth.service import (
    LAST_USED_COALESCE_MICROSECONDS,
    AuthenticationService,
    utc_microseconds,
)
from patchouli_lib.identifiers import validate_page_id
from patchouli_lib.tags.repository import (
    TagCountRecord,
    TaggedPageRecord,
    TagRecord,
    TagRepository,
    normalize_tag_name,
)


class TagAuthorizationError(RuntimeError):
    pass


class TagNotFoundError(RuntimeError):
    pass


class TagValidationError(RuntimeError):
    pass


class TagService:
    """Keep authentication, authorization, Tag writes and audit in one transaction.

    Until Library read/write grants exist, Agent directory reads include only Tags
    associated with live Pages in Sections where both query and Page-read grants
    are current. Unassociated Tag definitions are only visible to an Operator.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        clock: Callable[[], int] = utc_microseconds,
        id_factory: Callable[[], str] | None = None,
        touch_last_used: bool = True,
    ) -> None:
        from patchouli_lib.auth.service import new_opaque_id

        self._repository = TagRepository(connection)
        self._auth = AuthRepository(connection)
        self._clock = clock
        self._id_factory = id_factory or new_opaque_id
        self._touch_last_used = touch_last_used

    def list_tags(
        self,
        token: str,
        *,
        library_id: str,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[TagCountRecord], int | None]:
        caller = self._caller(token, library_id)
        try:
            query_key = None if query is None else normalize_tag_name(query)[1]
        except ValueError:
            raise TagValidationError from None
        scoped_id = None if caller.caller.kind is CallerKind.OPERATOR else caller.caller.id
        items = self._repository.visible_tags(
            library_id=library_id,
            caller_id=scoped_id,
            query_key=query_key,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(items) > limit
        return items[:limit], offset + limit if has_more else None

    def list_tag_pages(
        self,
        token: str,
        *,
        library_id: str,
        tag_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[TaggedPageRecord], int | None]:
        caller = self._caller(token, library_id)
        scoped_id = None if caller.caller.kind is CallerKind.OPERATOR else caller.caller.id
        if not self._repository.visible_tags(
            library_id=library_id, caller_id=scoped_id, tag_id=tag_id, limit=1
        ):
            raise TagNotFoundError
        items = self._repository.visible_tag_pages(
            library_id=library_id,
            tag_id=tag_id,
            caller_id=scoped_id,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(items) > limit
        return items[:limit], offset + limit if has_more else None

    def create_tag(
        self, token: str, *, library_id: str, name: str, request_id: str
    ) -> tuple[TagRecord, bool]:
        caller = self._caller(token, library_id)
        if caller.caller.kind is not CallerKind.OPERATOR:
            raise TagAuthorizationError
        try:
            normalize_tag_name(name)
        except ValueError:
            raise TagValidationError from None
        previous = self._repository.find_tag(library_id=library_id, name=name)
        if previous is not None:
            return previous, False
        now = self._clock()
        created = self._repository.add_tag(
            library_id=library_id, tag_id=self._id_factory(), name=name, created_at=now
        )
        self._audit(caller, "tag.create", "tag", created.id, request_id, now)
        return created, True

    def list_page_tags(
        self,
        token: str,
        *,
        library_id: str,
        section_id: str,
        page_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[TagRecord], int | None]:
        caller = self._caller(token, library_id)
        if caller.caller.kind is CallerKind.AGENT:
            self._require_section_action(caller, section_id, SectionAction.PAGE_READ)
        page_uid = self._live_page(library_id, section_id, page_id)
        items = self._repository.list_page_tags(
            library_id=library_id, page_uid=page_uid, limit=limit + 1, offset=offset
        )
        has_more = len(items) > limit
        return items[:limit], offset + limit if has_more else None

    def set_page_tag(
        self,
        token: str,
        *,
        library_id: str,
        section_id: str,
        page_id: str,
        tag_id: str,
        attach: bool,
        request_id: str,
    ) -> bool:
        caller = self._caller(token, library_id)
        if caller.caller.kind is CallerKind.AGENT:
            self._require_section_action(caller, section_id, SectionAction.PAGE_READ)
            self._require_section_action(caller, section_id, SectionAction.ARCHIVE_WRITE)
        page_uid = self._live_page(library_id, section_id, page_id)
        if self._repository.get_tag(library_id=library_id, tag_id=tag_id) is None:
            raise TagNotFoundError
        attached = self._repository.has_page_tag(
            library_id=library_id, page_uid=page_uid, tag_id=tag_id
        )
        if attach == attached:
            return False
        now = self._clock()
        if attach:
            self._repository.attach_page(
                library_id=library_id, page_uid=page_uid, tag_id=tag_id, created_at=now
            )
        else:
            if not self._repository.detach_page(
                library_id=library_id, page_uid=page_uid, tag_id=tag_id
            ):
                raise RuntimeError("Expected Tag association disappeared inside transaction.")
        self._audit(
            caller,
            "tag.page.attach" if attach else "tag.page.detach",
            "page_tag",
            f"{page_id}:{tag_id}",
            request_id,
            now,
        )
        return True

    def _caller(self, token: str, library_id: str) -> AuthenticatedCaller:
        caller = AuthenticationService(
            self._auth,
            clock=self._clock,
            last_used_coalesce_microseconds=(
                LAST_USED_COALESCE_MICROSECONDS if self._touch_last_used else -1
            ),
        ).authenticate(token)
        if caller.caller.library_id != library_id:
            raise TagNotFoundError
        if caller.caller.kind not in {CallerKind.AGENT, CallerKind.OPERATOR}:
            raise TagAuthorizationError
        return caller

    def _require_section_action(
        self, caller: AuthenticatedCaller, section_id: str, action: SectionAction
    ) -> None:
        grant = self._auth.get_grant(caller.caller.library_id, caller.caller.id, section_id, action)
        if grant is None:
            if not any(
                item.section_id == section_id
                for item in self._auth.list_grants(caller.caller.library_id, caller.caller.id)
            ):
                raise TagNotFoundError
            raise TagAuthorizationError

    def _live_page(self, library_id: str, section_id: str, page_id: str) -> bytes:
        try:
            validate_page_id(page_id)
        except ValueError:
            raise TagValidationError from None
        page = self._repository.live_page(
            library_id=library_id, section_id=section_id, page_id=page_id
        )
        if page is None:
            raise TagNotFoundError
        return page[0]

    def _audit(
        self,
        caller: AuthenticatedCaller,
        action: str,
        resource_type: str,
        resource_id: str,
        request_id: str,
        now: int,
    ) -> None:
        self._auth.add_audit_event(
            NewAuditEvent(
                id=self._id_factory(),
                library_id=caller.caller.library_id,
                actor_caller_id=caller.caller.id,
                actor_credential_id=caller.credential.id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=AuditOutcome.SUCCEEDED,
                request_id=request_id,
                occurred_at=now,
            )
        )


__all__ = [
    "TagAuthorizationError",
    "TagNotFoundError",
    "TagService",
    "TagValidationError",
]
