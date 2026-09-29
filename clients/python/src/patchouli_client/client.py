from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from typing import NoReturn, TypeVar
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
    FileSetCreateMetadata,
    FileSetCreateResult,
    FileSetFile,
    FileSetFileSummary,
    FileSetManifest,
    FileSetRevisionMetadata,
    FileSetRevisionResult,
    MarkdownContent,
    PageDocument,
    PageMetadata,
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
from patchouli_client.multipart import build_archive_multipart, build_file_set_multipart
from patchouli_client.secrets import BearerToken, IdempotencyKey
from patchouli_client.transport import OperationKind, RandomValue, RetryPolicy, Sleep, Transport

T = TypeVar("T")
_FILE_SET_ETAG = re.compile(r'"page-v[12]-[0-9a-f]{64}"', re.ASCII)


class PatchouliClient:
    def __init__(
        self,
        base_url: str,
        *,
        allow_private_http: bool = False,
        http_transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Sleep | None = None,
        random_value: RandomValue | None = None,
    ) -> None:
        self._transport = Transport(
            base_url,
            allow_private_http=allow_private_http,
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
    ) -> ClientResponse[CursorPage[PageMetadata]]:
        response = self._transport.send(
            "GET",
            f"/api/v1/sections/{self._segment(section_id)}/pages",
            token=token,
            operation=OperationKind.READ,
            params=self._cursor_params(limit, cursor),
        )
        result = self._page_success(response, PageMetadata.from_dict)
        if any(item.page.section_id != section_id for item in result.value.items):
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

    def create_file_set(
        self,
        library_id: str,
        section_id: str,
        book_id: str,
        metadata: FileSetCreateMetadata,
        files: Sequence[FileSetFile],
        *,
        token: BearerToken,
        idempotency_key: IdempotencyKey,
    ) -> ClientResponse[FileSetCreateResult]:
        multipart = build_file_set_multipart(metadata.to_wire(), files)
        response = self._transport.send(
            "POST",
            (
                f"/api/v1/libraries/{self._segment(library_id)}/sections/{self._segment(section_id)}"
                f"/books/{self._segment(book_id)}/pages"
            ),
            token=token,
            operation=OperationKind.WRITE,
            headers={"Content-Type": multipart.media_type},
            body=multipart.body,
            replayable=True,
            idempotency_key=idempotency_key,
        )
        result = self._success(response, {201}, FileSetCreateResult.from_dict)
        if result.value.section_id != section_id or result.value.book_id != book_id:
            raise ProtocolError("file-set create response did not match the requested scope")
        self._require_file_set_etag(result.metadata.etag)
        expected_location = self._file_set_manifest_path(
            library_id, section_id, result.value.manifest.page_id, result.value.manifest.revision_id
        )
        if (
            result.metadata.location is None
            or require_canonical_api_path(
                result.metadata.location, context="file-set create Location"
            )
            != expected_location
        ):
            raise ProtocolError("file-set create Location did not identify the exact manifest")
        return result

    def revise_file_set(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        metadata: FileSetRevisionMetadata,
        files: Sequence[FileSetFile],
        *,
        token: BearerToken,
        idempotency_key: IdempotencyKey,
        if_match: str,
    ) -> ClientResponse[FileSetRevisionResult]:
        self._require_file_set_etag(if_match)
        multipart = build_file_set_multipart(metadata.to_wire(), files)
        response = self._transport.send(
            "POST",
            (
                f"/api/v1/libraries/{self._segment(library_id)}/sections/{self._segment(section_id)}"
                f"/pages/{self._segment(page_id)}/file-revisions"
            ),
            token=token,
            operation=OperationKind.WRITE,
            headers={"Content-Type": multipart.media_type, "If-Match": if_match},
            body=multipart.body,
            replayable=True,
            idempotency_key=idempotency_key,
        )
        result = self._success(response, {200}, FileSetRevisionResult.from_dict)
        if result.value.section_id != section_id or result.value.manifest.page_id != page_id:
            raise ProtocolError("file-set revision response did not match the requested Page")
        self._require_file_set_etag(result.metadata.etag)
        if result.metadata.location is not None:
            raise ProtocolError("file-set revision response unexpectedly contained Location")
        return result

    def get_current_file_set(
        self, library_id: str, section_id: str, page_id: str, *, token: BearerToken
    ) -> ClientResponse[FileSetManifest]:
        response = self._transport.send(
            "GET",
            (
                f"/api/v1/libraries/{self._segment(library_id)}/sections/{self._segment(section_id)}"
                f"/pages/{self._segment(page_id)}"
            ),
            token=token,
            operation=OperationKind.READ,
        )
        result = self._success(response, {200}, FileSetManifest.from_dict)
        if result.value.page_id != page_id:
            raise ProtocolError("current file-set response did not match the requested Page")
        self._require_file_set_etag(result.metadata.etag)
        return result

    def get_file_set_manifest(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        revision_id: str,
        *,
        token: BearerToken,
    ) -> ClientResponse[FileSetManifest]:
        response = self._transport.send(
            "GET",
            self._file_set_manifest_path(library_id, section_id, page_id, revision_id),
            token=token,
            operation=OperationKind.READ,
        )
        result = self._success(response, {200}, FileSetManifest.from_dict)
        if result.value.page_id != page_id or result.value.revision_id != revision_id:
            raise ProtocolError("exact file-set manifest did not match the requested Revision")
        return result

    def download_file_set_file(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        revision_id: str,
        file: FileSetFileSummary,
        *,
        token: BearerToken,
    ) -> ClientResponse[bytes]:
        # A summary from the exact manifest binds the returned bytes to one file.
        FileSetFileSummary.from_dict(
            {
                "filename": file.filename,
                "size_bytes": file.size_bytes,
                "content_sha256": file.content_sha256,
            }
        )
        response = self._transport.get_bounded(
            self._file_set_manifest_path(library_id, section_id, page_id, revision_id)
            + f"/{self._segment(file.filename)}",
            token=token,
            max_success_bytes=file.size_bytes,
        )
        if response.status_code >= 400:
            self._raise_problem(response)
        if response.status_code != 200:
            raise ProtocolError("file download status did not match the operation contract")
        if self._single_header(response, "Content-Type") != "application/octet-stream":
            raise ProtocolError("file download did not use application/octet-stream")
        expected_disposition = "attachment; filename=\"download\"; filename*=UTF-8''" + quote(
            file.filename, safe=""
        )
        if self._single_header(response, "Content-Disposition") != expected_disposition:
            raise ProtocolError("file download did not use the safe attachment disposition")
        if self._single_header(response, "X-Content-Type-Options") != "nosniff":
            raise ProtocolError("file download did not prohibit content sniffing")
        body = response.content
        if len(body) != file.size_bytes or hashlib.sha256(body).hexdigest() != file.content_sha256:
            raise ProtocolError("file download did not match the exact manifest")
        return ClientResponse(
            value=body,
            metadata=ResponseMetadata.from_headers(response.headers),
        )

    @staticmethod
    def _require_file_set_etag(value: str | None) -> str:
        if value is None or _FILE_SET_ETAG.fullmatch(value) is None:
            raise ProtocolError("file-set response did not contain a strong Page ETag")
        return value

    @staticmethod
    def _single_header(response: httpx.Response, name: str) -> str:
        values = response.headers.get_list(name)
        if len(values) != 1:
            raise ProtocolError(f"file download did not contain one {name} header")
        return values[0]

    def _file_set_manifest_path(
        self, library_id: str, section_id: str, page_id: str, revision_id: str
    ) -> str:
        return (
            f"/api/v1/libraries/{self._segment(library_id)}/sections/{self._segment(section_id)}"
            f"/pages/{self._segment(page_id)}/revisions/{self._segment(revision_id)}/files"
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
        if response.status_code >= 400:
            self._raise_problem(response)

        if response.status_code not in expected_statuses:
            raise ProtocolError("response status did not match the operation contract")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            raise ProtocolError("successful response was not application/json")
        metadata = ResponseMetadata.from_headers(response.headers)
        return ClientResponse(
            value=parser(response_object(self._json(response))),
            metadata=metadata,
        )

    def _raise_problem(self, response: httpx.Response) -> NoReturn:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
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

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:
            raise ProtocolError("response body was not valid JSON") from None
