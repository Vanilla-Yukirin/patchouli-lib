"""Master-only browser adapters for movement and frozen original retries."""

from __future__ import annotations

from collections.abc import Callable
from time import time
from typing import Final

from fastapi import APIRouter, Request
from pydantic import TypeAdapter
from sqlalchemy import Connection, Engine, and_, select
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from patchouli_lib.admin.file_set_routes import (
    _SECURITY_HEADERS,
    _headers,
    _metadata,
    _operation_headers,
    _require_csrf,
    _UploadFailure,
)
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.page_move_pages import PAGE_MOVE_SCRIPT, MoveDestination, page_move_page
from patchouli_lib.admin.page_move_service import (
    MasterPageMoveCommand,
    MasterPageMoveConflictError,
    MasterPageMoveNotFoundError,
    MasterPageMoveService,
)
from patchouli_lib.admin.pages import AdminLocale, login_page
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.session import AdminSession, MasterAdminSession
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.schemas import OpaqueId
from patchouli_lib.identifiers import validate_page_id
from patchouli_lib.library.models import Book, Section

_PATH: Final = "/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}/move"
_ID = TypeAdapter(OpaqueId)
_MESSAGES: Final[dict[int, tuple[str, str]]] = {
    401: ("请重新使用主 Token 登录。", "Sign in again with the master Token."),
    403: ("请求未获允许，请检查登录和确认信息。", "Check sign-in and submission safety."),
    404: ("原路径、文档或目标不可新写。", "The source Page or destination is unavailable."),
    409: ("原操作键对应其他请求，请先检查上次结果。", "The key belongs to a different request."),
    412: ("文档已更新，请检查结果后明确开始新操作。", "The Page changed. Check state first."),
    413: ("移动请求超出大小限制。", "The movement request exceeds its size limit."),
    415: ("移动请求格式不受支持。", "The movement request media type is unsupported."),
    422: (
        "请检查目标分区、书籍、确认信息和操作头。",
        "Check destination, confirmation and headers.",
    ),
    428: ("移动需要当前文档的强版本标识。", "Movement requires a current strong ETag."),
    500: ("未能完成请求，请保留原操作并检查结果。", "Keep the original operation and check state."),
}


def _message(status: int, locale: AdminLocale) -> str:
    chinese, english = _MESSAGES[status]
    return chinese if locale == "zh-CN" else english


async def _destination(request: Request) -> tuple[str, str]:
    if request.headers.get("content-type", "").partition(";")[0].strip() != "application/json":
        raise _UploadFailure(415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 512:
            raise _UploadFailure(413)
        body.extend(chunk)
    data = _metadata(bytes(body))
    if set(data) != {"target_section_id", "target_book_id", "confirm_move"}:
        raise _UploadFailure(422)
    if data["confirm_move"] != "yes":
        raise _UploadFailure(422)
    return (
        _ID.validate_python(data["target_section_id"], strict=True),
        _ID.validate_python(data["target_book_id"], strict=True),
    )


def create_master_page_move_router(
    engine: Engine,
    *,
    current_session: Callable[[Request], AdminSession | None],
    locale_for: Callable[[Request], AdminLocale],
    same_origin: Callable[[Request], bool],
    html_response: Callable[[str, AdminLocale, int, bool], HTMLResponse],
    clear_session: Callable[[Response, Request], None],
) -> APIRouter:
    """Use the existing admin admission boundary; service owns every mutation."""
    router = APIRouter(include_in_schema=False)
    service = MasterPageMoveService(engine)
    read_model = AdminReadModel(engine)

    def failure(request: Request, locale: AdminLocale, status: int) -> JSONResponse:
        response = JSONResponse(
            {"message": _message(status, locale)},
            status_code=status,
            headers=_headers(request, locale),
        )
        if status == 401:
            clear_session(response, request)
        return response

    @router.get(_PATH)
    def movement_form(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        locale = locale_for(request)
        session = current_session(request)
        if type(session) is not MasterAdminSession:
            status = 401 if session is None else 403
            response = html_response(login_page(locale=locale), locale, status, False)
            if status == 401:
                clear_session(response, request)
            return response
        try:
            for identity in (library_id, section_id, book_id):
                _ID.validate_python(identity, strict=True)
            validate_page_id(page_id)
        except ValueError:
            return html_response(
                login_page(locale=locale, message=_message(422, locale)), locale, 422, False
            )
        destinations: tuple[MoveDestination, ...] = ()

        def authorize(connection: Connection) -> bool:
            nonlocal destinations
            if session.expires_at <= int(time()) or not MasterTokenRepository(
                connection
            ).is_session_generation_current(session.identity_id, session.session_generation):
                return False
            destinations = tuple(
                MoveDestination(*row)
                for row in connection.execute(
                    select(Section.id, Section.name, Book.id, Book.name)
                    .join(
                        Book,
                        and_(Book.library_id == Section.library_id, Book.section_id == Section.id),
                    )
                    .where(Section.library_id == library_id)
                    .order_by(Section.name, Section.id, Book.name, Book.id)
                )
            )
            return True

        try:
            view = read_model.get_page_by_id(library_id, page_id, authorize=authorize)
        except AuthenticationError:
            response = html_response(login_page(locale=locale), locale, 401, False)
            clear_session(response, request)
            return response
        if view is not None and (view.section.id != section_id or view.book.id != book_id):
            view = None
        return html_response(
            page_move_page(
                session.csrf_token,
                library_id=library_id,
                section_id=section_id,
                book_id=book_id,
                page_id=page_id,
                view=view,
                destinations=destinations,
                locale=locale,
            ),
            locale,
            200 if view is not None else 404,
            True,
        )

    @router.post(_PATH)
    async def movement_submit(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        locale = locale_for(request)
        try:
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
            target_section, target_book = await _destination(request)
            command = MasterPageMoveCommand(
                library_id=library_id,
                page_id=page_id,
                source_section_id=section_id,
                source_book_id=book_id,
                target_section_id=target_section,
                target_book_id=target_book,
                expected_etag=etag,
            )
            # No live path/ETag/deletion gate may precede the original-success lookup.
            result = await run_in_threadpool(
                service.move_page, command, key, master_session=session
            )
            receipt = result.receipt
            stable = f"/admin/libraries/{receipt.library_id}/pages/{receipt.page_id}"
            return JSONResponse(
                {
                    "page_id": receipt.page_id,
                    "page_url": stable,
                    "revision_url": stable + f"/revisions/{receipt.revision_number}",
                    "revision_number": receipt.revision_number,
                    "changed": bool(receipt.changed),
                    "replayed": result.replayed,
                    "target_section_id": receipt.target_section_id,
                    "target_book_id": receipt.target_book_id,
                    "etag": receipt.response_etag,
                },
                headers={**_headers(request, locale), "ETag": receipt.response_etag},
            )
        except _UploadFailure as error:
            return failure(request, locale, error.status_code)
        except AuthenticationError:
            return failure(request, locale, 401)
        except MasterPageMoveNotFoundError:
            return failure(request, locale, 404)
        except MasterPageMoveConflictError:
            return failure(request, locale, 409)
        except FileSetPreconditionFailedError:
            return failure(request, locale, 412)
        except (TypeError, ValueError):
            return failure(request, locale, 422)
        except Exception:
            return failure(request, locale, 500)

    @router.get("/page-move.js")
    def movement_script() -> PlainTextResponse:
        return PlainTextResponse(
            PAGE_MOVE_SCRIPT, media_type="application/javascript", headers=_SECURITY_HEADERS
        )

    return router


__all__ = ["create_master_page_move_router"]
