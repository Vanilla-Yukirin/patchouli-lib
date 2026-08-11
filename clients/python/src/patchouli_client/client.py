from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar
from urllib.parse import quote

import httpx

from patchouli_client.errors import ProblemError, ProtocolError
from patchouli_client.headers import ClientResponse, ResponseMetadata, require_strong_etag
from patchouli_client.models import (
    DEFAULT_PAGE_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    ArchiveCreateMetadata,
    ArchiveRevisionMetadata,
    Book,
    Capabilities,
    CursorPage,
    MarkdownContent,
    Page,
    PageDocument,
    ProblemDetails,
    SearchHit,
    SearchRequest,
    Section,
    WhoAmI,
    require_canonical_api_path,
    response_cursor,
    response_items,
    response_object,
)
from patchouli_client.multipart import build_archive_multipart
from patchouli_client.secrets import BearerToken, IdempotencyKey
from patchouli_client.transport import OperationKind, RandomValue, RetryPolicy, Sleep, Transport

T = TypeVar("T")


class PatchouliClient:
    def __init__(
        self,
        base_url: str,
        *,
        http_transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Sleep | None = None,
        random_value: RandomValue | None = None,
    ) -> None:
        self._transport = Transport(
            base_url,
            http_transport=http_transport,
            retry_policy=retry_policy,
            sleep=sleep,
            random_value=random_value,
        )

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> PatchouliClient:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def capabilities(self, *, token: BearerToken) -> ClientResponse[Capabilities]:
        response = self._transport.send(
            "GET", "/api/v1/capabilities", token=token, operation=OperationKind.READ
        )
        return self._success(response, {200}, Capabilities.from_dict)

    def whoami(self, *, token: BearerToken) -> ClientResponse[WhoAmI]:
        response = self._transport.send(
            "GET", "/api/v1/auth/whoami", token=token, operation=OperationKind.READ
        )
        return self._success(response, {200}, WhoAmI.from_dict)

    def list_sections(
        self,
        *,
        token: BearerToken,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> ClientResponse[CursorPage[Section]]:
        response = self._transport.send(
            "GET",
            "/api/v1/sections",
            token=token,
            operation=OperationKind.READ,
            params=self._cursor_params(limit, cursor),
        )
        return self._page_success(response, Section.from_dict)

    def list_books(
        self,
        section_id: str,
        *,
        token: BearerToken,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> ClientResponse[CursorPage[Book]]:
        response = self._transport.send(
            "GET",
            f"/api/v1/sections/{self._segment(section_id)}/books",
            token=token,
            operation=OperationKind.READ,
            params=self._cursor_params(limit, cursor),
        )
        result = self._page_success(response, Book.from_dict)
        if any(book.section_id != section_id for book in result.value.items):
            raise ProtocolError("Book response did not match the requested Section")
        return result

    def list_pages(
        self,
        section_id: str,
        *,
        token: BearerToken,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> ClientResponse[CursorPage[Page]]:
        response = self._transport.send(
            "GET",
            f"/api/v1/sections/{self._segment(section_id)}/pages",
            token=token,
            operation=OperationKind.READ,
            params=self._cursor_params(limit, cursor),
        )
        result = self._page_success(response, Page.from_dict)
        if any(page.section_id != section_id for page in result.value.items):
            raise ProtocolError("Page response did not match the requested Section")
        return result

    def search(
        self,
        section_id: str,
        request: SearchRequest,
        *,
        token: BearerToken,
    ) -> ClientResponse[CursorPage[SearchHit]]:
        response = self._transport.send(
            "POST",
            f"/api/v1/sections/{self._segment(section_id)}/search",
            token=token,
            operation=OperationKind.READ,
            json_body=request.to_wire(),
        )
        result = self._page_success(response, SearchHit.from_dict)
        if any(hit.page.section_id != section_id for hit in result.value.items):
            raise ProtocolError("search response did not match the requested Section")
        return result

    def get_page(
        self,
        section_id: str,
        page_id: str,
        *,
        token: BearerToken,
    ) -> ClientResponse[PageDocument]:
        response = self._transport.send(
            "GET",
            f"/api/v1/sections/{self._segment(section_id)}/pages/{self._segment(page_id)}",
            token=token,
            operation=OperationKind.READ,
        )
        result = self._success(response, {200}, PageDocument.from_dict)
        if result.value.page.section_id != section_id:
            raise ProtocolError("Page response did not match the requested Section")
        result.value.require_current_revision()
        if result.metadata.etag is None:
            raise ProtocolError("current Page response did not contain an ETag")
        require_strong_etag(result.metadata.etag)
        if result.value.revision.content is None:
            raise ProtocolError("current Page response did not contain Revision content")
        return result

    def get_revision(
        self,
        section_id: str,
        page_id: str,
        revision_number: int,
        *,
        token: BearerToken,
    ) -> ClientResponse[PageDocument]:
        if revision_number < 1:
            raise ValueError("revision number must be positive")
        response = self._transport.send(
            "GET",
            (
                f"/api/v1/sections/{self._segment(section_id)}/pages/"
                f"{self._segment(page_id)}/revisions/{revision_number}"
            ),
            token=token,
            operation=OperationKind.READ,
        )
        result = self._success(response, {200}, PageDocument.from_dict)
        if result.value.page.section_id != section_id:
            raise ProtocolError("Revision response did not match the requested Section")
        if result.value.revision.revision_number != revision_number:
            raise ProtocolError("Revision response did not match the requested revision number")
        if result.value.revision.content is None:
            raise ProtocolError("exact Revision response did not contain content")
        return result

    def create_archive(
        self,
        section_id: str,
        book_id: str,
        metadata: ArchiveCreateMetadata,
        content: MarkdownContent,
        *,
        token: BearerToken,
        idempotency_key: IdempotencyKey,
    ) -> ClientResponse[PageDocument]:
        multipart = build_archive_multipart(metadata.to_wire(), content)
        response = self._transport.send(
            "POST",
            (f"/api/v1/sections/{self._segment(section_id)}/books/{self._segment(book_id)}/pages"),
            token=token,
            operation=OperationKind.WRITE,
            headers={"Content-Type": multipart.media_type},
            body=multipart.body,
            replayable=True,
            idempotency_key=idempotency_key,
        )
        return self._mutation_success(
            response,
            operation="create",
            section_id=section_id,
            book_id=book_id,
        )

    def revise_archive(
        self,
        section_id: str,
        page_id: str,
        metadata: ArchiveRevisionMetadata,
        content: MarkdownContent,
        *,
        token: BearerToken,
        idempotency_key: IdempotencyKey,
        if_match: str,
    ) -> ClientResponse[PageDocument]:
        require_strong_etag(if_match)
        multipart = build_archive_multipart(metadata.to_wire(), content)
        response = self._transport.send(
            "POST",
            (
                f"/api/v1/sections/{self._segment(section_id)}/pages/"
                f"{self._segment(page_id)}/revisions"
            ),
            token=token,
            operation=OperationKind.WRITE,
            headers={"Content-Type": multipart.media_type, "If-Match": if_match},
            body=multipart.body,
            replayable=True,
            idempotency_key=idempotency_key,
        )
        return self._mutation_success(
            response,
            operation="revise",
            section_id=section_id,
            book_id=None,
        )

    @staticmethod
    def _segment(value: str) -> str:
        if not value:
            raise ValueError("resource identifiers must not be empty")
        return quote(value, safe="")

    @staticmethod
    def _cursor_params(limit: int, cursor: str | None) -> dict[str, str | int]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("collection limit must be an integer")
        if not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"collection limit must be between 1 and {MAX_PAGE_LIMIT}")
        if cursor is not None and (
            not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH
        ):
            raise ValueError("collection cursor must be a non-empty bounded string or null")
        result: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            result["cursor"] = cursor
        return result

    def _mutation_success(
        self,
        response: httpx.Response,
        *,
        operation: str,
        section_id: str,
        book_id: str | None,
    ) -> ClientResponse[PageDocument]:
        result = self._success(response, {201}, PageDocument.from_dict)
        if result.value.page.section_id != section_id:
            raise ProtocolError("mutation response did not match the requested Section")
        if book_id is not None and result.value.page.book_id != book_id:
            raise ProtocolError("create response did not match the requested Book")
        result.value.require_current_revision()
        if result.metadata.location is None:
            raise ProtocolError("mutation response did not contain Location")
        location = require_canonical_api_path(
            result.metadata.location,
            context="mutation Location",
        )
        if operation == "create":
            expected_location = (
                f"/api/v1/sections/{quote(result.value.page.section_id, safe='')}/pages/"
                f"{quote(result.value.page.page_id, safe='')}"
            )
        elif operation == "revise":
            expected_location = result.value.citation.href
        else:  # pragma: no cover - internal caller invariant
            raise AssertionError("unknown mutation operation")
        if location != expected_location:
            raise ProtocolError("mutation Location did not identify the response resource")
        if result.metadata.etag is None:
            raise ProtocolError("mutation response did not contain an ETag")
        require_strong_etag(result.metadata.etag)
        return result

    def _page_success(
        self,
        response: httpx.Response,
        parser: Callable[[Mapping[str, object]], T],
    ) -> ClientResponse[CursorPage[T]]:
        def parse_page(data: Mapping[str, object]) -> CursorPage[T]:
            return CursorPage(
                items=tuple(parser(response_object(item)) for item in response_items(data)),
                next_cursor=response_cursor(data),
            )

        return self._success(response, {200}, parse_page)

    def _success(
        self,
        response: httpx.Response,
        expected_statuses: set[int],
        parser: Callable[[Mapping[str, object]], T],
    ) -> ClientResponse[T]:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if response.status_code >= 400:
            if content_type != "application/problem+json":
                raise ProtocolError("error response was not RFC 9457 Problem Details")
            problem = ProblemDetails.from_dict(response_object(self._json(response)))
            if problem.status != response.status_code:
                raise ProtocolError("Problem Details status did not match the HTTP status")
            metadata = ResponseMetadata.from_headers(
                response.headers, request_id_fallback=problem.request_id
            )
            if metadata.request_id != problem.request_id:
                raise ProtocolError("Problem Details request ID did not match the response header")
            raise ProblemError(problem, metadata)

        if response.status_code not in expected_statuses:
            raise ProtocolError("response status did not match the operation contract")
        if content_type != "application/json":
            raise ProtocolError("successful response was not application/json")
        metadata = ResponseMetadata.from_headers(response.headers)
        return ClientResponse(
            value=parser(response_object(self._json(response))),
            metadata=metadata,
        )

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:
            raise ProtocolError("response body was not valid JSON") from None
