"""Master-session browser adapters for complete, atomic Page file-set writes."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable, Sequence
from typing import Final, cast

from fastapi import APIRouter, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from patchouli_lib.admin.file_set_pages import FILE_SET_UPLOAD_SCRIPT, file_set_upload_page
from patchouli_lib.admin.file_set_service import (
    MasterFileSetConflictError,
    MasterFileSetNotFoundError,
    MasterFileSetResult,
    MasterFileSetService,
)
from patchouli_lib.admin.pages import AdminLocale, login_page
from patchouli_lib.admin.read_model import AdminReadModel, BookView, PageView
from patchouli_lib.admin.revision_restore_pages import (
    REVISION_RESTORE_SCRIPT,
    revision_restore_page,
)
from patchouli_lib.admin.revision_restore_service import (
    MasterRevisionRestoreCommand,
    MasterRevisionRestoreService,
)
from patchouli_lib.admin.session import AdminSession, MasterAdminSession
from patchouli_lib.api.errors import ApplicationProblem
from patchouli_lib.api.file_set_multipart import parse_file_set_multipart
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, ensure_request_id
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.file_set_write_service import (
    FileSetAppendCommand,
    FileSetWritePreconditionRequiredError,
)
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    OpaqueId,
    StrongPageETag,
)
from patchouli_lib.idempotency import digest_idempotency_key
from patchouli_lib.identifiers import MAX_REVISION_NUMBER, parse_occurrence_time, validate_page_id

_BOOK_PATH: Final = "/libraries/{library_id}/sections/{section_id}/books/{book_id}"
_PAGE_PATH: Final = _BOOK_PATH + "/pages/{page_id}"
_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_ETAG_ADAPTER = TypeAdapter(StrongPageETag)
_PAGE_ETAG_PATTERN = re.compile(rb'^"page-v[12]-[0-9a-f]{64}"$', re.ASCII)
_SECURITY_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_ERROR_MESSAGES: Final[dict[int, tuple[str, str]]] = {
    401: ("请重新使用主 Token 登录。", "Sign in again with the master Token."),
    403: (
        "请求未获允许，请检查登录状态后重试。",
        "The request is not permitted. Check your sign-in.",
    ),
    404: ("目标不存在或已删除。", "The target is absent or deleted."),
    409: (
        "原操作键对应其他内容。请检查上次结果，再明确开始新操作。",
        "The operation key belongs to different content. Check the previous result "
        "before explicitly starting a new operation.",
    ),
    412: (
        "文档已更新，请检查当前版本后开始新操作。",
        "The document changed. Check its current revision.",
    ),
    413: ("文件数量或大小超出限制。", "The upload exceeds the file count or size limit."),
    415: ("上传格式不受支持。", "The upload media type is not supported."),
    422: (
        "上传参数不正确，请检查标题、时间、文件和操作头。",
        "Check upload metadata, files and headers.",
    ),
    428: ("更新需要当前文档的版本标识。", "Updating requires the current document ETag."),
    500: (
        "服务器未能完成请求。请保留原文件和操作键，检查状态后重试。",
        "The server could not complete the request. Keep the original files and operation "
        "key; check state before retrying.",
    ),
}


class _UploadFailure(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


def _message(status_code: int, locale: AdminLocale) -> str:
    chinese, english = _ERROR_MESSAGES.get(status_code, _ERROR_MESSAGES[500])
    return chinese if locale == "zh-CN" else english


def _headers(request: Request, locale: AdminLocale) -> dict[str, str]:
    return {
        **_SECURITY_HEADERS,
        "Content-Language": locale,
        REQUEST_ID_HEADER: ensure_request_id(request),
    }


def _header_values(request: Request, expected: bytes) -> tuple[bytes, ...]:
    headers: Sequence[tuple[bytes, bytes]] = request.scope.get("headers", ())
    return tuple(value for name, value in headers if name.lower() == expected)


def _require_csrf(request: Request, session: MasterAdminSession) -> None:
    values = _header_values(request, b"x-csrf-token")
    if len(values) != 1:
        raise _UploadFailure(403)
    try:
        candidate = values[0].decode("ascii", errors="strict")
        if not session.csrf_token.isascii() or not hmac.compare_digest(
            candidate, session.csrf_token
        ):
            raise _UploadFailure(403)
    except UnicodeDecodeError:
        raise _UploadFailure(403) from None


def _operation_headers(
    request: Request, *, revise: bool
) -> tuple[ArchiveIdempotencyKey, str | None]:
    values = _header_values(request, b"idempotency-key")
    if len(values) != 1:
        raise _UploadFailure(422)
    try:
        key = ArchiveIdempotencyKey(
            key_digest=digest_idempotency_key(values[0].decode("ascii", errors="strict"))
        )
    except (UnicodeDecodeError, ValueError):
        raise _UploadFailure(422) from None
    etags = _header_values(request, b"if-match")
    if not revise:
        if etags:
            raise _UploadFailure(422)
        return key, None
    if not etags:
        raise _UploadFailure(428)
    if len(etags) != 1 or _PAGE_ETAG_PATTERN.fullmatch(etags[0]) is None:
        raise _UploadFailure(422)
    try:
        etag = _ETAG_ADAPTER.validate_python(etags[0].decode("ascii"), strict=True)
    except (UnicodeDecodeError, ValidationError):
        raise _UploadFailure(422) from None
    return key, etag


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate metadata key.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("Invalid JSON constant.")


def _metadata(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _UploadFailure(422) from None
    if type(value) is not dict:
        raise _UploadFailure(422)
    return cast(dict[str, object], value)


def _revision_number(raw: str) -> int:
    if (
        not 1 <= len(raw) <= 19
        or not raw.isascii()
        or not raw.isdecimal()
        or raw[0] == "0"
        or int(raw) > MAX_REVISION_NUMBER
    ):
        raise _UploadFailure(422)
    return int(raw)


async def _restore_confirmation(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise _UploadFailure(415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 512:
            raise _UploadFailure(413)
        body.extend(chunk)
    if _metadata(bytes(body)) != {"confirm_restore": "yes"}:
        raise _UploadFailure(422)


def _create_time(metadata: dict[str, object]) -> int | None:
    if not {"title"} <= set(metadata) <= {"title", "occurred_at"}:
        raise _UploadFailure(422)
    if type(metadata["title"]) is not str:
        raise _UploadFailure(422)
    value = metadata.get("occurred_at")
    if value is None:
        return None
    if type(value) is not str:
        raise _UploadFailure(422)
    try:
        return parse_occurrence_time(value).utc_microseconds
    except ValueError:
        raise _UploadFailure(422) from None


def _success(
    request: Request,
    result: MasterFileSetResult,
    *,
    locale: AdminLocale,
    create: bool,
    occurrence_defaulted: bool,
) -> JSONResponse:
    receipt = result.receipt
    # Read the frozen receipt, not the live Page: retries can succeed after
    # subsequent writes or deletion, without restoring or changing that Page.
    revision_url = (
        f"/admin/libraries/{receipt.library_id}/sections/{receipt.section_id}"
        f"/books/{receipt.book_id}/pages/{receipt.page_id}"
        f"/revisions/{receipt.revision_number}"
    )
    return JSONResponse(
        {
            "page_id": receipt.page_id,
            "revision_id": receipt.revision_id,
            "revision_number": receipt.revision_number,
            "changed": bool(receipt.changed),
            "replayed": result.replayed,
            "etag": receipt.response_etag,
            "snapshot_sha256": receipt.snapshot_sha256.hex(),
            "files": [
                {
                    "name": item.name,
                    "size_bytes": item.content_size_bytes,
                    "sha256": item.content_sha256.hex(),
                }
                for item in result.manifest.files
            ],
            "revision_url": revision_url,
            "warnings": ["occurrence_defaulted"] if create and occurrence_defaulted else [],
        },
        status_code=201 if create else 200,
        headers={**_headers(request, locale), "ETag": receipt.response_etag},
    )


def create_master_file_set_router(
    engine: Engine,
    *,
    current_session: Callable[[Request], AdminSession | None],
    locale_for: Callable[[Request], AdminLocale],
    same_origin: Callable[[Request], bool],
    html_response: Callable[[str, AdminLocale, int, bool], HTMLResponse],
    clear_session: Callable[[Response, Request], None],
) -> APIRouter:
    """Reuse the admin session boundary without borrowing any Agent credential."""

    router = APIRouter(include_in_schema=False)
    service = MasterFileSetService(engine)
    restore_service = MasterRevisionRestoreService(engine)
    read_model = AdminReadModel(engine)

    def failure(request: Request, locale: AdminLocale, status_code: int) -> JSONResponse:
        response = JSONResponse(
            {"message": _message(status_code, locale)},
            status_code=status_code,
            headers=_headers(request, locale),
        )
        if status_code == 401:
            clear_session(response, request)
        return response

    def upload_form(
        request: Request,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str | None,
    ) -> Response:
        locale: AdminLocale = "en"
        try:
            locale = locale_for(request)
            session = current_session(request)
            if session is None or type(session) is not MasterAdminSession:
                status_code = 401 if session is None else 403
                response = html_response(
                    login_page(locale=locale, message=_message(status_code, locale)),
                    locale,
                    status_code,
                    False,
                )
                if status_code == 401:
                    clear_session(response, request)
                return response
            book: BookView | None = None
            page: PageView | None = None
            valid_path = True
            try:
                for scoped_id in (library_id, section_id, book_id):
                    _OPAQUE_ID_ADAPTER.validate_python(scoped_id, strict=True)
                if page_id is not None:
                    validate_page_id(page_id)
            except ValueError:
                valid_path = False
            if valid_path:
                if page_id is None:
                    book = read_model.get_book_page(library_id, section_id, book_id)
                else:
                    page = read_model.get_page(library_id, section_id, book_id, page_id)
            # Keep an authenticated shell when a formerly valid target is
            # unavailable, so the tab can replay its saved operation key.
            available = book is not None if page_id is None else page is not None
            content = file_set_upload_page(
                session.csrf_token,
                library_id=library_id,
                section_id=section_id,
                book_id=book_id,
                page_id=page_id,
                book=book,
                page=page,
                locale=locale,
            )
            return html_response(content, locale, 200 if available else 404, True)
        except Exception:
            return html_response(
                login_page(locale=locale, message=_message(500, locale)), locale, 500, False
            )

    async def upload(
        request: Request,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str | None,
    ) -> Response:
        locale: AdminLocale = "en"
        try:
            locale = locale_for(request)
            if not same_origin(request):
                raise _UploadFailure(403)
            session = await run_in_threadpool(current_session, request)
            if session is None:
                raise _UploadFailure(401)
            if type(session) is not MasterAdminSession:
                raise _UploadFailure(403)
            _require_csrf(request, session)
            key, etag = _operation_headers(request, revise=page_id is not None)
            upload = await parse_file_set_multipart(request, allow_browser_metadata=True)
            metadata = _metadata(upload.metadata)
            try:
                scoped_book = _OPAQUE_ID_ADAPTER.validate_python(book_id, strict=True)
                files = tuple((item.name, item.content) for item in upload.manifest.files)
                source = ArchiveSourceInput(kind="admin_web")
                occurrence_defaulted = False
                if page_id is None:
                    occurred_at = _create_time(metadata)
                    occurrence_defaulted = occurred_at is None
                    command = FileSetCreateCommand(
                        library_id=library_id,
                        section_id=section_id,
                        book_id=scoped_book,
                        title=cast(str, metadata["title"]),
                        occurred_at=occurred_at,
                        files=files,
                        source=source,
                        request_id=ensure_request_id(request),
                    )
                else:
                    if metadata:
                        raise _UploadFailure(422)
                    revision_command = FileSetAppendCommand(
                        library_id=library_id,
                        section_id=section_id,
                        page_id=page_id,
                        expected_etag=etag,
                        files=files,
                        source=source,
                        request_id=ensure_request_id(request),
                    )
            except (ValueError, TypeError):
                raise _UploadFailure(422) from None
            # The service rechecks master generation in its own transaction.
            # It also checks a matching success receipt before live target/ETag;
            # never put a live-path query in front of this call.
            if page_id is None:
                result = await run_in_threadpool(
                    service.create_page, command, key, master_session=session
                )
            else:
                result = await run_in_threadpool(
                    service.revise_page,
                    revision_command,
                    scoped_book,
                    key,
                    master_session=session,
                )
            return _success(
                request,
                result,
                locale=locale,
                create=page_id is None,
                occurrence_defaulted=occurrence_defaulted,
            )
        except _UploadFailure as error:
            return failure(request, locale, error.status_code)
        except ApplicationProblem as error:
            status_code = error.status_code if error.status_code in (413, 415, 422) else 500
            return failure(request, locale, status_code)
        except AuthenticationError:
            return failure(request, locale, 401)
        except MasterFileSetConflictError:
            return failure(request, locale, 409)
        except MasterFileSetNotFoundError:
            return failure(request, locale, 404)
        except FileSetWritePreconditionRequiredError:
            return failure(request, locale, 428)
        except FileSetPreconditionFailedError:
            return failure(request, locale, 412)
        except Exception:
            return failure(request, locale, 500)

    @router.get(_BOOK_PATH + "/new-page")
    def create_form(request: Request, library_id: str, section_id: str, book_id: str) -> Response:
        return upload_form(request, library_id, section_id, book_id, None)

    @router.post(_BOOK_PATH + "/pages")
    async def create_page(
        request: Request, library_id: str, section_id: str, book_id: str
    ) -> Response:
        return await upload(request, library_id, section_id, book_id, None)

    @router.get(_PAGE_PATH + "/files/edit")
    def revision_form(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        return upload_form(request, library_id, section_id, book_id, page_id)

    @router.post(_PAGE_PATH + "/file-revisions")
    async def revise_page(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        return await upload(request, library_id, section_id, book_id, page_id)

    @router.get(_PAGE_PATH + "/revisions/{revision_number}/restore")
    def restore_form(
        request: Request,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: str,
    ) -> Response:
        locale: AdminLocale = "en"
        try:
            locale = locale_for(request)
            session = current_session(request)
            if session is None or type(session) is not MasterAdminSession:
                status = 401 if session is None else 403
                response = html_response(
                    login_page(locale=locale, message=_message(status, locale)),
                    locale,
                    status,
                    False,
                )
                if status == 401:
                    clear_session(response, request)
                return response
            number = _revision_number(revision_number)
            for scoped_id in (library_id, section_id, book_id):
                _OPAQUE_ID_ADAPTER.validate_python(scoped_id, strict=True)
            validate_page_id(page_id)
            view = read_model.get_page(library_id, section_id, book_id, page_id, number)
            # A protected unavailable shell permits an existing tab's original
            # success to be replayed after deletion, without allowing a new write.
            return html_response(
                revision_restore_page(
                    session.csrf_token,
                    library_id=library_id,
                    section_id=section_id,
                    book_id=book_id,
                    page_id=page_id,
                    revision_number=number,
                    view=view,
                    locale=locale,
                ),
                locale,
                200 if view is not None else 404,
                True,
            )
        except (_UploadFailure, ValueError):
            return html_response(
                login_page(locale=locale, message=_message(422, locale)), locale, 422, False
            )
        except Exception:
            return html_response(
                login_page(locale=locale, message=_message(500, locale)), locale, 500, False
            )

    @router.post(_PAGE_PATH + "/revisions/{revision_number}/restore")
    async def restore_revision(
        request: Request,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: str,
    ) -> Response:
        locale: AdminLocale = "en"
        try:
            locale = locale_for(request)
            if not same_origin(request):
                raise _UploadFailure(403)
            session = await run_in_threadpool(current_session, request)
            if session is None:
                raise _UploadFailure(401)
            if type(session) is not MasterAdminSession:
                raise _UploadFailure(403)
            _require_csrf(request, session)
            key, etag = _operation_headers(request, revise=True)
            if etag is None:
                raise _UploadFailure(428)
            number = _revision_number(revision_number)
            await _restore_confirmation(request)
            command = MasterRevisionRestoreCommand(
                library_id=library_id,
                section_id=section_id,
                book_id=book_id,
                page_id=page_id,
                source_revision_number=number,
                expected_etag=etag,
            )
            result = await run_in_threadpool(
                restore_service.restore_revision, command, key, master_session=session
            )
            return _success(
                request, result, locale=locale, create=False, occurrence_defaulted=False
            )
        except _UploadFailure as error:
            return failure(request, locale, error.status_code)
        except (ValueError, TypeError):
            return failure(request, locale, 422)
        except AuthenticationError:
            return failure(request, locale, 401)
        except MasterFileSetConflictError:
            return failure(request, locale, 409)
        except MasterFileSetNotFoundError:
            return failure(request, locale, 404)
        except FileSetPreconditionFailedError:
            return failure(request, locale, 412)
        except Exception:
            return failure(request, locale, 500)

    @router.get("/revision-restore.js")
    def restore_script() -> PlainTextResponse:
        return PlainTextResponse(
            REVISION_RESTORE_SCRIPT, media_type="application/javascript", headers=_SECURITY_HEADERS
        )

    @router.get("/file-set-upload.js")
    def upload_script() -> PlainTextResponse:
        return PlainTextResponse(
            FILE_SET_UPLOAD_SCRIPT,
            media_type="application/javascript",
            headers=_SECURITY_HEADERS,
        )

    return router


__all__ = ["create_master_file_set_router"]
