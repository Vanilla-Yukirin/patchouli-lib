from __future__ import annotations

import hmac
from collections.abc import Callable
from ipaddress import IPv6Address
from typing import Final, cast
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from patchouli_lib.admin.contracts import (
    BootstrapInput,
    ProvisionAgentInput,
    RecoverOperatorInput,
    RevokeAgentCredentialInput,
)
from patchouli_lib.admin.pages import (
    STYLESHEET,
    AdminLocale,
    action_result_page,
    book_page,
    browser_not_found_page,
    caller_page,
    callers_page,
    credential_page,
    dashboard_page,
    guide_page,
    libraries_page,
    library_page,
    login_page,
    page_preview_page,
    section_page,
)
from patchouli_lib.admin.passwords import password_matches
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.service import AdminActionService, DeliveredCredential
from patchouli_lib.admin.session import AdminSession, AdminSessionCodec
from patchouli_lib.auth.service import AuthenticationError, AuthorizationError
from patchouli_lib.config import Settings
from patchouli_lib.library.schemas import CreateBookInput, CreateLibraryInput, CreateSectionInput
from patchouli_lib.library.service import (
    LibrarySeedConflictError,
    LibraryStructureNotFoundError,
)
from patchouli_lib.operator.service import (
    BootstrapAlreadyCompletedError,
    CredentialLifecycleError,
    OperatorRecoveryUnavailableError,
    PolicyConflictError,
    ResourceNotFoundError,
)

_SESSION_COOKIE: Final[str] = "patchouli_admin_session"
_LOCALE_COOKIE: Final[str] = "patchouli_admin_locale"
_LOCALE_COOKIE_MAX_AGE: Final[int] = 31_536_000
_MAX_FORM_BYTES: Final[int] = 16_384
_MAX_FORM_FIELDS: Final[int] = 32
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
FormValues = dict[str, str | list[str]]
Action = Callable[[FormValues], DeliveredCredential | None]


class _FormError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.safe_message = message


def create_admin_router(
    engine: Engine,
    settings: Settings,
    *,
    session_codec: AdminSessionCodec | None = None,
    action_service: AdminActionService | None = None,
) -> APIRouter:
    if not settings.admin_enabled:
        raise ValueError("Admin router requires complete admin configuration.")
    password_hash = cast(SecretStr, settings.admin_password_hash).get_secret_value()
    signing_secret = cast(
        SecretStr,
        settings.admin_session_signing_secret,
    ).get_secret_value()
    codec = session_codec or AdminSessionCodec(
        signing_secret.encode("utf-8"),
        ttl_seconds=settings.admin_session_ttl_seconds,
    )
    service = action_service or AdminActionService(engine)
    read_model = AdminReadModel(engine)
    router = APIRouter(prefix="/admin", include_in_schema=False)

    def secure_cookie(request: Request) -> bool:
        return request.scope["scheme"] == "https" or not settings.admin_allow_private_http

    def current_session(request: Request) -> AdminSession | None:
        return codec.verify(request.cookies.get(_SESSION_COOKIE, ""))

    def requested_locale(request: Request) -> AdminLocale | None:
        values = request.query_params.getlist("lang")
        if len(values) != 1:
            return None
        value = values[0]
        if value == "en":
            return "en"
        if value == "zh-CN":
            return "zh-CN"
        return None

    def locale_for(request: Request) -> AdminLocale:
        requested = requested_locale(request)
        if requested is not None:
            return requested
        remembered = request.cookies.get(_LOCALE_COOKIE)
        if remembered == "zh-CN":
            return "zh-CN"
        return "en"

    def html(
        content: str,
        *,
        locale: AdminLocale,
        status_code: int = 200,
    ) -> HTMLResponse:
        return HTMLResponse(
            content,
            status_code=status_code,
            headers={**_SECURITY_HEADERS, "Content-Language": locale},
        )

    def redirect(location: str) -> RedirectResponse:
        return RedirectResponse(
            location,
            status_code=303,
            headers=_SECURITY_HEADERS,
        )

    def remember_requested_locale(response: Response, request: Request) -> None:
        requested = requested_locale(request)
        if requested is None:
            return
        response.set_cookie(
            _LOCALE_COOKIE,
            requested,
            max_age=_LOCALE_COOKIE_MAX_AGE,
            path="/admin",
            secure=secure_cookie(request),
            httponly=True,
            samesite="strict",
        )

    def forbidden(
        request: Request,
        message: str = "Request origin was rejected.",
    ) -> HTMLResponse:
        locale = locale_for(request)
        return html(login_page(locale=locale, message=message), locale=locale, status_code=403)

    def protected_page(
        request: Request,
        render: Callable[[str, AdminLocale], str | None],
    ) -> Response:
        locale = locale_for(request)
        session = current_session(request)
        if session is None:
            redirect_response = redirect("/admin/login")
            _clear_cookie(redirect_response, secure=secure_cookie(request))
            remember_requested_locale(redirect_response, request)
            return redirect_response
        rendered = render(session.csrf_token, locale)
        page_response = html(
            rendered
            if rendered is not None
            else browser_not_found_page(session.csrf_token, locale=locale),
            locale=locale,
            status_code=200 if rendered is not None else 404,
        )
        remember_requested_locale(page_response, request)
        return page_response

    async def protected_action(
        request: Request,
        *,
        allowed_fields: frozenset[str],
        repeatable_fields: frozenset[str] = frozenset(),
        action: Action,
        success_heading: str,
        success_message: str | None = None,
    ) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."),
                locale=locale,
                status_code=401,
            )
        try:
            values = await _read_form(
                request,
                allowed_fields=allowed_fields | {"csrf_token"},
                repeatable_fields=repeatable_fields,
            )
            _require_csrf(values, session)
            result = await run_in_threadpool(action, values)
        except _FormError as exc:
            return html(
                dashboard_page(
                    session.csrf_token,
                    locale=locale,
                    message=exc.safe_message,
                ),
                locale=locale,
                status_code=exc.status_code,
            )
        except (ValidationError, ValueError):
            return html(
                dashboard_page(
                    session.csrf_token,
                    locale=locale,
                    message="Check the submitted fields and try again.",
                ),
                locale=locale,
                status_code=422,
            )
        except (AuthenticationError, AuthorizationError):
            return html(
                dashboard_page(
                    session.csrf_token,
                    locale=locale,
                    message="The operator credential was rejected.",
                ),
                locale=locale,
                status_code=403,
            )
        except ResourceNotFoundError:
            return html(
                dashboard_page(
                    session.csrf_token,
                    locale=locale,
                    message="The requested local resource was not found.",
                ),
                locale=locale,
                status_code=404,
            )
        except (
            BootstrapAlreadyCompletedError,
            CredentialLifecycleError,
            IntegrityError,
            LibrarySeedConflictError,
            OperatorRecoveryUnavailableError,
            PolicyConflictError,
        ):
            return html(
                dashboard_page(
                    session.csrf_token,
                    locale=locale,
                    message="The action conflicts with current local state.",
                ),
                locale=locale,
                status_code=409,
            )
        except Exception:
            return html(
                dashboard_page(
                    session.csrf_token,
                    locale=locale,
                    message="The action could not be completed.",
                ),
                locale=locale,
                status_code=500,
            )
        if result is None:
            return html(
                action_result_page(
                    session.csrf_token,
                    heading=success_heading,
                    message=success_message or "The action completed.",
                    locale=locale,
                ),
                locale=locale,
            )
        return html(
            credential_page(
                session.csrf_token,
                heading=success_heading,
                result=result,
                locale=locale,
            ),
            locale=locale,
        )

    def revoke_agent(values: FormValues) -> None:
        service.revoke_agent_credential(RevokeAgentCredentialInput.model_validate(values))

    async def structure_action(
        request: Request,
        *,
        allowed_fields: frozenset[str],
        action: Callable[[FormValues, bytes], str],
        render: Callable[[str, AdminLocale, str], str | None],
    ) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."),
                locale=locale,
                status_code=401,
            )
        try:
            values = await _read_form(request, allowed_fields=allowed_fields | {"csrf_token"})
            _require_csrf(values, session)
            location = await run_in_threadpool(action, values, session.audit_fingerprint())
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except LibraryStructureNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except (LibrarySeedConflictError, IntegrityError):
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(location)
        page = render(session.csrf_token, locale, message)
        return html(
            page if page is not None else browser_not_found_page(session.csrf_token, locale=locale),
            locale=locale,
            status_code=status if page is not None else 404,
        )

    @router.get("")
    def dashboard(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: dashboard_page(
                csrf, locale=locale, activities=read_model.recent_content_activity()
            ),
        )

    @router.get("/login")
    def login(request: Request) -> Response:
        locale = locale_for(request)
        if current_session(request) is not None:
            redirect_response = redirect("/admin")
            remember_requested_locale(redirect_response, request)
            return redirect_response
        page_response = html(login_page(locale=locale), locale=locale)
        remember_requested_locale(page_response, request)
        return page_response

    @router.get("/libraries")
    def libraries(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: libraries_page(csrf, read_model.list_libraries(), locale=locale),
        )

    @router.get("/agents")
    def agents(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: callers_page(csrf, read_model.list_callers(), locale=locale),
        )

    @router.post("/libraries")
    async def create_library(request: Request) -> Response:
        def action(values: FormValues, fingerprint: bytes) -> str:
            created = service.create_library(
                CreateLibraryInput.model_validate(values), session_fingerprint=fingerprint
            )
            return f"/admin/libraries/{created.id}"

        return await structure_action(
            request,
            allowed_fields=frozenset(CreateLibraryInput.model_fields),
            action=action,
            render=lambda csrf, locale, message: libraries_page(
                csrf, read_model.list_libraries(), locale=locale, message=message
            ),
        )

    @router.get("/libraries/{library_id}")
    def library_detail(request: Request, library_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_library(library_id)
            return None if view is None else library_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.post("/libraries/{library_id}/sections")
    async def create_section(request: Request, library_id: str) -> Response:
        def action(values: FormValues, fingerprint: bytes) -> str:
            created = service.create_section(
                library_id,
                CreateSectionInput.model_validate(values),
                session_fingerprint=fingerprint,
            )
            return f"/admin/libraries/{library_id}/sections/{created.id}"

        def render(csrf: str, locale: AdminLocale, message: str) -> str | None:
            view = read_model.get_library(library_id)
            return (
                None if view is None else library_page(csrf, view, locale=locale, message=message)
            )

        return await structure_action(
            request,
            allowed_fields=frozenset(CreateSectionInput.model_fields),
            action=action,
            render=render,
        )

    @router.get("/libraries/{library_id}/callers/{caller_id}")
    def caller_detail(request: Request, library_id: str, caller_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_caller(library_id, caller_id)
            return None if view is None else caller_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.get("/libraries/{library_id}/sections/{section_id}")
    def section_detail(request: Request, library_id: str, section_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_section(library_id, section_id)
            return None if view is None else section_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.post("/libraries/{library_id}/sections/{section_id}/books")
    async def create_book(request: Request, library_id: str, section_id: str) -> Response:
        def action(values: FormValues, fingerprint: bytes) -> str:
            created = service.create_book(
                library_id,
                section_id,
                CreateBookInput.model_validate(values),
                session_fingerprint=fingerprint,
            )
            return f"/admin/libraries/{library_id}/sections/{section_id}/books/{created.id}"

        def render(csrf: str, locale: AdminLocale, message: str) -> str | None:
            view = read_model.get_section(library_id, section_id)
            return (
                None if view is None else section_page(csrf, view, locale=locale, message=message)
            )

        return await structure_action(
            request,
            allowed_fields=frozenset(CreateBookInput.model_fields),
            action=action,
            render=render,
        )

    @router.get("/libraries/{library_id}/sections/{section_id}/books/{book_id}")
    def book_detail(request: Request, library_id: str, section_id: str, book_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_book(library_id, section_id, book_id)
            return None if view is None else book_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.get("/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}")
    def page_detail(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_page(library_id, section_id, book_id, page_id)
            return None if view is None else page_preview_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.get(
        "/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}"
        "/revisions/{revision_number}"
    )
    def page_revision_detail(
        request: Request,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: int,
    ) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_page(library_id, section_id, book_id, page_id, revision_number)
            return None if view is None else page_preview_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.post("/login")
    async def login_submit(request: Request) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset({"password"}),
            )
            candidate = _single(values, "password")
        except _FormError as exc:
            return html(
                login_page(locale=locale, message=exc.safe_message),
                locale=locale,
                status_code=exc.status_code,
            )
        if not await run_in_threadpool(password_matches, candidate, password_hash):
            return html(
                login_page(locale=locale, message="Invalid password."),
                locale=locale,
                status_code=401,
            )
        encoded, _ = codec.issue()
        response = redirect("/admin")
        response.set_cookie(
            _SESSION_COOKIE,
            encoded,
            max_age=settings.admin_session_ttl_seconds,
            path="/admin",
            secure=secure_cookie(request),
            httponly=True,
            samesite="strict",
        )
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."),
                locale=locale,
                status_code=401,
            )
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset({"csrf_token"}),
            )
            _require_csrf(values, session)
        except _FormError as exc:
            return html(
                login_page(locale=locale, message=exc.safe_message),
                locale=locale,
                status_code=exc.status_code,
            )
        response = redirect("/admin/login")
        _clear_cookie(response, secure=secure_cookie(request))
        return response

    @router.post("/bootstrap")
    async def bootstrap(request: Request) -> Response:
        fields = frozenset(BootstrapInput.model_fields)
        return await protected_action(
            request,
            allowed_fields=fields,
            action=lambda values: service.bootstrap(BootstrapInput.model_validate(values)),
            success_heading="Library initialized",
        )

    @router.post("/recover")
    async def recover(request: Request) -> Response:
        fields = frozenset(RecoverOperatorInput.model_fields)
        return await protected_action(
            request,
            allowed_fields=fields,
            action=lambda values: service.recover_operator(
                RecoverOperatorInput.model_validate(values)
            ),
            success_heading="Operator credential recovered",
        )

    @router.post("/agents/provision")
    async def provision(request: Request) -> Response:
        fields = frozenset(ProvisionAgentInput.model_fields)
        return await protected_action(
            request,
            allowed_fields=fields,
            repeatable_fields=frozenset({"grants"}),
            action=lambda values: service.provision_agent(
                ProvisionAgentInput.model_validate(values)
            ),
            success_heading="Agent credential created",
        )

    @router.post("/agents/revoke")
    async def revoke(request: Request) -> Response:
        fields = frozenset(RevokeAgentCredentialInput.model_fields)
        return await protected_action(
            request,
            allowed_fields=fields,
            action=revoke_agent,
            success_heading="Agent credential revoked",
            success_message="The Agent credential is no longer active.",
        )

    @router.get("/guide")
    def guide(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: guide_page(csrf, "guide", locale=locale),
        )

    @router.get("/agent")
    def agent_guide(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: guide_page(csrf, "agent", locale=locale),
        )

    @router.get("/mcp")
    def mcp_guide(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: guide_page(csrf, "mcp", locale=locale),
        )

    @router.get("/style.css")
    def stylesheet() -> Response:
        return PlainTextResponse(
            STYLESHEET,
            media_type="text/css",
            headers=_SECURITY_HEADERS,
        )

    return router


def _origin_parts(value: str) -> tuple[str, str, int] | None:
    # Reject characters urlsplit would silently strip or interpret ambiguously.
    if not value.isascii() or any(ord(char) <= 32 or char in "\\,?#\x7f" for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.netloc.endswith(":")
        ):
            return None
        host = parsed.hostname.casefold()
        if ":" in host:
            if "%" in host:
                return None
            host = str(IPv6Address(host))
        port = parsed.port
        if port == 0:
            return None
        return (
            parsed.scheme,
            host,
            port if port is not None else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


def _same_origin_submission(request: Request) -> bool:
    origins = request.headers.getlist("origin")
    hosts = request.headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1:
        return False
    origin = _origin_parts(origins[0])
    # Only the ASGI server's trusted proxy handling may determine the scheme.
    # Never use Origin, Forwarded or X-Forwarded-Host to choose the target site.
    target = _origin_parts(f"{request.scope['scheme']}://{hosts[0]}")
    return origin is not None and origin == target


async def _read_form(
    request: Request,
    *,
    allowed_fields: frozenset[str],
    repeatable_fields: frozenset[str] = frozenset(),
) -> FormValues:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/x-www-form-urlencoded":
        raise _FormError(415, "Only URL-encoded forms are accepted.")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError:
            raise _FormError(400, "The submitted form is invalid.") from None
        if content_length > _MAX_FORM_BYTES:
            raise _FormError(413, "The submitted form is too large.")
    body = bytearray()
    async for chunk in request.stream():
        _extend_form_body(body, chunk)
    try:
        decoded = body.decode("utf-8")
        pairs = parse_qsl(
            decoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=_MAX_FORM_FIELDS,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError):
        raise _FormError(400, "The submitted form is invalid.") from None

    values: FormValues = {}
    for name, value in pairs:
        if name not in allowed_fields:
            raise _FormError(422, "The submitted form contains an unknown field.")
        current = values.get(name)
        if current is None:
            values[name] = [value] if name in repeatable_fields else value
        elif name in repeatable_fields and isinstance(current, list):
            current.append(value)
        else:
            raise _FormError(422, "The submitted form contains a duplicate field.")
    return values


def _extend_form_body(body: bytearray, chunk: bytes) -> None:
    if len(body) + len(chunk) > _MAX_FORM_BYTES:
        raise _FormError(413, "The submitted form is too large.")
    body.extend(chunk)


def _require_csrf(values: FormValues, session: AdminSession) -> None:
    presented = values.pop("csrf_token", None)
    if (
        not isinstance(presented, str)
        or not presented.isascii()
        or not session.csrf_token.isascii()
        or not hmac.compare_digest(
            presented,
            session.csrf_token,
        )
    ):
        raise _FormError(403, "The form expired or failed its safety check.")


def _single(values: FormValues, name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str):
        raise _FormError(422, "A required form field is missing.")
    return value


def _clear_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        _SESSION_COOKIE,
        path="/admin",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


__all__ = ["create_admin_router"]
