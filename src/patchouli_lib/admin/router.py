from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import IPv6Address
from typing import Final, cast
from urllib.parse import parse_qsl, quote, urlsplit
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import SecretStr, ValidationError
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from patchouli_lib.admin.contracts import (
    BootstrapInput,
    MasterCorrectOccurrenceInput,
    MasterDeletePageFormInput,
    MasterPageTagFormInput,
    MasterProvisionAgentInput,
    MasterRestoreArchiveFormInput,
    MasterRotateAgentCredentialInput,
    MasterSetAgentLibraryGrantsInput,
    MasterTagFormInput,
    MasterUpdateAgentInput,
    MasterUpdatePageTitleInput,
    PageTagFormInput,
    ProvisionAgentInput,
    RecoverOperatorInput,
    RestoreArchiveFormInput,
    RevokeAgentCredentialInput,
    TagFormInput,
)
from patchouli_lib.admin.file_download import AdminFileDownloadService
from patchouli_lib.admin.file_set_routes import create_master_file_set_router
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_setup import (
    MasterSetupAuthorizationError,
    MasterSetupService,
    MasterSetupUnavailableError,
)
from patchouli_lib.admin.master_setup_session import MasterSetupSessionCodec
from patchouli_lib.admin.master_token_store import (
    MasterTokenAlreadyInitialized,
    MasterTokenRepository,
)
from patchouli_lib.admin.page_move_routes import create_master_page_move_router
from patchouli_lib.admin.pages import (
    REVEAL_SCRIPT,
    STYLESHEET,
    AdminLocale,
    SearchFormValues,
    action_result_page,
    agent_grants_page,
    book_page,
    browser_not_found_page,
    caller_page,
    callers_page,
    credential_page,
    dashboard_page,
    guide_page,
    libraries_page,
    library_page,
    library_scope_index_page,
    localize,
    login_page,
    master_setup_page,
    operations_page,
    page_delete_error_page,
    page_preview_page,
    request_log_page,
    restore_error_page,
    search_page,
    section_page,
    tag_detail_page,
    tag_directory_page,
    trash_detail_page,
    trash_directory_page,
)
from patchouli_lib.admin.passwords import password_matches
from patchouli_lib.admin.read_model import AdminReadModel, LibraryItem, TagItem
from patchouli_lib.admin.service import (
    AdminActionService,
    AgentMetadataVersionConflictError,
    AgentNameConflictError,
    DeliveredCredential,
    GrantVersionConflictError,
    PageTitleNotFoundError,
    PageTitleVersionConflictError,
)
from patchouli_lib.admin.session import AdminSession, AdminSessionCodec, MasterAdminSession
from patchouli_lib.api.agent_skill_routes import SkillBundle
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.service import AuthenticationError, AuthorizationError, utc_microseconds
from patchouli_lib.config import Settings
from patchouli_lib.content.service import (
    ArchiveLifecycleUnchangedError,
    ArchiveNotFoundError,
    ArchivePreconditionFailedError,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.service import IdempotencyConflictError
from patchouli_lib.library.schemas import (
    CreateBookInput,
    CreateLibraryInput,
    CreateSectionInput,
    UpdateBookInput,
    UpdateLibraryInput,
    UpdateSectionInput,
)
from patchouli_lib.library.service import (
    LibrarySeedConflictError,
    LibraryStructureNotFoundError,
    LibraryStructureVersionConflictError,
)
from patchouli_lib.operator.service import (
    BootstrapAlreadyCompletedError,
    CredentialLifecycleError,
    OperatorRecoveryUnavailableError,
    PolicyConflictError,
    ResourceNotFoundError,
)
from patchouli_lib.search.index_v2 import SearchIndexUnavailableError
from patchouli_lib.search.query_v2 import (
    MAX_QUERY_BODY_BYTES,
    InvalidSearchQueryV2,
    SearchQueryV2,
    parse_query_v2_json,
)
from patchouli_lib.search.service_v2 import SearchScopeError, search_pages_for_master
from patchouli_lib.tags.service import (
    TagAuthorizationError,
    TagNotFoundError,
    TagValidationError,
)

_SESSION_COOKIE: Final[str] = "patchouli_admin_session"
_SETUP_COOKIE: Final[str] = "patchouli_master_setup_session"
_SETUP_PATH: Final[str] = "/admin/master/setup"
_SETUP_TTL: Final[int] = 300
_LOCALE_COOKIE: Final[str] = "patchouli_admin_locale"
_TAG_FLASH_COOKIE: Final[str] = "patchouli_admin_tag_result"
_LOCALE_COOKIE_MAX_AGE: Final[int] = 31_536_000
_TAG_FLASH_MAX_AGE: Final[int] = 60
_MAX_FORM_BYTES: Final[int] = 16_384
# 4,000 four-byte Unicode characters and a 200-character name can require
# over 50 KiB once percent-encoded; keep this exception on edit routes only.
_MAX_BOOK_EDIT_FORM_BYTES: Final[int] = 65_536
# Section description has the same 4,000-character Unicode bound as Book summary.
_MAX_SECTION_EDIT_FORM_BYTES: Final[int] = 65_536
_MAX_LIBRARY_EDIT_FORM_BYTES: Final[int] = 65_536
_MAX_PAGE_TITLE_EDIT_FORM_BYTES: Final[int] = 65_536
_MAX_FORM_FIELDS: Final[int] = 32
_MAX_AGENT_PROVISION_FORM_FIELDS: Final[int] = 256
_RESTORE_STALE_MESSAGE: Final[str] = (
    "The page changed since this form was opened. Reload the trash detail and try again."
)
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
    password_hash = (
        settings.admin_password_hash.get_secret_value()
        if settings.admin_password_hash is not None
        else None
    )
    signing_secret = cast(
        SecretStr,
        settings.admin_session_signing_secret,
    ).get_secret_value()
    codec = session_codec or AdminSessionCodec(
        signing_secret.encode("utf-8"),
        ttl_seconds=settings.admin_session_ttl_seconds,
    )
    setup_codec = MasterSetupSessionCodec(signing_secret.encode("utf-8"), ttl_seconds=_SETUP_TTL)
    master_setup_service = MasterSetupService(
        engine, settings, legacy_session_codec=codec, setup_session_codec=setup_codec
    )
    service = action_service or AdminActionService(engine)
    read_model = AdminReadModel(engine)
    file_download = AdminFileDownloadService(engine)
    skill_bundle = SkillBundle()
    retrieval_available = settings.retrieval_cursor_signing_secret is not None
    router = APIRouter(prefix="/admin", include_in_schema=False)

    def secure_cookie(request: Request) -> bool:
        return request.scope["scheme"] == "https" or not settings.admin_allow_private_http

    def session_for_connection(request: Request, connection: Connection) -> AdminSession | None:
        encoded = request.cookies.get(_SESSION_COOKIE, "")
        repository = MasterTokenRepository(connection)
        if repository.has_identity():
            master = codec.verify_master(encoded)
            if master is not None and repository.is_session_generation_current(
                master.identity_id, master.session_generation
            ):
                return master
            return None
        return codec.verify(encoded) if password_hash is not None else None

    def current_session(request: Request) -> AdminSession | None:
        with engine.connect() as connection:
            return session_for_connection(request, connection)

    def authenticate_master(candidate: str) -> tuple[bool, tuple[str, int] | None]:
        with engine.connect() as connection:
            repository = MasterTokenRepository(connection)
            if not repository.has_identity():
                return False, None
            state = repository.authenticate(candidate)
        if state is None:
            return True, None
        return True, (state.identity_id, state.session_generation)

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

    def revision_history_before(request: Request) -> int | None:
        values = request.query_params.getlist("before_revision_number")
        if not values:
            return None
        if len(values) != 1 or not 1 <= len(values[0]) <= 19:
            raise ValueError("Invalid revision history cursor")
        value = values[0]
        if not value.isascii() or not value.isdecimal() or value[0] == "0":
            raise ValueError("Invalid revision history cursor")
        before = int(value)
        if not 2 <= before <= 1 << 63:
            raise ValueError("Invalid revision history cursor")
        return before

    def activity_before(request: Request) -> str | None:
        values = request.query_params.getlist("before")
        if len(values) > 1:
            raise ValueError("Invalid activity cursor")
        return values[0] if values else None

    def html(
        content: str,
        *,
        locale: AdminLocale,
        status_code: int = 200,
        allow_self_script: bool = False,
    ) -> HTMLResponse:
        headers = {**_SECURITY_HEADERS, "Content-Language": locale}
        if allow_self_script:
            headers["Content-Security-Policy"] += "; script-src 'self'; connect-src 'self'"
        return HTMLResponse(
            content,
            status_code=status_code,
            headers=headers,
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

    def master_write_forbidden(request: Request, session: MasterAdminSession) -> HTMLResponse:
        locale = locale_for(request)
        message = (
            "此操作尚不支持主 Token 会话。"
            if locale == "zh-CN"
            else "This action is not available to Master Token sessions yet."
        )
        return html(
            operations_page(session.csrf_token, locale=locale, message=message, master_mode=True),
            locale=locale,
            status_code=403,
        )

    def protected_page(
        request: Request,
        render: Callable[[str, AdminLocale], str | None],
        *,
        allow_self_script: bool = False,
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
            allow_self_script=allow_self_script,
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
            if isinstance(session, MasterAdminSession):
                return master_write_forbidden(request, session)
            result = await run_in_threadpool(action, values)
        except _FormError as exc:
            return html(
                operations_page(
                    session.csrf_token,
                    locale=locale,
                    message=exc.safe_message,
                ),
                locale=locale,
                status_code=exc.status_code,
            )
        except (ValidationError, ValueError):
            return html(
                operations_page(
                    session.csrf_token,
                    locale=locale,
                    message="Check the submitted fields and try again.",
                ),
                locale=locale,
                status_code=422,
            )
        except (AuthenticationError, AuthorizationError):
            return html(
                operations_page(
                    session.csrf_token,
                    locale=locale,
                    message="The operator credential was rejected.",
                ),
                locale=locale,
                status_code=403,
            )
        except ResourceNotFoundError:
            return html(
                operations_page(
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
                operations_page(
                    session.csrf_token,
                    locale=locale,
                    message="The action conflicts with current local state.",
                ),
                locale=locale,
                status_code=409,
            )
        except Exception:
            return html(
                operations_page(
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
        action: Callable[[FormValues, AdminSession], str],
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
            location = await run_in_threadpool(action, values, session)
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
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

    async def tag_action(
        request: Request,
        *,
        legacy_fields: frozenset[str],
        master_fields: frozenset[str],
        action: Callable[[FormValues, AdminSession], tuple[str, str]],
        render: Callable[[str, AdminLocale, str | None, bool, AdminSession], str | None],
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
            fields = master_fields if isinstance(session, MasterAdminSession) else legacy_fields
            values = await _read_form(request, allowed_fields=fields | {"csrf_token"})
            _require_csrf(values, session)
            location, result = await run_in_threadpool(action, values, session)
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError, TagValidationError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = (
                (401, "Sign in again.")
                if isinstance(session, MasterAdminSession)
                else (403, "The operator credential was rejected.")
            )
        except (AuthorizationError, TagAuthorizationError):
            status, message = 403, "The operator credential was rejected."
        except TagNotFoundError:
            status, message = 404, "The requested Tag or page was not found."
        except IntegrityError:
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            response = redirect(location)
            payload = json.dumps(
                {"path": location, "result": result, "at": int(time.time())},
                separators=(",", ":"),
            ).encode("utf-8")
            signature = hmac.new(
                signing_secret.encode("utf-8"),
                b"patchouli-lib/admin-tag-result/v1\x00" + session.audit_fingerprint() + payload,
                hashlib.sha256,
            ).hexdigest()
            encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
            response.set_cookie(
                _TAG_FLASH_COOKIE,
                f"{encoded}.{signature}",
                max_age=_TAG_FLASH_MAX_AGE,
                path="/admin",
                secure=secure_cookie(request),
                httponly=True,
                samesite="strict",
            )
            return response
        if current_session(request) is None:
            login_response = html(
                login_page(locale=locale, message="Sign in again."),
                locale=locale,
                status_code=401,
            )
            _clear_cookie(login_response, secure=secure_cookie(request))
            return login_response
        page = render(session.csrf_token, locale, message, True, session)
        return html(
            page if page is not None else browser_not_found_page(session.csrf_token, locale=locale),
            locale=locale,
            status_code=status if page is not None else 404,
        )

    def result_message(request: Request, mapping: dict[str, str]) -> str | None:
        session = current_session(request)
        raw = request.cookies.get(_TAG_FLASH_COOKIE, "")
        if session is None or len(raw) > 2048 or raw.count(".") != 1:
            return None
        encoded, supplied_signature = raw.split(".", 1)
        try:
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            expected_signature = hmac.new(
                signing_secret.encode("utf-8"),
                b"patchouli-lib/admin-tag-result/v1\x00" + session.audit_fingerprint() + payload,
                hashlib.sha256,
            ).hexdigest()
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return None
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        now = int(time.time())
        if (
            type(data) is not dict
            or set(data) != {"path", "result", "at"}
            or data["path"] != request.url.path
            or type(data["result"]) is not str
            or type(data["at"]) is not int
            or not 0 <= now - data["at"] <= _TAG_FLASH_MAX_AGE
        ):
            return None
        return mapping.get(data["result"])

    def consume_tag_result(request: Request, response: Response) -> Response:
        if _TAG_FLASH_COOKIE in request.cookies:
            response.delete_cookie(_TAG_FLASH_COOKIE, path="/admin")
        return response

    @router.get("")
    def dashboard(request: Request) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            try:
                before = activity_before(request)
                activity = read_model.content_activity_page(before=before)
            except ValueError:
                return None
            return dashboard_page(
                csrf,
                locale=locale,
                activities=activity.items,
                activity_next_cursor=activity.next_cursor,
                activity_before_cursor=before,
                show_request_logs=isinstance(current_session(request), MasterAdminSession),
            )

        return protected_page(request, render)

    def request_log_view(request: Request, *, actor: tuple[str, str] | None = None) -> Response:
        locale = locale_for(request)
        session = current_session(request)
        if session is None:
            response = redirect("/admin/login")
            _clear_cookie(response, secure=secure_cookie(request))
            remember_requested_locale(response, request)
            return response
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            before = activity_before(request)
            if actor is not None and read_model.get_caller(*actor) is None:
                raise ValueError("Unknown identity")
            result = read_model.request_log_page(before=before, actor=actor)
        except ValueError:
            return html(
                browser_not_found_page(session.csrf_token, locale=locale),
                locale=locale,
                status_code=404,
            )
        page_response = html(
            request_log_page(
                session.csrf_token,
                result.items,
                locale=locale,
                next_cursor=result.next_cursor,
                before_cursor=before,
                actor=actor,
            ),
            locale=locale,
        )
        remember_requested_locale(page_response, request)
        return page_response

    @router.get("/requests")
    def api_requests(request: Request) -> Response:
        return request_log_view(request)

    @router.get("/libraries/{library_id}/callers/{caller_id}/requests")
    def caller_api_requests(request: Request, library_id: str, caller_id: str) -> Response:
        return request_log_view(request, actor=(library_id, caller_id))

    @router.get("/setup")
    def setup(request: Request) -> Response:
        locale = locale_for(request)
        session = current_session(request)
        if session is None:
            redirect_response = redirect("/admin/login")
            _clear_cookie(redirect_response, secure=secure_cookie(request))
            remember_requested_locale(redirect_response, request)
            return redirect_response
        page_response = html(
            operations_page(
                session.csrf_token,
                locale=locale,
                master_mode=isinstance(session, MasterAdminSession),
            ),
            locale=locale,
        )
        remember_requested_locale(page_response, request)
        return page_response

    @router.get("/login")
    def login(request: Request) -> Response:
        locale = locale_for(request)
        if current_session(request) is not None:
            redirect_response = redirect("/admin")
            remember_requested_locale(redirect_response, request)
            return redirect_response
        with engine.connect() as connection:
            has_master = MasterTokenRepository(connection).has_identity()
        if not has_master and password_hash is None and settings.admin_setup_token is not None:
            response = redirect(_SETUP_PATH)
            remember_requested_locale(response, request)
            return response
        page_response = html(login_page(locale=locale, master_mode=has_master), locale=locale)
        remember_requested_locale(page_response, request)
        return page_response

    def setup_error(request: Request, status: int, message: str) -> Response:
        locale = locale_for(request)
        legacy = current_session(request)
        form_session = setup_codec.verify(request.cookies.get(_SETUP_COOKIE, ""))
        with engine.connect() as connection:
            has_master = MasterTokenRepository(connection).has_identity()
        csrf = None
        if not has_master:
            if legacy is not None:
                csrf = legacy.csrf_token
            elif form_session is not None and settings.admin_setup_token is not None:
                csrf = form_session.csrf_token
        return html(
            master_setup_page(
                csrf,
                require_proof=legacy is None,
                locale=locale,
                message=message,
            ),
            locale=locale,
            status_code=status,
        )

    @router.get("/master/setup")
    def master_setup_form(request: Request) -> Response:
        response: Response
        locale = locale_for(request)
        with engine.connect() as connection:
            if MasterTokenRepository(connection).has_identity():
                return redirect("/admin/login")
        legacy = current_session(request)
        if legacy is None and settings.admin_setup_token is None:
            if password_hash is not None:
                response = redirect("/admin/login")
                remember_requested_locale(response, request)
                return response
            return setup_error(
                request,
                403,
                "网页首次设置未启用。请在服务器本机设置主 Token，或由部署者配置一次性设置凭据。"
                if locale == "zh-CN"
                else "Web setup is disabled. Initialize the master Token locally, or ask the "
                "operator to configure a one-time setup proof.",
            )
        encoded, form_session = setup_codec.issue()
        csrf = legacy.csrf_token if legacy is not None else form_session.csrf_token
        response = html(
            master_setup_page(csrf, require_proof=legacy is None, locale=locale), locale=locale
        )
        if legacy is None:
            response.set_cookie(
                _SETUP_COOKIE,
                encoded,
                max_age=_SETUP_TTL,
                path=_SETUP_PATH,
                secure=secure_cookie(request),
                httponly=True,
                samesite="strict",
            )
        remember_requested_locale(response, request)
        return response

    @router.post("/master/setup")
    async def master_setup_submit(request: Request) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        legacy = current_session(request)
        if isinstance(legacy, MasterAdminSession):
            return setup_error(
                request,
                409,
                "主 Token 已设置，不能再次初始化。"
                if locale == "zh-CN"
                else "The master Token is already set up and cannot be initialized again.",
            )
        form_session = setup_codec.verify(request.cookies.get(_SETUP_COOKIE, ""))
        csrf_session = legacy if legacy is not None else form_session
        if csrf_session is None:
            return setup_error(
                request,
                401,
                "设置表单已过期，请重新打开。"
                if locale == "zh-CN"
                else "The setup form expired. Open it again.",
            )
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(
                    {"csrf_token", "master_token", "confirmation", "setup_proof"}
                ),
            )
            presented = _single(values, "csrf_token")
            if not presented.isascii() or not hmac.compare_digest(
                presented, csrf_session.csrf_token
            ):
                raise _FormError(403, "The form expired or failed its safety check.")
            state = await run_in_threadpool(
                master_setup_service.initialize,
                _single(values, "master_token"),
                _single(values, "confirmation"),
                legacy_cookie=request.cookies.get(_SESSION_COOKIE) if legacy is not None else None,
                setup_proof=_single(values, "setup_proof") if legacy is None else None,
                setup_cookie=request.cookies.get(_SETUP_COOKIE) if legacy is None else None,
            )
        except _FormError as exc:
            return setup_error(request, exc.status_code, localize(locale, exc.safe_message))
        except MasterTokenAlreadyInitialized:
            return setup_error(
                request,
                409,
                "主 Token 已设置，不能再次初始化。"
                if locale == "zh-CN"
                else "The master Token is already set up and cannot be initialized again.",
            )
        except (MasterSetupAuthorizationError, MasterSetupUnavailableError):
            return setup_error(
                request,
                403,
                "设置凭据不正确或此设置方式未启用。"
                if locale == "zh-CN"
                else "The setup proof is invalid or this setup method is not enabled.",
            )
        except ValueError:
            return setup_error(
                request,
                422,
                "主 Token 必须为 32 至 1024 字节，两次输入必须一致。"
                if locale == "zh-CN"
                else "The master Token must contain 32 to 1024 bytes and both entries must match.",
            )
        encoded, _ = codec.issue_master(state.identity_id, state.session_generation)
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
        response.delete_cookie(
            _SETUP_COOKIE,
            path=_SETUP_PATH,
            secure=secure_cookie(request),
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/libraries")
    def libraries(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: libraries_page(csrf, read_model.list_libraries(), locale=locale),
        )

    def search_filters() -> tuple[tuple[LibraryItem, tuple[TagItem, ...]], ...]:
        libraries = read_model.list_libraries()
        return tuple(
            (library, view.tags if (view := read_model.list_library_tags(library.id)) else ())
            for library in libraries
        )

    @router.get("/search")
    def search_form(request: Request) -> Response:
        locale = locale_for(request)
        session = current_session(request)
        if session is None:
            response = redirect("/admin/login")
            _clear_cookie(response, secure=secure_cookie(request))
            remember_requested_locale(response, request)
            return response
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "Only the Master Token session can search here.")
        page_response = html(
            search_page(session.csrf_token, search_filters(), locale=locale), locale=locale
        )
        remember_requested_locale(page_response, request)
        return page_response

    @router.post("/search")
    async def search_submit(request: Request) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            response = html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "Only the Master Token session can search here.")
        form_values = SearchFormValues()
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(
                    {
                        "csrf_token",
                        "keywords",
                        "library_id",
                        "tags",
                        "occurred_from",
                        "occurred_before",
                    }
                ),
                repeatable_fields=frozenset({"tags", "library_id"}),
                max_fields=516,
                max_bytes=MAX_QUERY_BODY_BYTES,
            )
            _require_csrf(values, session)
            form_values = _search_form_values(values)
            query = _search_query_from_form(form_values)
            result = await run_in_threadpool(
                search_pages_for_master,
                engine,
                request.cookies.get(_SESSION_COOKIE, ""),
                codec,
                query,
            )
        except _FormError as exc:
            message, status = exc.safe_message, exc.status_code
        except InvalidSearchQueryV2:
            message = "The search form is invalid. Check the selected fields and UTC times."
            status = 422
        except SearchScopeError:
            message, status = "The selected search scope is unavailable.", 422
        except SearchIndexUnavailableError:
            message, status = "Search index is not ready. Rebuild it before searching.", 503
        except AuthenticationError:
            response = html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        else:
            filters = await run_in_threadpool(search_filters)
            response = html(
                search_page(
                    session.csrf_token,
                    filters,
                    locale=locale,
                    results=result.items,
                    form_values=form_values,
                ),
                locale=locale,
            )
            remember_requested_locale(response, request)
            return response
        filters = await run_in_threadpool(search_filters)
        response = html(
            search_page(
                session.csrf_token,
                filters,
                locale=locale,
                message=message,
                form_values=form_values,
            ),
            locale=locale,
            status_code=status,
        )
        remember_requested_locale(response, request)
        return response

    @router.get("/tags")
    def tags_index(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: library_scope_index_page(
                csrf, read_model.list_libraries(), section="tags", locale=locale
            ),
        )

    @router.get("/trash")
    def trash_index(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: library_scope_index_page(
                csrf, read_model.list_libraries(), section="trash", locale=locale
            ),
        )

    @router.get("/agents")
    def agents(request: Request) -> Response:
        locale = locale_for(request)
        session = current_session(request)
        if session is None:
            redirect_response = redirect("/admin/login")
            _clear_cookie(redirect_response, secure=secure_cookie(request))
            remember_requested_locale(redirect_response, request)
            return redirect_response
        page_response = html(
            callers_page(
                session.csrf_token,
                read_model.list_callers(),
                locale=locale,
                libraries=read_model.list_libraries(),
                allow_master_actions=isinstance(session, MasterAdminSession),
            ),
            locale=locale,
        )
        remember_requested_locale(page_response, request)
        return page_response

    @router.post("/agents/create")
    async def create_agent_as_master(request: Request) -> Response:
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
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(MasterProvisionAgentInput.model_fields) | {"csrf_token"},
                repeatable_fields=frozenset({"grants"}),
                max_fields=_MAX_AGENT_PROVISION_FORM_FIELDS,
            )
            _require_csrf(values, session)
            raw_grants = values.pop("grants", [])
            if not isinstance(raw_grants, list):
                raise _FormError(422, "Check the submitted fields and try again.")
            payload: dict[str, object] = dict(values)
            payload["grants"] = [
                {"library_id": item.partition(":")[0], "action": item.partition(":")[2]}
                for item in raw_grants
            ]
            result = await run_in_threadpool(
                service.provision_agent_as_master,
                MasterProvisionAgentInput.model_validate(payload),
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except ResourceNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except IntegrityError:
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return html(
                credential_page(
                    session.csrf_token,
                    heading="Agent credential created",
                    result=result,
                    locale=locale,
                    recoverable=True,
                ),
                locale=locale,
            )
        if status == 401:
            return html(login_page(locale=locale, message=message), locale=locale, status_code=401)
        return html(
            callers_page(
                session.csrf_token,
                read_model.list_callers(),
                locale=locale,
                libraries=read_model.list_libraries(),
                allow_master_actions=True,
                message=message,
            ),
            locale=locale,
            status_code=status,
        )

    @router.post("/libraries")
    async def create_library(request: Request) -> Response:
        def action(values: FormValues, session: AdminSession) -> str:
            created = service.create_library(
                CreateLibraryInput.model_validate(values),
                session_fingerprint=session.audit_fingerprint(),
                master_session=session if isinstance(session, MasterAdminSession) else None,
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
            return (
                None
                if view is None
                else library_page(
                    csrf,
                    view,
                    locale=locale,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                )
            )

        return protected_page(request, render)

    @router.post("/libraries/{library_id}")
    async def update_library(request: Request, library_id: str) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(UpdateLibraryInput.model_fields) | {"csrf_token"},
                max_bytes=_MAX_LIBRARY_EDIT_FORM_BYTES,
            )
            _require_csrf(values, session)
            submitted = UpdateLibraryInput.model_validate(values)
            await run_in_threadpool(
                service.update_library_as_master,
                library_id,
                submitted,
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except LibraryStructureNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except LibraryStructureVersionConflictError:
            status, message = (
                409,
                "The Library changed since this form was opened. Reload and try again.",
            )
        except (LibrarySeedConflictError, IntegrityError):
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(f"/admin/libraries/{library_id}")
        view = read_model.get_library(library_id)
        if status == 401:
            response = html(
                login_page(locale=locale, message=message), locale=locale, status_code=status
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        return html(
            browser_not_found_page(session.csrf_token, locale=locale)
            if view is None
            else library_page(
                session.csrf_token, view, locale=locale, master_mode=True, message=message
            ),
            locale=locale,
            status_code=404 if view is None else status,
        )

    @router.get("/libraries/{library_id}/trash")
    def library_trash(request: Request, library_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            values = request.query_params.getlist("before")
            if len(values) > 1:
                return None
            try:
                view = read_model.list_trash(library_id, before=values[0] if values else None)
            except ValueError:
                return None
            return None if view is None else trash_directory_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.get("/libraries/{library_id}/tags")
    def library_tags(request: Request, library_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.list_library_tags(library_id)
            return (
                None
                if view is None
                else tag_directory_page(
                    csrf,
                    view,
                    locale=locale,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                )
            )

        return protected_page(request, render)

    @router.post("/libraries/{library_id}/tags")
    async def create_library_tag(request: Request, library_id: str) -> Response:
        def action(values: FormValues, session: AdminSession) -> tuple[str, str]:
            if isinstance(session, MasterAdminSession):
                tag_id, created = service.create_tag_as_master(
                    library_id,
                    MasterTagFormInput.model_validate(values),
                    master_session=session,
                )
            else:
                tag_id, created = service.create_tag(
                    library_id, TagFormInput.model_validate(values)
                )
            return (
                f"/admin/libraries/{library_id}/tags/{tag_id}",
                "created" if created else "existing",
            )

        def render(
            csrf: str, locale: AdminLocale, message: str | None, error: bool, session: AdminSession
        ) -> str | None:
            view = read_model.list_library_tags(library_id)
            return (
                None
                if view is None
                else tag_directory_page(
                    csrf,
                    view,
                    locale=locale,
                    message=message,
                    error=error,
                    master_mode=isinstance(session, MasterAdminSession),
                )
            )

        return await tag_action(
            request,
            legacy_fields=frozenset(TagFormInput.model_fields),
            master_fields=frozenset(MasterTagFormInput.model_fields),
            action=action,
            render=render,
        )

    @router.get("/libraries/{library_id}/tags/{tag_id}")
    def library_tag_detail(request: Request, library_id: str, tag_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_library_tag(library_id, tag_id)
            return (
                None
                if view is None
                else tag_detail_page(
                    csrf,
                    view,
                    locale=locale,
                    message=result_message(
                        request,
                        {
                            "created": "Tag created.",
                            "existing": "Tag already exists; nothing changed.",
                        },
                    ),
                )
            )

        return consume_tag_result(request, protected_page(request, render))

    @router.post("/libraries/{library_id}/sections")
    async def create_section(request: Request, library_id: str) -> Response:
        def action(values: FormValues, session: AdminSession) -> str:
            created = service.create_section(
                library_id,
                CreateSectionInput.model_validate(values),
                session_fingerprint=session.audit_fingerprint(),
                master_session=session if isinstance(session, MasterAdminSession) else None,
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
            if view is None:
                return None
            try:
                before = activity_before(request)
                activity = read_model.content_activity_page(
                    before=before, actor=(view.library_id, view.id)
                )
            except ValueError:
                return None
            return caller_page(
                csrf,
                view,
                locale=locale,
                allow_master_actions=isinstance(current_session(request), MasterAdminSession),
                activities=activity.items,
                activity_next_cursor=activity.next_cursor,
                activity_before_cursor=before,
            )

        return protected_page(request, render, allow_self_script=True)

    @router.post("/libraries/{library_id}/callers/{caller_id}/metadata")
    async def update_agent_metadata(request: Request, library_id: str, caller_id: str) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(MasterUpdateAgentInput.model_fields) | {"csrf_token"},
                max_bytes=_MAX_LIBRARY_EDIT_FORM_BYTES,
            )
            _require_csrf(values, session)
            await run_in_threadpool(
                service.update_agent_as_master,
                library_id,
                caller_id,
                MasterUpdateAgentInput.model_validate(values),
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except AgentMetadataVersionConflictError:
            status, message = (
                409,
                "The Agent changed since this form was opened. Reload and try again.",
            )
        except AgentNameConflictError:
            status, message = 409, "An identity with that name already exists in this Library."
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except ResourceNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except IntegrityError:
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(f"/admin/libraries/{library_id}/callers/{caller_id}")
        if status == 401:
            response = html(
                login_page(locale=locale, message=message), locale=locale, status_code=401
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        view = read_model.get_caller(library_id, caller_id)
        if view is None or view.kind != "agent":
            return html(
                browser_not_found_page(session.csrf_token, locale=locale),
                locale=locale,
                status_code=404,
            )
        activity = read_model.content_activity_page(actor=(view.library_id, view.id))
        return html(
            caller_page(
                session.csrf_token,
                view,
                locale=locale,
                allow_master_actions=True,
                activities=activity.items,
                activity_next_cursor=activity.next_cursor,
                message=message,
            ),
            locale=locale,
            status_code=status,
            allow_self_script=True,
        )

    def grant_editor(
        request: Request,
        library_id: str,
        caller_id: str,
        credential_id: str,
        *,
        message: str | None = None,
        status_code: int = 200,
    ) -> Response:
        locale = locale_for(request)
        session = current_session(request)
        if session is None:
            return redirect("/admin/login")
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        view = read_model.get_caller(library_id, caller_id)
        credential = (
            next((item for item in view.credentials if item.id == credential_id), None)
            if view is not None
            else None
        )
        now = utc_microseconds()
        if (
            view is None
            or view.kind != "agent"
            or view.disabled_at is not None
            or credential is None
            or not credential.library_grants_policy
            or credential.created_at > now
            or credential.expires_at <= now
            or credential.revoked_at is not None
            or credential.rotated_at is not None
        ):
            return html(
                browser_not_found_page(session.csrf_token, locale=locale),
                locale=locale,
                status_code=404,
            )
        page_response = html(
            agent_grants_page(
                session.csrf_token,
                view,
                credential,
                read_model.list_libraries(),
                locale=locale,
                message=message,
            ),
            locale=locale,
            status_code=status_code,
        )
        remember_requested_locale(page_response, request)
        return page_response

    @router.get("/libraries/{library_id}/callers/{caller_id}/credentials/{credential_id}/grants")
    def edit_agent_grants(
        request: Request, library_id: str, caller_id: str, credential_id: str
    ) -> Response:
        return grant_editor(request, library_id, caller_id, credential_id)

    @router.post(
        "/libraries/{library_id}/callers/{caller_id}/credentials/{credential_id}"
        "/grants/{target_library_id}"
    )
    async def set_agent_library_grants(
        request: Request,
        library_id: str,
        caller_id: str,
        credential_id: str,
        target_library_id: str,
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
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        stale_grants = False
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(MasterSetAgentLibraryGrantsInput.model_fields)
                | {"csrf_token"},
            )
            _require_csrf(values, session)
            await run_in_threadpool(
                service.set_agent_grants_as_master,
                library_id,
                caller_id,
                credential_id,
                target_library_id,
                MasterSetAgentLibraryGrantsInput.model_validate(values),
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except GrantVersionConflictError:
            status, message = 409, "Grants changed since this page was opened. Review and retry."
            stale_grants = True
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except ResourceNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except PolicyConflictError:
            status, message = 409, "Legacy Section credentials require explicit reprovision."
        except (CredentialLifecycleError, IntegrityError):
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(
                f"/admin/libraries/{library_id}/callers/{caller_id}"
                f"/credentials/{credential_id}/grants"
            )
        if status == 401:
            return html(login_page(locale=locale, message=message), locale=locale, status_code=401)
        if status == 409 and not stale_grants:
            return html(
                operations_page(
                    session.csrf_token, locale=locale, message=message, master_mode=True
                ),
                locale=locale,
                status_code=status,
            )
        return grant_editor(
            request,
            library_id,
            caller_id,
            credential_id,
            message=message,
            status_code=status,
        )

    @router.post("/libraries/{library_id}/callers/{caller_id}/credentials/{credential_id}/reveal")
    async def reveal_agent_token(
        request: Request, library_id: str, caller_id: str, credential_id: str
    ) -> Response:
        def safe_text(message: str, status_code: int) -> PlainTextResponse:
            return PlainTextResponse(message, status_code=status_code, headers=_SECURITY_HEADERS)

        if not _same_origin_submission(request):
            return safe_text("Request origin was rejected.", 403)
        session = current_session(request)
        if session is None:
            return safe_text("Sign in again.", 401)
        if not isinstance(session, MasterAdminSession):
            return safe_text("A master session is required.", 403)
        try:
            values = await _read_form(request, allowed_fields=frozenset({"csrf_token"}))
            _require_csrf(values, session)
        except _FormError as exc:
            return safe_text(exc.safe_message, exc.status_code)

        def current_value() -> tuple[int, str]:
            # Serialize this sensitive read with rotation, revocation and master
            # session invalidation so all checks describe one current state.
            with immediate_transaction(engine) as connection:
                if not MasterTokenRepository(connection).is_session_generation_current(
                    session.identity_id, session.session_generation
                ):
                    return 401, "Sign in again."
                repository = AuthRepository(connection)
                caller = repository.get_caller(library_id, caller_id)
                credential = repository.get_credential(library_id, caller_id, credential_id)
                if caller is None or caller.kind.value != "agent" or credential is None:
                    return 404, "The Agent credential was not found."
                now = utc_microseconds()
                if (
                    caller.disabled_at is not None
                    or credential.created_at > now
                    or credential.expires_at <= now
                    or credential.revoked_at is not None
                    or credential.rotated_at is not None
                ):
                    return 410, "The Agent credential is no longer active."
                value = repository.get_active_agent_token_value(
                    library_id, caller_id, credential_id, active_at=now
                )
                if value is None:
                    return 410, "The Agent Token cannot be recovered."
                # A reveal is committed only with its non-secret audit record.
                # If the audit write fails, the transaction aborts before the
                # plaintext can be sent to the browser.
                MasterAuditRepository(connection).add_success(
                    identity_id=session.identity_id,
                    session_generation=session.session_generation,
                    session_fingerprint=session.audit_fingerprint(),
                    action="auth.agent_token.reveal",
                    target_type="credential",
                    target_id=credential_id,
                    occurred_at=now,
                    event_id=uuid4().hex,
                )
                return 200, value

        status, value = await run_in_threadpool(current_value)
        return safe_text(value, status)

    @router.post("/libraries/{library_id}/callers/{caller_id}/credentials/{credential_id}/revoke")
    async def revoke_agent_token_as_master(
        request: Request, library_id: str, caller_id: str, credential_id: str
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
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(request, allowed_fields=frozenset({"csrf_token"}))
            _require_csrf(values, session)
            await run_in_threadpool(
                service.revoke_agent_credential_as_master,
                library_id,
                caller_id,
                credential_id,
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except ResourceNotFoundError:
            status, message = 404, "The Agent credential was not found."
        except (CredentialLifecycleError, IntegrityError):
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(f"/admin/libraries/{library_id}/callers/{caller_id}")
        return html(
            operations_page(session.csrf_token, locale=locale, message=message, master_mode=True),
            locale=locale,
            status_code=status,
        )

    @router.post("/libraries/{library_id}/callers/{caller_id}/credentials/{credential_id}/rotate")
    async def rotate_agent_token_as_master(
        request: Request, library_id: str, caller_id: str, credential_id: str
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
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(MasterRotateAgentCredentialInput.model_fields)
                | {"csrf_token"},
            )
            _require_csrf(values, session)
            result = await run_in_threadpool(
                service.rotate_agent_credential_as_master,
                library_id,
                caller_id,
                credential_id,
                MasterRotateAgentCredentialInput.model_validate(values),
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except ResourceNotFoundError:
            status, message = 404, "The Agent credential was not found."
        except PolicyConflictError:
            status, message = 409, "Legacy Section credentials require explicit reprovision."
        except (CredentialLifecycleError, IntegrityError):
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return html(
                credential_page(
                    session.csrf_token,
                    heading="Agent credential rotated",
                    result=result,
                    locale=locale,
                    recoverable=True,
                ),
                locale=locale,
            )
        if status == 401:
            return html(login_page(locale=locale, message=message), locale=locale, status_code=401)
        return html(
            operations_page(session.csrf_token, locale=locale, message=message, master_mode=True),
            locale=locale,
            status_code=status,
        )

    @router.get("/libraries/{library_id}/sections/{section_id}")
    def section_detail(request: Request, library_id: str, section_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_section(library_id, section_id)
            return (
                None
                if view is None
                else section_page(
                    csrf,
                    view,
                    locale=locale,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                )
            )

        return protected_page(request, render)

    @router.post("/libraries/{library_id}/sections/{section_id}")
    async def update_section(request: Request, library_id: str, section_id: str) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(UpdateSectionInput.model_fields) | {"csrf_token"},
                max_bytes=_MAX_SECTION_EDIT_FORM_BYTES,
            )
            _require_csrf(values, session)
            submitted = UpdateSectionInput.model_validate(values)
            await run_in_threadpool(
                service.update_section_as_master,
                library_id,
                section_id,
                submitted,
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except LibraryStructureNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except LibraryStructureVersionConflictError:
            status, message = (
                409,
                "The Section changed since this form was opened. Reload and try again.",
            )
        except (LibrarySeedConflictError, IntegrityError):
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(f"/admin/libraries/{library_id}/sections/{section_id}")
        view = read_model.get_section(library_id, section_id)
        if status == 401:
            response = html(
                login_page(locale=locale, message=message), locale=locale, status_code=status
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        return html(
            browser_not_found_page(session.csrf_token, locale=locale)
            if view is None
            else section_page(
                session.csrf_token, view, locale=locale, master_mode=True, message=message
            ),
            locale=locale,
            status_code=404 if view is None else status,
        )

    @router.get("/libraries/{library_id}/sections/{section_id}/trash")
    def section_trash(request: Request, library_id: str, section_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            values = request.query_params.getlist("before")
            if len(values) > 1:
                return None
            try:
                view = read_model.list_trash(
                    library_id, section_id=section_id, before=values[0] if values else None
                )
            except ValueError:
                return None
            return None if view is None else trash_directory_page(csrf, view, locale=locale)

        return protected_page(request, render)

    @router.get("/libraries/{library_id}/sections/{section_id}/trash/{page_id}")
    def trash_detail(request: Request, library_id: str, section_id: str, page_id: str) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            view = read_model.get_trash_page(library_id, section_id, page_id)
            return (
                None
                if view is None
                else trash_detail_page(
                    csrf,
                    view,
                    locale=locale,
                    restore_key=uuid4().hex,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                )
            )

        return protected_page(request, render)

    @router.post("/libraries/{library_id}/sections/{section_id}/trash/{page_id}/restore")
    async def restore_trash_page(
        request: Request, library_id: str, section_id: str, page_id: str
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
            fields = (
                MasterRestoreArchiveFormInput.model_fields
                if isinstance(session, MasterAdminSession)
                else RestoreArchiveFormInput.model_fields
            )
            values = await _read_form(request, allowed_fields=frozenset(fields) | {"csrf_token"})
            _require_csrf(values, session)
            if isinstance(session, MasterAdminSession):
                submitted_master = MasterRestoreArchiveFormInput.model_validate(values)
                await run_in_threadpool(
                    service.restore_archive_page_as_master,
                    library_id,
                    section_id,
                    page_id,
                    submitted_master,
                    master_session=session,
                )
            else:
                submitted = RestoreArchiveFormInput.model_validate(values)
                await run_in_threadpool(
                    service.restore_archive_page, library_id, section_id, page_id, submitted
                )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = (
                (401, "Sign in again.")
                if isinstance(session, MasterAdminSession)
                else (403, "The operator credential was rejected.")
            )
        except AuthorizationError:
            status, message = 403, "The operator credential was rejected."
        except ArchiveNotFoundError:
            status, message = 404, "The requested Archive page was not found."
        except ArchivePreconditionFailedError:
            status, message = 412, _RESTORE_STALE_MESSAGE
        except ArchiveLifecycleUnchangedError:
            status, message = 409, "The page has already been restored."
        except IdempotencyConflictError:
            status, message = 409, "The restore request conflicts with a previous request."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(f"/admin/libraries/{library_id}/sections/{section_id}")
        if current_session(request) is None:
            login_response = html(
                login_page(locale=locale, message="Sign in again."),
                locale=locale,
                status_code=401,
            )
            _clear_cookie(login_response, secure=secure_cookie(request))
            return login_response
        return html(
            restore_error_page(session.csrf_token, library_id, section_id, message, locale=locale),
            locale=locale,
            status_code=status,
        )

    @router.post("/libraries/{library_id}/sections/{section_id}/books")
    async def create_book(request: Request, library_id: str, section_id: str) -> Response:
        def action(values: FormValues, session: AdminSession) -> str:
            created = service.create_book(
                library_id,
                section_id,
                CreateBookInput.model_validate(values),
                session_fingerprint=session.audit_fingerprint(),
                master_session=session if isinstance(session, MasterAdminSession) else None,
            )
            return f"/admin/libraries/{library_id}/sections/{section_id}/books/{created.id}"

        def render(csrf: str, locale: AdminLocale, message: str) -> str | None:
            view = read_model.get_section(library_id, section_id)
            return (
                None
                if view is None
                else section_page(
                    csrf,
                    view,
                    locale=locale,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                    message=message,
                )
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
            try:
                cursors = request.query_params.getlist("before")
                if len(cursors) > 1:
                    return None
                before = cursors[0] if cursors else None
                view = read_model.get_book_page(library_id, section_id, book_id, before)
            except ValueError:
                return None
            return (
                None
                if view is None
                else book_page(
                    csrf,
                    view,
                    locale=locale,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                    before_cursor=before,
                )
            )

        return protected_page(request, render)

    @router.post("/libraries/{library_id}/sections/{section_id}/books/{book_id}")
    async def update_book(
        request: Request, library_id: str, section_id: str, book_id: str
    ) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(UpdateBookInput.model_fields) | {"csrf_token"},
                max_bytes=_MAX_BOOK_EDIT_FORM_BYTES,
            )
            _require_csrf(values, session)
            submitted = UpdateBookInput.model_validate(values)
            await run_in_threadpool(
                service.update_book_as_master,
                library_id,
                section_id,
                book_id,
                submitted,
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except LibraryStructureNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except LibraryStructureVersionConflictError:
            status, message = (
                409,
                "The Book changed since this form was opened. Reload and try again.",
            )
        except (LibrarySeedConflictError, IntegrityError):
            status, message = 409, "The action conflicts with current local state."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(f"/admin/libraries/{library_id}/sections/{section_id}/books/{book_id}")
        view = read_model.get_book_page(library_id, section_id, book_id)
        if status == 401:
            response = html(
                login_page(locale=locale, message=message), locale=locale, status_code=status
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        return html(
            browser_not_found_page(session.csrf_token, locale=locale)
            if view is None
            else book_page(
                session.csrf_token, view, locale=locale, master_mode=True, message=message
            ),
            locale=locale,
            status_code=404 if view is None else status,
        )

    def stable_page_response(
        request: Request,
        library_id: str,
        page_id: str,
        revision_number: str | None = None,
    ) -> Response:
        locale = locale_for(request)
        session: AdminSession | None = None

        def authorize(connection: Connection) -> bool:
            nonlocal session
            session = session_for_connection(request, connection)
            return isinstance(session, MasterAdminSession)

        # Bad path/cursor text still passes transaction-local admission before 404.
        number = None
        if revision_number is not None:
            number = (
                int(revision_number)
                if 1 <= len(revision_number) <= 19
                and revision_number.isascii()
                and revision_number.isdecimal()
                and revision_number[0] != "0"
                else 0
            )
        try:
            before = revision_history_before(request)
        except ValueError:
            before = 0
        try:
            view = read_model.get_page_by_id(
                library_id,
                page_id,
                number,
                before_revision_number=before,
                authorize=authorize,
            )
        except AuthenticationError:
            if session is not None:
                return forbidden(request, "A master session is required.")
            redirect_response = redirect("/admin/login")
            _clear_cookie(redirect_response, secure=secure_cookie(request))
            remember_requested_locale(redirect_response, request)
            return redirect_response
        assert isinstance(session, MasterAdminSession)
        response = html(
            browser_not_found_page(session.csrf_token, locale=locale)
            if view is None
            else page_preview_page(
                session.csrf_token,
                view,
                locale=locale,
                master_mode=True,
                navigation_base=f"/admin/libraries/{library_id}/pages/{page_id}",
            ),
            locale=locale,
            status_code=404 if view is None else 200,
        )
        remember_requested_locale(response, request)
        return response

    @router.get("/libraries/{library_id}/pages/{page_id}")
    def stable_page_detail(request: Request, library_id: str, page_id: str) -> Response:
        return stable_page_response(request, library_id, page_id)

    @router.get("/libraries/{library_id}/pages/{page_id}/revisions/{revision_number}")
    def stable_page_revision_detail(
        request: Request, library_id: str, page_id: str, revision_number: str
    ) -> Response:
        return stable_page_response(request, library_id, page_id, revision_number)

    @router.get("/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}")
    def page_detail(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        def render(csrf: str, locale: AdminLocale) -> str | None:
            try:
                before = revision_history_before(request)
            except ValueError:
                return None
            view = read_model.get_page(
                library_id, section_id, book_id, page_id, before_revision_number=before
            )
            return (
                None
                if view is None
                else page_preview_page(
                    csrf,
                    view,
                    locale=locale,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                    message=result_message(
                        request,
                        {
                            "attached": "Tag attached.",
                            "already-attached": "Tag was already attached; nothing changed.",
                            "removed": "Tag removed.",
                            "not-attached": "Tag was not attached; nothing changed.",
                        },
                    ),
                )
            )

        return consume_tag_result(request, protected_page(request, render))

    @router.post(
        "/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}/delete"
    )
    async def delete_page(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(MasterDeletePageFormInput.model_fields) | {"csrf_token"},
            )
            _require_csrf(values, session)
            submitted = MasterDeletePageFormInput.model_validate(values)
            await run_in_threadpool(
                service.delete_page_as_master,
                library_id,
                section_id,
                book_id,
                page_id,
                submitted,
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except ArchiveNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except ArchivePreconditionFailedError:
            status, message = (
                412,
                "The Page changed since this form was opened. Reload and try again.",
            )
        except ArchiveLifecycleUnchangedError:
            status, message = 409, "The page has already been deleted."
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(f"/admin/libraries/{library_id}/sections/{section_id}/trash/{page_id}")
        if status == 401:
            response = html(
                login_page(locale=locale, message=message), locale=locale, status_code=status
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        return html(
            page_delete_error_page(
                session.csrf_token, library_id, section_id, message, locale=locale
            ),
            locale=locale,
            status_code=status,
        )

    @router.post("/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}")
    async def update_page_title(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            return html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(MasterUpdatePageTitleInput.model_fields) | {"csrf_token"},
                max_bytes=_MAX_PAGE_TITLE_EDIT_FORM_BYTES,
            )
            _require_csrf(values, session)
            submitted = MasterUpdatePageTitleInput.model_validate(values)
            await run_in_threadpool(
                service.update_page_title_as_master,
                library_id,
                section_id,
                book_id,
                page_id,
                submitted,
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except PageTitleNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except PageTitleVersionConflictError:
            status, message = (
                409,
                "The Page changed since this form was opened. Reload and try again.",
            )
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            return redirect(
                f"/admin/libraries/{library_id}/sections/{section_id}/books/{book_id}"
                f"/pages/{page_id}"
            )
        if status == 401:
            response = html(
                login_page(locale=locale, message=message), locale=locale, status_code=status
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        view = read_model.get_page(library_id, section_id, book_id, page_id)
        return html(
            browser_not_found_page(session.csrf_token, locale=locale)
            if view is None
            else page_preview_page(
                session.csrf_token,
                view,
                locale=locale,
                master_mode=True,
                message=message,
                error=True,
            ),
            locale=locale,
            status_code=404 if view is None else status,
        )

    @router.post(
        "/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}/occurrence"
    )
    async def correct_page_occurrence(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        locale = locale_for(request)
        if not _same_origin_submission(request):
            return forbidden(request)
        session = current_session(request)
        if session is None:
            response = html(
                login_page(locale=locale, message="Sign in again."), locale=locale, status_code=401
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        if not isinstance(session, MasterAdminSession):
            return forbidden(request, "A master session is required.")
        try:
            values = await _read_form(
                request,
                allowed_fields=frozenset(MasterCorrectOccurrenceInput.model_fields)
                | {"csrf_token"},
            )
            _require_csrf(values, session)
            submitted = MasterCorrectOccurrenceInput.model_validate(values)
            changed = await run_in_threadpool(
                service.correct_page_occurrence_as_master,
                library_id,
                section_id,
                book_id,
                page_id,
                submitted,
                master_session=session,
            )
        except _FormError as exc:
            status, message = exc.status_code, exc.safe_message
        except AuthenticationError:
            status, message = 401, "Sign in again."
        except ArchiveNotFoundError:
            status, message = 404, "The requested local resource was not found."
        except ArchivePreconditionFailedError:
            status, message = (
                412,
                "The Page changed since this form was opened. Reload and try again.",
            )
        except (ValidationError, ValueError):
            status, message = 422, "Check the submitted fields and try again."
        except Exception:
            status, message = 500, "The action could not be completed."
        else:
            status = 200
            message = "Declared time saved." if changed else "Declared time is unchanged."
        if status == 401:
            response = html(
                login_page(locale=locale, message=message), locale=locale, status_code=status
            )
            _clear_cookie(response, secure=secure_cookie(request))
            return response
        view = read_model.get_page(library_id, section_id, book_id, page_id)
        return html(
            browser_not_found_page(session.csrf_token, locale=locale)
            if view is None
            else page_preview_page(
                session.csrf_token,
                view,
                locale=locale,
                master_mode=True,
                message=message,
                error=status != 200,
            ),
            locale=locale,
            status_code=404 if view is None else status,
        )

    @router.post(
        "/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}/tags"
    )
    async def set_page_tag(
        request: Request, library_id: str, section_id: str, book_id: str, page_id: str
    ) -> Response:
        base = (
            f"/admin/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}"
        )

        def action(values: FormValues, session: AdminSession) -> tuple[str, str]:
            if isinstance(session, MasterAdminSession):
                submitted = MasterPageTagFormInput.model_validate(values)
                changed = service.set_page_tag_as_master(
                    library_id,
                    section_id,
                    book_id,
                    page_id,
                    submitted,
                    master_session=session,
                )
            else:
                submitted = PageTagFormInput.model_validate(values)
                changed = service.set_page_tag(library_id, section_id, book_id, page_id, submitted)
            result = (
                ("attached" if changed else "already-attached")
                if submitted.operation == "attach"
                else ("removed" if changed else "not-attached")
            )
            return base, result

        def render(
            csrf: str, locale: AdminLocale, message: str | None, error: bool, session: AdminSession
        ) -> str | None:
            view = read_model.get_page(library_id, section_id, book_id, page_id)
            return (
                None
                if view is None
                else page_preview_page(
                    csrf,
                    view,
                    locale=locale,
                    message=message,
                    error=error,
                    master_mode=isinstance(session, MasterAdminSession),
                )
            )

        return await tag_action(
            request,
            legacy_fields=frozenset(PageTagFormInput.model_fields),
            master_fields=frozenset(MasterPageTagFormInput.model_fields),
            action=action,
            render=render,
        )

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
            try:
                before = revision_history_before(request)
            except ValueError:
                return None
            view = read_model.get_page(
                library_id,
                section_id,
                book_id,
                page_id,
                revision_number,
                before_revision_number=before,
            )
            return (
                None
                if view is None
                else page_preview_page(
                    csrf,
                    view,
                    locale=locale,
                    master_mode=isinstance(current_session(request), MasterAdminSession),
                )
            )

        return protected_page(request, render)

    @router.get(
        "/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}"
        "/revisions/{revision_number}/files/{filename:path}"
    )
    async def download_page_file(
        request: Request,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: str,
        filename: str,
    ) -> Response:
        locale = locale_for(request)
        session: AdminSession | None = None

        def authorize(connection: Connection) -> bool:
            nonlocal session
            session = session_for_connection(request, connection)
            return session is not None

        # Invalid revision text still goes through session admission before a
        # not-found result. Never coerce aliases such as '+1' or '01'.
        number = (
            int(revision_number)
            if 1 <= len(revision_number) <= 19
            and revision_number.isascii()
            and revision_number.isdecimal()
            and revision_number[0] != "0"
            else 0
        )
        try:
            file = await run_in_threadpool(
                file_download.get_file,
                library_id,
                section_id,
                book_id,
                page_id,
                number,
                filename,
                authorize=authorize,
            )
        except AuthenticationError:
            response = redirect("/admin/login")
            _clear_cookie(response, secure=secure_cookie(request))
            remember_requested_locale(response, request)
            return response
        except Exception:
            message = (
                "无法下载此文件。" if locale == "zh-CN" else "The file could not be downloaded."
            )
            return PlainTextResponse(
                message, status_code=500, headers={**_SECURITY_HEADERS, "Content-Language": locale}
            )
        if file is None:
            assert session is not None
            return html(
                browser_not_found_page(session.csrf_token, locale=locale),
                locale=locale,
                status_code=404,
            )
        return Response(
            content=file.content,
            media_type="application/octet-stream",
            headers={
                **_SECURITY_HEADERS,
                "Content-Language": locale,
                "Content-Disposition": (
                    "attachment; filename=\"download\"; filename*=UTF-8''"
                    + quote(file.filename, safe="")
                ),
            },
        )

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
        master_initialized, master_identity = await run_in_threadpool(
            authenticate_master, candidate
        )
        if master_initialized and master_identity is not None:
            encoded, _ = codec.issue_master(*master_identity)
        elif (
            not master_initialized
            and password_hash is not None
            and await run_in_threadpool(password_matches, candidate, password_hash)
        ):
            encoded, _ = codec.issue()
        else:
            return html(
                login_page(
                    locale=locale, message="Invalid password.", master_mode=master_initialized
                ),
                locale=locale,
                status_code=401,
            )
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
            lambda csrf, locale: guide_page(
                csrf,
                "guide",
                locale=locale,
                skill_bundle=skill_bundle,
                retrieval_available=retrieval_available,
            ),
        )

    @router.get("/agent")
    def agent_guide(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: guide_page(
                csrf,
                "agent",
                locale=locale,
                skill_bundle=skill_bundle,
                retrieval_available=retrieval_available,
            ),
        )

    @router.get("/mcp")
    def mcp_guide(request: Request) -> Response:
        return protected_page(
            request,
            lambda csrf, locale: guide_page(
                csrf,
                "mcp",
                locale=locale,
                skill_bundle=skill_bundle,
                retrieval_available=retrieval_available,
            ),
        )

    @router.get("/style.css")
    def stylesheet() -> Response:
        return PlainTextResponse(
            STYLESHEET,
            media_type="text/css",
            headers=_SECURITY_HEADERS,
        )

    @router.get("/reveal.js")
    def reveal_script() -> Response:
        return PlainTextResponse(
            REVEAL_SCRIPT,
            media_type="application/javascript",
            headers=_SECURITY_HEADERS,
        )

    router.include_router(
        create_master_file_set_router(
            engine,
            current_session=current_session,
            locale_for=locale_for,
            same_origin=_same_origin_submission,
            html_response=lambda content, locale, status, script: html(
                content, locale=locale, status_code=status, allow_self_script=script
            ),
            clear_session=lambda response, request: _clear_cookie(
                response, secure=secure_cookie(request)
            ),
        )
    )
    router.include_router(
        create_master_page_move_router(
            engine,
            current_session=current_session,
            locale_for=locale_for,
            same_origin=_same_origin_submission,
            html_response=lambda content, locale, status, script: html(
                content, locale=locale, status_code=status, allow_self_script=script
            ),
            clear_session=lambda response, request: _clear_cookie(
                response, secure=secure_cookie(request)
            ),
        )
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
    max_fields: int = _MAX_FORM_FIELDS,
    max_bytes: int = _MAX_FORM_BYTES,
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
        if content_length > max_bytes:
            raise _FormError(413, "The submitted form is too large.")
    body = bytearray()
    async for chunk in request.stream():
        _extend_form_body(body, chunk, max_bytes=max_bytes)
    try:
        decoded = body.decode("utf-8")
        pairs = parse_qsl(
            decoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=max_fields,
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


def _extend_form_body(body: bytearray, chunk: bytes, *, max_bytes: int = _MAX_FORM_BYTES) -> None:
    if len(body) + len(chunk) > max_bytes:
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


def _form_utc_microseconds(value: str) -> int | None:
    if not value:
        return None
    if (
        re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
            r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?",
            value,
        )
        is None
    ):
        raise InvalidSearchQueryV2
    try:
        parsed = datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        raise InvalidSearchQueryV2 from None
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _search_form_values(values: FormValues) -> SearchFormValues:
    """Retain admitted POST controls without replacing them with normalized values."""

    library_ids = values.get("library_id", [])
    tags = values.get("tags", [])
    if not isinstance(library_ids, list) or not isinstance(tags, list):
        raise InvalidSearchQueryV2
    return SearchFormValues(
        keywords=_single(values, "keywords"),
        library_ids=tuple(identity for identity in library_ids if identity),
        tags=tuple(tags),
        occurred_from=_single(values, "occurred_from"),
        occurred_before=_single(values, "occurred_before"),
    )


def _search_query_from_form(values: SearchFormValues) -> SearchQueryV2:
    """Translate the bounded HTML form into the exact v2 API query contract."""

    keywords = values.keywords.split()
    tag_identities = []
    for tag in values.tags:
        library, separator, identity = tag.partition(":")
        if separator != ":" or not library or not identity or ":" in identity:
            raise InvalidSearchQueryV2
        tag_identities.append({"library_id": library, "tag_id": identity})
    query: dict[str, object] = {"keywords": keywords, "tags_any": tag_identities}
    if values.library_ids:
        query["libraries"] = list(values.library_ids)
    for raw_time, field in (
        (values.occurred_from, "occurred_from_us"),
        (values.occurred_before, "occurred_before_us"),
    ):
        value = _form_utc_microseconds(raw_time)
        if value is not None:
            query[field] = value
    if (
        not keywords
        and not tag_identities
        and not any(field in query for field in ("occurred_from_us", "occurred_before_us"))
    ):
        raise _FormError(422, "Choose a keyword, Tag, or time range before searching.")
    return parse_query_v2_json(json.dumps(query, ensure_ascii=False).encode("utf-8"))


def _clear_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        _SESSION_COOKIE,
        path="/admin",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


__all__ = ["create_admin_router"]
