from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import anyio
from fastapi import APIRouter, Request
from pydantic import TypeAdapter, ValidationError
from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import parse_options_header
from sqlalchemy import Connection, Engine
from starlette.responses import JSONResponse, Response

from patchouli_lib.api.authentication import (
    AuthenticatedRequestContext,
    BearerAuthentication,
    extract_bearer_token,
)
from patchouli_lib.api.contracts import (
    API_V1_PREFIX,
    PROTECTED_CACHE_CONTROL,
    PaginationParameters,
    build_api_v1_path,
)
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, SectionAction
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
    Clock,
    utc_microseconds,
)
from patchouli_lib.content import (
    AppendArchiveRevisionCommand,
    ArchiveIdempotencyKey,
    ArchiveLifecycleUnchangedError,
    ArchiveMutationReplay,
    ArchiveMutationResult,
    ArchiveNotFoundError,
    ArchiveOccurrenceUnchangedError,
    ArchivePreconditionFailedError,
    ArchivePreconditionRequiredError,
    ArchiveService,
    ArchiveSourceInput,
    CorrectArchiveOccurrenceCommand,
    CreateArchiveCommand,
    page_current_etag,
)
from patchouli_lib.content.models import MAX_MARKDOWN_BYTES
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveCitation,
    PageLifecycleCommand,
    PageLifecycleResponseBody,
    PageRecord,
    StrongPageETag,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency import IdempotencyConflictError, digest_idempotency_key
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
from patchouli_lib.identifiers import InvalidPageIdError, canonical_utc_wire, parse_occurrence_time
from patchouli_lib.library.schemas import OpaqueId
from patchouli_lib.retrieval.cursor import CursorBinding, CursorCodec, InvalidCursorError

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

MAX_ARCHIVE_METADATA_BYTES: Final = 64 * 1024
MAX_ARCHIVE_MULTIPART_BYTES: Final = MAX_MARKDOWN_BYTES + 128 * 1024
MAX_OCCURRENCE_JSON_BYTES: Final = 1_024

_CONTENT_DISPOSITION = b"content-disposition"
_CONTENT_LENGTH = b"content-length"
_CONTENT_TYPE = b"content-type"
_IDEMPOTENCY_KEY = b"idempotency-key"
_IF_MATCH = b"if-match"
_PAGE_ETAG_PATTERN = re.compile(rb'^"page-v[12]-[0-9a-f]{64}"$', re.ASCII)
_MIME_TOKEN_BYTES = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_MAX_MIME_HEADER_BYTES = 1_024
_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_PAGE_ETAG_ADAPTER = TypeAdapter(StrongPageETag)
_TRASH_CURSOR_KEY_PATTERN = re.compile(r"^[0-9]{20}:[a-z0-9-]{1,80}$", re.ASCII)

ArchiveServiceFactory = Callable[[Connection], ArchiveService]
MutationKind = Literal["create", "revise"]


class _MultipartValidationFailure(ValueError):
    pass


class _MultipartMediaFailure(ValueError):
    pass


class _PayloadTooLarge(ValueError):
    pass


def _validation_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=422,
        code="request_validation_failed",
        title="Request validation failed",
        detail="The request did not satisfy the required schema.",
    )


def _payload_too_large_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=413,
        code="content_too_large",
        title="Content too large",
        detail="The request content is too large.",
    )


def _unsupported_media_type_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=415,
        code="unsupported_media_type",
        title="Unsupported media type",
        detail="The media type is not supported.",
    )


def _precondition_required_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=428,
        code="precondition_required",
        title="Precondition required",
        detail="A required precondition is missing.",
    )


def _revision_conflict_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=412,
        code="revision_conflict",
        title="Precondition failed",
        detail="The Page has a newer current Revision.",
    )


def _occurrence_conflict_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=412,
        code="page_conflict",
        title="Precondition failed",
        detail="The Page changed since the supplied ETag.",
    )


def _idempotency_conflict_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=409,
        code="idempotency_mismatch",
        title="Idempotency conflict",
        detail="The idempotency key was already used for a different request.",
    )


def _occurrence_unchanged_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=409,
        code="occurrence_unchanged",
        title="Declared time unchanged",
        detail="The Page already has the requested declared time.",
    )


def _lifecycle_unchanged_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=409,
        code="page_state_unchanged",
        title="Page state unchanged",
        detail="The Page is already in the requested state.",
    )


def _raw_header_values(request: Request, expected_name: bytes) -> tuple[bytes, ...]:
    headers: Sequence[tuple[bytes, bytes]] = request.scope.get("headers", ())
    return tuple(value for name, value in headers if name.lower() == expected_name)


def _validate_route_id(value: str) -> str:
    try:
        return _OPAQUE_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise _validation_problem() from None


def _idempotency_key(request: Request) -> ArchiveIdempotencyKey:
    values = _raw_header_values(request, _IDEMPOTENCY_KEY)
    if len(values) != 1:
        raise _validation_problem()
    try:
        presented = values[0].decode("ascii", errors="strict")
        digest = digest_idempotency_key(presented)
    except (UnicodeDecodeError, ValueError):
        raise _validation_problem() from None
    finally:
        if "presented" in locals():
            del presented
    return ArchiveIdempotencyKey(key_digest=digest)


def _create_precondition(request: Request) -> None:
    if _raw_header_values(request, _IF_MATCH):
        raise _validation_problem()


def _revision_precondition(request: Request) -> str:
    values = _raw_header_values(request, _IF_MATCH)
    if not values:
        raise _precondition_required_problem()
    if len(values) != 1 or _PAGE_ETAG_PATTERN.fullmatch(values[0]) is None:
        raise _validation_problem()
    try:
        value = values[0].decode("ascii", errors="strict")
        return _PAGE_ETAG_ADAPTER.validate_python(value, strict=True)
    except (UnicodeDecodeError, ValidationError):
        raise _validation_problem() from None


def _content_type_boundary(request: Request) -> bytes:
    values = _raw_header_values(request, _CONTENT_TYPE)
    if len(values) != 1:
        raise _unsupported_media_type_problem()
    try:
        _require_unique_mime_parameters(values[0])
        media_type, parameters = parse_options_header(values[0])
    except (AssertionError, UnicodeError, ValueError):
        raise _unsupported_media_type_problem() from None
    normalized = {key.lower(): value for key, value in parameters.items()}
    if (
        media_type != b"multipart/form-data"
        or len(normalized) != len(parameters)
        or set(normalized) != {b"boundary"}
        or not normalized[b"boundary"]
    ):
        raise _unsupported_media_type_problem()
    boundary = normalized[b"boundary"]
    if any(value < 0x20 or value > 0x7E for value in boundary):
        raise _validation_problem()
    return boundary


def _content_length_too_large(request: Request) -> bool:
    values = _raw_header_values(request, _CONTENT_LENGTH)
    if len(values) != 1 or not values[0].isdigit():
        return False
    try:
        return int(values[0]) > MAX_ARCHIVE_MULTIPART_BYTES
    except ValueError:  # pragma: no cover - guarded ASCII digits
        return False


def _normalized_parameters(parameters: Mapping[bytes, bytes]) -> dict[bytes, bytes]:
    normalized = {key.lower(): value for key, value in parameters.items()}
    if len(normalized) != len(parameters):
        raise _MultipartMediaFailure
    return normalized


def _require_unique_mime_parameters(value: bytes) -> None:
    """Reject repeated MIME parameters without misreading quoted separators."""

    if not value or len(value) > _MAX_MIME_HEADER_BYTES:
        raise ValueError("Invalid bounded MIME header.")
    position = value.find(b";")
    if position < 0:
        return
    seen: set[bytes] = set()
    length = len(value)
    while position < length:
        if value[position] != ord(";"):
            raise ValueError("Invalid MIME parameter separator.")
        position += 1
        while position < length and value[position] in b" \t":
            position += 1
        name_start = position
        while position < length and value[position] in _MIME_TOKEN_BYTES:
            position += 1
        if position == name_start:
            raise ValueError("Invalid MIME parameter name.")
        name = value[name_start:position].lower()
        while position < length and value[position] in b" \t":
            position += 1
        if position >= length or value[position] != ord("="):
            raise ValueError("Invalid MIME parameter assignment.")
        position += 1
        while position < length and value[position] in b" \t":
            position += 1
        if position >= length:
            raise ValueError("Missing MIME parameter value.")
        if value[position] == ord('"'):
            position += 1
            while position < length:
                current = value[position]
                if current == ord("\\"):
                    if position + 1 >= length or value[position + 1] in b"\x00\r\n":
                        raise ValueError("Invalid MIME quoted escape.")
                    position += 2
                    continue
                if current == ord('"'):
                    position += 1
                    break
                if current in b"\r\n" or current < 0x20 or current == 0x7F:
                    raise ValueError("Invalid MIME quoted value.")
                position += 1
            else:
                raise ValueError("Unterminated MIME quoted value.")
        else:
            value_start = position
            while position < length and value[position] in _MIME_TOKEN_BYTES:
                position += 1
            if position == value_start:
                raise ValueError("Invalid MIME token value.")
        while position < length and value[position] in b" \t":
            position += 1
        if position < length and value[position] != ord(";"):
            raise ValueError("Invalid MIME parameter suffix.")
        if name in seen:
            raise ValueError("Duplicate MIME parameter.")
        seen.add(name)


class _MultipartCollector:
    """Bounded callback target for the reviewed python-multipart parser."""

    def __init__(self) -> None:
        self.parts: dict[bytes, bytes] = {}
        self.ended = False
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._part_name: bytes | None = None
        self._part_data = bytearray()

    def callbacks(self) -> MultipartCallbacks:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        self._header_name.clear()
        self._header_value.clear()
        self._headers.clear()
        self._part_name = None
        self._part_data.clear()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        name = bytes(self._header_name).lower()
        if not name or name in self._headers:
            raise _MultipartValidationFailure
        self._headers[name] = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        if set(self._headers) != {_CONTENT_DISPOSITION, _CONTENT_TYPE}:
            raise _MultipartValidationFailure
        try:
            _require_unique_mime_parameters(self._headers[_CONTENT_DISPOSITION])
            disposition, raw_disposition_parameters = parse_options_header(
                self._headers[_CONTENT_DISPOSITION]
            )
        except (AssertionError, UnicodeError, ValueError):
            raise _MultipartValidationFailure from None
        disposition_parameters = _normalized_parameters(raw_disposition_parameters)
        part_name = disposition_parameters.get(b"name")
        if disposition != b"form-data" or part_name not in {b"metadata", b"content"}:
            raise _MultipartValidationFailure
        if part_name in self.parts:
            raise _MultipartValidationFailure

        try:
            _require_unique_mime_parameters(self._headers[_CONTENT_TYPE])
            media_type, raw_media_parameters = parse_options_header(self._headers[_CONTENT_TYPE])
        except (AssertionError, UnicodeError, ValueError):
            raise _MultipartMediaFailure from None
        media_parameters = _normalized_parameters(raw_media_parameters)
        if b"charset" in media_parameters:
            media_parameters[b"charset"] = media_parameters[b"charset"].lower()
        charset = media_parameters.get(b"charset", b"")
        if part_name == b"metadata":
            if media_type != b"application/json" or (
                media_parameters and media_parameters != {b"charset": b"utf-8"}
            ):
                raise _MultipartMediaFailure
        elif media_type != b"text/markdown" or media_parameters != {b"charset": b"utf-8"}:
            raise _MultipartMediaFailure
        if charset not in {b"", b"utf-8"}:
            raise _MultipartMediaFailure
        self._part_name = part_name

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._part_name is None:
            raise _MultipartValidationFailure
        incoming = end - start
        limit = MAX_ARCHIVE_METADATA_BYTES if self._part_name == b"metadata" else MAX_MARKDOWN_BYTES
        if len(self._part_data) + incoming > limit:
            raise _PayloadTooLarge
        self._part_data.extend(data[start:end])

    def on_part_end(self) -> None:
        if self._part_name is None or self._part_name in self.parts:
            raise _MultipartValidationFailure
        self.parts[self._part_name] = bytes(self._part_data)

    def on_end(self) -> None:
        self.ended = True


async def _archive_parts(request: Request) -> tuple[bytes, bytes]:
    boundary = _content_type_boundary(request)
    if _content_length_too_large(request):
        raise _payload_too_large_problem()
    collector = _MultipartCollector()
    try:
        parser = MultipartParser(
            boundary,
            callbacks=collector.callbacks(),
            max_size=MAX_ARCHIVE_MULTIPART_BYTES,
            max_header_count=4,
            max_header_size=1_024,
        )
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_ARCHIVE_MULTIPART_BYTES:
                raise _PayloadTooLarge
            parser.write(chunk)
    except _PayloadTooLarge:
        raise _payload_too_large_problem() from None
    except _MultipartMediaFailure:
        raise _unsupported_media_type_problem() from None
    except (
        _MultipartValidationFailure,
        FormParserError,
        MultipartParseError,
        AssertionError,
        UnicodeError,
        ValueError,
    ):
        raise _validation_problem() from None
    if not collector.ended or set(collector.parts) != {b"metadata", b"content"}:
        raise _validation_problem()
    return collector.parts[b"metadata"], collector.parts[b"content"]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _MultipartValidationFailure
        result[key] = value
    return result


def _metadata_object(value: bytes) -> dict[str, Any]:
    if not value:
        raise _validation_problem()
    try:
        decoded = value.decode("utf-8", errors="strict")
        parsed = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(_MultipartValidationFailure()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _MultipartValidationFailure,
        RecursionError,
        ValueError,
    ):
        raise _validation_problem() from None
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise _validation_problem()
    return cast(dict[str, Any], parsed)


def _source_input(value: object) -> ArchiveSourceInput:
    if not isinstance(value, dict) or set(value) not in ({"kind"}, {"kind", "locator"}):
        raise _validation_problem()
    try:
        return ArchiveSourceInput.model_validate(value)
    except ValidationError:
        raise _validation_problem() from None


def _create_command(
    metadata_bytes: bytes,
    content: bytes,
    *,
    context: AuthenticatedRequestContext,
    section_id: str,
    book_id: str,
    request_id: str,
) -> CreateArchiveCommand:
    metadata = _metadata_object(metadata_bytes)
    if set(metadata) not in ({"title", "source"}, {"title", "occurred_at", "source"}):
        raise _validation_problem()
    try:
        occurrence = (
            parse_occurrence_time(metadata["occurred_at"]).utc_microseconds
            if "occurred_at" in metadata
            else None
        )
        return CreateArchiveCommand(
            library_id=context.authenticated.caller.library_id,
            section_id=section_id,
            book_id=book_id,
            title=metadata["title"],
            occurred_at=occurrence,
            content_md=content,
            source=_source_input(metadata["source"]),
            request_id=request_id,
        )
    except (ValidationError, ValueError, TypeError):
        raise _validation_problem() from None


def _revision_command(
    metadata_bytes: bytes,
    content: bytes,
    *,
    context: AuthenticatedRequestContext,
    section_id: str,
    page_id: str,
    expected_etag: str,
    request_id: str,
) -> AppendArchiveRevisionCommand:
    metadata = _metadata_object(metadata_bytes)
    if set(metadata) != {"source"}:
        raise _validation_problem()
    try:
        return AppendArchiveRevisionCommand(
            library_id=context.authenticated.caller.library_id,
            section_id=section_id,
            page_id=page_id,
            expected_etag=expected_etag,
            source=_source_input(metadata["source"]),
            content_md=content,
            request_id=request_id,
        )
    except (ValidationError, ValueError, TypeError):
        raise _validation_problem() from None


async def _occurrence_value(request: Request) -> int:
    values = _raw_header_values(request, _CONTENT_TYPE)
    if len(values) != 1:
        raise _unsupported_media_type_problem()
    try:
        _require_unique_mime_parameters(values[0])
        media_type, parameters = parse_options_header(values[0])
    except (AssertionError, UnicodeError, ValueError):
        raise _unsupported_media_type_problem() from None
    normalized = {key.lower(): value.lower() for key, value in parameters.items()}
    if (
        media_type != b"application/json"
        or len(normalized) != len(parameters)
        or normalized not in ({}, {b"charset": b"utf-8"})
    ):
        raise _unsupported_media_type_problem()
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_OCCURRENCE_JSON_BYTES:
            raise _payload_too_large_problem()
        chunks.extend(chunk)
    metadata = _metadata_object(bytes(chunks))
    if set(metadata) != {"occurred_at"}:
        raise _validation_problem()
    try:
        return parse_occurrence_time(metadata["occurred_at"]).utc_microseconds
    except (ValueError, TypeError):
        raise _validation_problem() from None


def _occurrence_command(
    *,
    context: AuthenticatedRequestContext,
    section_id: str,
    page_id: str,
    expected_etag: str,
    occurred_at: int,
    request_id: str,
) -> CorrectArchiveOccurrenceCommand:
    try:
        return CorrectArchiveOccurrenceCommand(
            library_id=context.authenticated.caller.library_id,
            section_id=section_id,
            page_id=page_id,
            expected_etag=expected_etag,
            occurred_at=occurred_at,
            request_id=request_id,
        )
    except (ValidationError, ValueError, TypeError):
        raise _validation_problem() from None


def _lifecycle_command(
    *,
    context: AuthenticatedRequestContext,
    section_id: str,
    page_id: str,
    expected_etag: str,
    request_id: str,
) -> PageLifecycleCommand:
    try:
        return PageLifecycleCommand(
            library_id=context.authenticated.caller.library_id,
            section_id=section_id,
            page_id=page_id,
            expected_etag=expected_etag,
            request_id=request_id,
        )
    except (ValidationError, ValueError, TypeError):
        raise _validation_problem() from None


async def _require_empty_body(request: Request) -> None:
    async for chunk in request.stream():
        if chunk:
            raise _validation_problem()


def _trash_pagination(request: Request) -> PaginationParameters:
    pairs = request.query_params.multi_items()
    if any(key not in {"limit", "cursor"} for key, _ in pairs):
        raise _validation_problem()
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise _validation_problem()
        values[key] = value
    try:
        return PaginationParameters.model_validate(values)
    except ValidationError:
        raise _validation_problem() from None


def _trash_cursor_binding(
    context: AuthenticatedRequestContext, section_id: str, limit: int
) -> CursorBinding:
    caller = context.authenticated.caller
    return CursorBinding(
        caller_id=caller.id,
        policy_version=caller.policy_version,
        section_id=section_id,
        route_identity="trash.list",
        limit=limit,
        filters_identity=b"deleted:true;archive:write",
        sort_identity=b"deleted-at:descending;page-id:ascending",
    )


def _trash_cursor_key(page: PageRecord) -> str:
    if page.deleted_at is None:
        raise ValueError("Trash cursor requires a deleted Page.")
    return f"{page.deleted_at:020d}:{page.page_id}"


def _trash_cursor_before(value: str) -> tuple[int, str]:
    if _TRASH_CURSOR_KEY_PATTERN.fullmatch(value) is None:
        raise InvalidCursorError
    timestamp, page_id = value.split(":", maxsplit=1)
    return int(timestamp), page_id


def _trash_item(page: PageRecord) -> dict[str, object]:
    if page.deleted_at is None:
        raise ValueError("Trash view requires a deleted Page.")
    citation = ArchiveCitation(
        section_id=page.section_id,
        page_id=page.page_id,
        revision_id=page.current_revision_id,
        revision_number=page.current_revision_number,
        href=build_api_v1_path(
            "sections",
            page.section_id,
            "pages",
            page.page_id,
            "revisions",
            str(page.current_revision_number),
        ),
    )
    body = PageLifecycleResponseBody(
        section_id=page.section_id,
        page_id=page.page_id,
        state="trashed",
        deleted_at=canonical_utc_wire(page.deleted_at),
        updated_at=canonical_utc_wire(page.updated_at),
        current_revision_id=page.current_revision_id,
        current_revision_number=page.current_revision_number,
        citation=citation,
    )
    return {
        **body.model_dump(mode="json"),
        "etag": page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        ),
    }


def _require_archive_access(
    context: AuthenticatedRequestContext,
    section_id: str,
) -> None:
    if context.authenticated.caller.kind is not CallerKind.AGENT:
        raise insufficient_scope()
    section_actions = {grant.action for grant in context.grants if grant.section_id == section_id}
    if not section_actions:
        raise resource_not_found()
    if SectionAction.ARCHIVE_WRITE not in section_actions:
        raise insufficient_scope()


def _require_trash_read_access(
    context: AuthenticatedRequestContext,
    section_id: str,
    *,
    detail: bool,
) -> None:
    _require_archive_access(context, section_id)
    required = SectionAction.PAGE_READ if detail else SectionAction.QUERY
    if not any(
        grant.section_id == section_id and grant.action == required for grant in context.grants
    ):
        raise insufficient_scope()


def _perform_mutation(
    engine: Engine,
    service_factory: ArchiveServiceFactory,
    token: str,
    command: CreateArchiveCommand | AppendArchiveRevisionCommand,
    idempotency: ArchiveIdempotencyKey,
    mutation: MutationKind,
    clock: Clock,
) -> ArchiveMutationResult:
    try:
        with immediate_transaction(engine) as connection:
            AuthenticationService(
                AuthRepository(connection),
                clock=clock,
            ).authorize_content(
                token,
                library_id=command.library_id,
                section_id=command.section_id,
                action=SectionAction.ARCHIVE_WRITE,
            )
            content = ContentRepository(connection)
            if mutation == "create":
                if not isinstance(command, CreateArchiveCommand):
                    raise AssertionError("Create mutation requires a create command.")
                book = content.get_book(command.library_id, command.book_id)
                if book is None or book.section_id != command.section_id:
                    raise ArchiveNotFoundError
            else:
                if not isinstance(command, AppendArchiveRevisionCommand):
                    raise AssertionError("Revision mutation requires a revision command.")
                page = content.get_page(command.library_id, command.page_id)
                if (
                    page is None
                    or page.page_type != "archive"
                    or page.section_id != command.section_id
                ):
                    raise ArchiveNotFoundError
            service = service_factory(connection)
            if mutation == "create":
                if not isinstance(command, CreateArchiveCommand):
                    raise AssertionError("Create mutation requires a create command.")
                return service.create_archive(token, command, idempotency)
            if not isinstance(command, AppendArchiveRevisionCommand):
                raise AssertionError("Revision mutation requires a revision command.")
            return service.append_revision(token, command, idempotency)
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except ArchiveNotFoundError:
        raise resource_not_found() from None
    except ArchivePreconditionRequiredError:
        raise _precondition_required_problem() from None
    except ArchivePreconditionFailedError:
        raise _revision_conflict_problem() from None
    except IdempotencyConflictError:
        raise _idempotency_conflict_problem() from None


def _perform_occurrence_correction(
    engine: Engine,
    service_factory: ArchiveServiceFactory,
    token: str,
    command: CorrectArchiveOccurrenceCommand,
    idempotency: ArchiveIdempotencyKey,
    clock: Clock,
) -> OriginalResponse | ReplayResponse:
    try:
        with immediate_transaction(engine) as connection:
            AuthenticationService(AuthRepository(connection), clock=clock).authorize_content(
                token,
                library_id=command.library_id,
                section_id=command.section_id,
                action=SectionAction.ARCHIVE_WRITE,
            )
            page = ContentRepository(connection).get_page(command.library_id, command.page_id)
            if page is None or page.page_type != "archive" or page.section_id != command.section_id:
                raise ArchiveNotFoundError
            return service_factory(connection).correct_occurrence(token, command, idempotency)
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except ArchiveNotFoundError:
        raise resource_not_found() from None
    except ArchivePreconditionFailedError:
        raise _occurrence_conflict_problem() from None
    except ArchiveOccurrenceUnchangedError:
        raise _occurrence_unchanged_problem() from None
    except IdempotencyConflictError:
        raise _idempotency_conflict_problem() from None


def _perform_lifecycle(
    engine: Engine,
    service_factory: ArchiveServiceFactory,
    token: str,
    command: PageLifecycleCommand,
    idempotency: ArchiveIdempotencyKey,
    action: Literal["delete", "restore"],
    clock: Clock,
) -> OriginalResponse | ReplayResponse:
    try:
        with immediate_transaction(engine) as connection:
            AuthenticationService(AuthRepository(connection), clock=clock).authorize_content(
                token,
                library_id=command.library_id,
                section_id=command.section_id,
                action=SectionAction.ARCHIVE_WRITE,
            )
            page = ContentRepository(connection).get_page(command.library_id, command.page_id)
            if page is None or page.page_type != "archive" or page.section_id != command.section_id:
                raise ArchiveNotFoundError
            return service_factory(connection).transition_page_lifecycle(
                token, command, idempotency, action=action
            )
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except ArchiveNotFoundError:
        raise resource_not_found() from None
    except ArchivePreconditionFailedError:
        raise _occurrence_conflict_problem() from None
    except ArchiveLifecycleUnchangedError:
        raise _lifecycle_unchanged_problem() from None
    except IdempotencyConflictError:
        raise _idempotency_conflict_problem() from None


def _read_trash(
    engine: Engine,
    token: str,
    library_id: str,
    section_id: str,
    clock: Clock,
    *,
    page_id: str | None = None,
    limit: int = 20,
    before: tuple[int, str] | None = None,
) -> PageRecord | tuple[PageRecord, ...]:
    try:
        with engine.connect() as connection, connection.begin():
            AuthenticationService(AuthRepository(connection), clock=clock).authorize_content(
                token,
                library_id=library_id,
                section_id=section_id,
                action=SectionAction.ARCHIVE_WRITE,
            )
            AuthenticationService(AuthRepository(connection), clock=clock).authorize_content(
                token,
                library_id=library_id,
                section_id=section_id,
                action=(SectionAction.PAGE_READ if page_id is not None else SectionAction.QUERY),
            )
            repository = ContentRepository(connection)
            if page_id is None:
                return repository.list_deleted_pages(
                    library_id, section_id, limit=limit, before=before
                )
            page = repository.get_page(library_id, page_id)
            if (
                page is None
                or page.page_type != "archive"
                or page.section_id != section_id
                or page.deleted_at is None
            ):
                raise ArchiveNotFoundError
            return page
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except ArchiveNotFoundError:
        raise resource_not_found() from None
    except InvalidPageIdError:
        raise _validation_problem() from None


async def _authenticate(
    authenticate: BearerAuthentication,
    request: Request,
) -> AuthenticatedRequestContext:
    return await anyio.to_thread.run_sync(
        partial(authenticate, request),
        abandon_on_cancel=False,
    )


async def _mutation_result(
    request: Request,
    *,
    engine: Engine,
    service_factory: ArchiveServiceFactory,
    command: CreateArchiveCommand | AppendArchiveRevisionCommand,
    idempotency: ArchiveIdempotencyKey,
    mutation: MutationKind,
    clock: Clock,
) -> ArchiveMutationResult:
    token = extract_bearer_token(request)

    def worker() -> ArchiveMutationResult:
        return _perform_mutation(
            engine,
            service_factory,
            token,
            command,
            idempotency,
            mutation,
            clock,
        )

    return await anyio.to_thread.run_sync(
        worker,
        abandon_on_cancel=False,
    )


def _success_response(request: Request, result: ArchiveMutationResult) -> Response:
    stored = result.response
    if stored.response_status != 201 or stored.response_location is None:
        raise RuntimeError("Archive service returned an invalid success response.")
    headers = {
        "Location": stored.response_location,
        "ETag": stored.response_etag,
        REQUEST_ID_HEADER: get_request_id(request),
        "Cache-Control": PROTECTED_CACHE_CONTROL,
    }
    if isinstance(result, ArchiveMutationReplay):
        headers["Idempotency-Replayed"] = "true"
    return Response(
        content=stored.response_body,
        status_code=stored.response_status,
        media_type=stored.response_media_type,
        headers=headers,
    )


def _metadata_success_response(
    request: Request, stored: OriginalResponse | ReplayResponse
) -> Response:
    if stored.response_status != 200 or stored.response_location is None:
        raise RuntimeError("Page metadata mutation returned an invalid success response.")
    headers = {
        "Location": stored.response_location,
        "ETag": stored.response_etag,
        REQUEST_ID_HEADER: get_request_id(request),
        "Cache-Control": PROTECTED_CACHE_CONTROL,
    }
    if isinstance(stored, ReplayResponse):
        headers["Idempotency-Replayed"] = "true"
    return Response(
        content=stored.response_body,
        status_code=200,
        media_type=stored.response_media_type,
        headers=headers,
    )


def create_archive_router(
    engine: Engine,
    *,
    clock: Clock = utc_microseconds,
    service_factory: ArchiveServiceFactory | None = None,
    cursor_codec: CursorCodec | None = None,
) -> APIRouter:
    """Create protected Archive mutation routes for an application Engine."""

    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine, clock=clock)
    resolved_service_factory = service_factory or (
        lambda connection: ArchiveService(connection, clock=clock)
    )

    @router.post("/sections/{section_id}/books/{book_id}/pages", status_code=201)
    async def create_archive(
        section_id: str,
        book_id: str,
        request: Request,
    ) -> Response:
        validated_section_id = _validate_route_id(section_id)
        validated_book_id = _validate_route_id(book_id)
        idempotency = _idempotency_key(request)
        _create_precondition(request)
        metadata, content = await _archive_parts(request)
        context = await _authenticate(authenticate, request)
        _require_archive_access(context, validated_section_id)
        command = _create_command(
            metadata,
            content,
            context=context,
            section_id=validated_section_id,
            book_id=validated_book_id,
            request_id=get_request_id(request),
        )
        result = await _mutation_result(
            request,
            engine=engine,
            service_factory=resolved_service_factory,
            command=command,
            idempotency=idempotency,
            mutation="create",
            clock=clock,
        )
        return _success_response(request, result)

    @router.post("/sections/{section_id}/pages/{page_id}/revisions", status_code=201)
    async def revise_archive(
        section_id: str,
        page_id: str,
        request: Request,
    ) -> Response:
        validated_section_id = _validate_route_id(section_id)
        idempotency = _idempotency_key(request)
        expected_etag = _revision_precondition(request)
        metadata, content = await _archive_parts(request)
        context = await _authenticate(authenticate, request)
        _require_archive_access(context, validated_section_id)
        command = _revision_command(
            metadata,
            content,
            context=context,
            section_id=validated_section_id,
            page_id=page_id,
            expected_etag=expected_etag,
            request_id=get_request_id(request),
        )
        result = await _mutation_result(
            request,
            engine=engine,
            service_factory=resolved_service_factory,
            command=command,
            idempotency=idempotency,
            mutation="revise",
            clock=clock,
        )
        return _success_response(request, result)

    @router.patch("/sections/{section_id}/pages/{page_id}/occurrence", status_code=200)
    async def correct_occurrence(
        section_id: str,
        page_id: str,
        request: Request,
    ) -> Response:
        validated_section_id = _validate_route_id(section_id)
        idempotency = _idempotency_key(request)
        expected_etag = _revision_precondition(request)
        occurred_at = await _occurrence_value(request)
        context = await _authenticate(authenticate, request)
        _require_archive_access(context, validated_section_id)
        command = _occurrence_command(
            context=context,
            section_id=validated_section_id,
            page_id=page_id,
            expected_etag=expected_etag,
            occurred_at=occurred_at,
            request_id=get_request_id(request),
        )
        token = extract_bearer_token(request)
        stored = await anyio.to_thread.run_sync(
            partial(
                _perform_occurrence_correction,
                engine,
                resolved_service_factory,
                token,
                command,
                idempotency,
                clock,
            ),
            abandon_on_cancel=False,
        )
        return _metadata_success_response(request, stored)

    async def lifecycle_action(
        action: Literal["delete", "restore"],
        section_id: str,
        page_id: str,
        request: Request,
    ) -> Response:
        validated_section_id = _validate_route_id(section_id)
        idempotency = _idempotency_key(request)
        expected_etag = _revision_precondition(request)
        await _require_empty_body(request)
        context = await _authenticate(authenticate, request)
        _require_archive_access(context, validated_section_id)
        command = _lifecycle_command(
            context=context,
            section_id=validated_section_id,
            page_id=page_id,
            expected_etag=expected_etag,
            request_id=get_request_id(request),
        )
        token = extract_bearer_token(request)
        stored = await anyio.to_thread.run_sync(
            partial(
                _perform_lifecycle,
                engine,
                resolved_service_factory,
                token,
                command,
                idempotency,
                action,
                clock,
            ),
            abandon_on_cancel=False,
        )
        return _metadata_success_response(request, stored)

    @router.delete("/sections/{section_id}/pages/{page_id}", status_code=200)
    async def delete_page(section_id: str, page_id: str, request: Request) -> Response:
        return await lifecycle_action("delete", section_id, page_id, request)

    @router.post("/sections/{section_id}/pages/{page_id}/restore", status_code=200)
    async def restore_page(section_id: str, page_id: str, request: Request) -> Response:
        return await lifecycle_action("restore", section_id, page_id, request)

    @router.get("/sections/{section_id}/trash/{page_id}")
    async def get_trashed_page(section_id: str, page_id: str, request: Request) -> JSONResponse:
        validated_section_id = _validate_route_id(section_id)
        context = await _authenticate(authenticate, request)
        _require_trash_read_access(context, validated_section_id, detail=True)
        page = await anyio.to_thread.run_sync(
            partial(
                _read_trash,
                engine,
                extract_bearer_token(request),
                context.authenticated.caller.library_id,
                validated_section_id,
                clock,
                page_id=page_id,
            ),
            abandon_on_cancel=False,
        )
        if not isinstance(page, PageRecord):
            raise RuntimeError("Trash detail read returned a collection.")
        item = _trash_item(page)
        return JSONResponse(
            content=item,
            headers={
                REQUEST_ID_HEADER: get_request_id(request),
                "Cache-Control": PROTECTED_CACHE_CONTROL,
                "ETag": cast(str, item["etag"]),
            },
        )

    if cursor_codec is not None:

        @router.get("/sections/{section_id}/trash")
        async def list_trashed_pages(section_id: str, request: Request) -> JSONResponse:
            validated_section_id = _validate_route_id(section_id)
            pagination = _trash_pagination(request)
            context = await _authenticate(authenticate, request)
            _require_trash_read_access(context, validated_section_id, detail=False)
            binding = _trash_cursor_binding(context, validated_section_id, pagination.limit)
            before: tuple[int, str] | None = None
            if pagination.cursor is not None:
                try:
                    key = cursor_codec.decode(pagination.cursor, binding=binding)
                except InvalidCursorError:
                    raise ApplicationProblem(
                        status_code=400,
                        code="invalid_cursor",
                        title="Invalid cursor",
                        detail="The pagination cursor is invalid or no longer applicable.",
                    ) from None
                try:
                    before = _trash_cursor_before(key)
                except InvalidCursorError:
                    raise ApplicationProblem(
                        status_code=400,
                        code="invalid_cursor",
                        title="Invalid cursor",
                        detail="The pagination cursor is invalid or no longer applicable.",
                    ) from None
            rows = await anyio.to_thread.run_sync(
                partial(
                    _read_trash,
                    engine,
                    extract_bearer_token(request),
                    context.authenticated.caller.library_id,
                    validated_section_id,
                    clock,
                    limit=pagination.limit + 1,
                    before=before,
                ),
                abandon_on_cancel=False,
            )
            if not isinstance(rows, tuple):
                raise RuntimeError("Trash list read returned one Page.")
            visible = rows[: pagination.limit]
            next_cursor = (
                cursor_codec.encode(binding=binding, last_key=_trash_cursor_key(visible[-1]))
                if len(rows) > pagination.limit
                else None
            )
            return JSONResponse(
                content={
                    "items": [_trash_item(page) for page in visible],
                    "next_cursor": next_cursor,
                },
                headers={
                    REQUEST_ID_HEADER: get_request_id(request),
                    "Cache-Control": PROTECTED_CACHE_CONTROL,
                },
            )

    return router


__all__ = [
    "MAX_ARCHIVE_METADATA_BYTES",
    "MAX_ARCHIVE_MULTIPART_BYTES",
    "ArchiveServiceFactory",
    "create_archive_router",
]
