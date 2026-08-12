import re
from collections.abc import Mapping, Sequence
from email.utils import format_datetime, parsedate_to_datetime
from http import HTTPStatus
from typing import Annotated, Literal, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import Field
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, ExceptionHandler, Message, Receive, Scope, Send

from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL, WireModel
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, ensure_request_id

PROBLEM_MEDIA_TYPE = "application/problem+json"
DEFAULT_PROBLEM_TYPE: Literal["about:blank"] = "about:blank"
WWW_AUTHENTICATE_BEARER = "Bearer"

_MAX_VALIDATION_ERRORS = 20
_MAX_VALIDATION_LOCATION_DEPTH = 10
_PROBLEM_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_SAFE_ERROR_TYPE_PATTERN = re.compile(r"^[a-z0-9_.-]{1,64}$", re.ASCII)
_SAFE_LOCATION_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$", re.ASCII)
_HTTP_METHOD_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,32}$", re.ASCII)
_AUTHENTICATION_CHALLENGES = {
    (401, "authentication_required"): WWW_AUTHENTICATE_BEARER,
    (401, "invalid_token"): 'Bearer error="invalid_token"',
    (403, "insufficient_scope"): 'Bearer error="insufficient_scope"',
}


class ValidationIssue(WireModel):
    """Allow-listed request validation metadata without client input values."""

    location: Annotated[list[str | int], Field(max_length=_MAX_VALIDATION_LOCATION_DEPTH)]
    type: Annotated[str, Field(pattern=r"^[a-z0-9_.-]{1,64}$")]
    message: Literal["Invalid value."] = "Invalid value."


class ValidationProblemDetails(WireModel):
    """The only non-empty Problem Details extension payload in the MVP kernel."""

    errors: Annotated[list[ValidationIssue], Field(max_length=_MAX_VALIDATION_ERRORS)]
    truncated: bool


class EmptyProblemDetails(WireModel):
    """The explicit empty extension payload for problems without safe metadata."""


class ProblemDetails(WireModel):
    """RFC 9457 response with stable PatchouliLib extension members."""

    type: Literal["about:blank"] = DEFAULT_PROBLEM_TYPE
    title: str
    status: Annotated[int, Field(ge=400, le=599)]
    detail: str
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    request_id: str
    details: EmptyProblemDetails | ValidationProblemDetails = Field(
        default_factory=EmptyProblemDetails
    )


class ApplicationProblem(Exception):
    """A reviewed, client-safe application failure.

    ``title`` and ``detail`` are public contract text. Dynamic private values belong
    neither in those fields nor in ``details``.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        details: object = None,
    ) -> None:
        if not 400 <= status_code <= 599:
            raise ValueError("Application problems require a 4xx or 5xx status.")
        if _PROBLEM_CODE_PATTERN.fullmatch(code) is None:
            raise ValueError("Application problem codes must use the stable wire format.")
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.details = details


class SafeExceptionMiddleware:
    """Convert unknown pre-response failures without logging their unsafe text."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_with_state(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_with_state)
        except Exception:
            if response_started:
                raise RuntimeError("Unhandled error after the response started.") from None
            request = Request(scope, receive=receive)
            response = problem_response(
                request,
                status_code=500,
                code="internal_error",
                title="Internal server error",
                detail="The server could not complete the request.",
            )
            await response(scope, receive, send)


def authentication_required() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=401,
        code="authentication_required",
        title="Authentication required",
        detail="A caller credential is required.",
    )


def invalid_token() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=401,
        code="invalid_token",
        title="Invalid credential",
        detail="The caller credential is invalid or no longer active.",
    )


def insufficient_scope() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=403,
        code="insufficient_scope",
        title="Insufficient scope",
        detail="The caller does not have the required action.",
    )


def resource_not_found() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=404,
        code="resource_not_found",
        title="Resource not found",
        detail="The requested resource was not found.",
    )


def _safe_problem_details(
    details: object,
) -> EmptyProblemDetails | ValidationProblemDetails:
    """Serialize only explicitly reviewed extension payload types."""
    if isinstance(details, ValidationProblemDetails):
        return details
    return EmptyProblemDetails()


def _validation_location(location: object) -> list[str | int]:
    if not isinstance(location, Sequence) or isinstance(location, str | bytes):
        return []
    result: list[str | int] = []
    for segment in location[:_MAX_VALIDATION_LOCATION_DEPTH]:
        if isinstance(segment, int) or (
            isinstance(segment, str) and _SAFE_LOCATION_PATTERN.fullmatch(segment) is not None
        ):
            result.append(segment)
        else:
            result.append("<field>")
    return result


def _validation_details(exc: RequestValidationError) -> ValidationProblemDetails:
    raw_errors = exc.errors()
    errors: list[ValidationIssue] = []
    for error in raw_errors[:_MAX_VALIDATION_ERRORS]:
        error_type = error.get("type")
        if (
            not isinstance(error_type, str)
            or _SAFE_ERROR_TYPE_PATTERN.fullmatch(error_type) is None
        ):
            error_type = "validation_error"
        errors.append(
            ValidationIssue(
                location=_validation_location(error.get("loc")),
                type=error_type,
            )
        )
    return ValidationProblemDetails(
        errors=errors,
        truncated=len(raw_errors) > _MAX_VALIDATION_ERRORS,
    )


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    details: object = None,
    safe_headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = ensure_request_id(request)
    problem = ProblemDetails(
        title=title,
        status=status_code,
        detail=detail,
        code=code,
        request_id=request_id,
        details=_safe_problem_details(details),
    )
    headers = dict(safe_headers or {})
    headers[REQUEST_ID_HEADER] = request_id
    headers["Cache-Control"] = PROTECTED_CACHE_CONTROL
    challenge = _AUTHENTICATION_CHALLENGES.get((status_code, code))
    if challenge is not None:
        headers["WWW-Authenticate"] = challenge
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json"),
        headers=headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def application_problem_handler(
    request: Request,
    exc: ApplicationProblem,
) -> JSONResponse:
    return problem_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        details=exc.details,
    )


async def request_validation_problem_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return problem_response(
        request,
        status_code=422,
        code="request_validation_failed",
        title="Request validation failed",
        detail="The request did not satisfy the required schema.",
        details=_validation_details(exc),
    )


def _http_problem(status_code: int) -> tuple[str, str, str]:
    problems = {
        400: ("Bad request", "malformed_request", "The request could not be processed."),
        401: ("Authentication required", "authentication_required", "Authentication is required."),
        403: ("Forbidden", "insufficient_scope", "The request is not permitted."),
        404: ("Resource not found", "resource_not_found", "The requested resource was not found."),
        405: ("Method not allowed", "method_not_allowed", "The method is not allowed."),
        413: ("Content too large", "content_too_large", "The request content is too large."),
        415: (
            "Unsupported media type",
            "unsupported_media_type",
            "The media type is not supported.",
        ),
        428: (
            "Precondition required",
            "precondition_required",
            "A required precondition is missing.",
        ),
        429: ("Too many requests", "rate_limited", "The request rate limit was exceeded."),
    }
    if status_code in problems:
        return problems[status_code]
    if 400 <= status_code < 500:
        try:
            title = HTTPStatus(status_code).phrase
        except ValueError:
            title = "Request error"
        return title, "request_error", "The request could not be completed."
    return "Internal server error", "internal_error", "The server could not complete the request."


def _safe_allow_header(value: str) -> str | None:
    methods = [method.strip() for method in value.split(",")]
    if not methods or any(_HTTP_METHOD_PATTERN.fullmatch(method) is None for method in methods):
        return None
    canonical = ", ".join(methods)
    return canonical if len(canonical) <= 256 else None


def _safe_retry_after_header(value: str) -> str | None:
    if value.isascii() and value.isdigit() and len(value) <= 10:
        return value
    if len(value) > 64 or not value.isascii() or any(ord(character) < 0x20 for character in value):
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        canonical = format_datetime(parsed, usegmt=True)
    except ValueError:
        return None
    return value if canonical == value else None


def _safe_http_exception_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    safe_headers: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered == "allow":
            safe_value = _safe_allow_header(value)
            if safe_value is not None:
                safe_headers["Allow"] = safe_value
        elif lowered == "retry-after":
            safe_value = _safe_retry_after_header(value)
            if safe_value is not None:
                safe_headers["Retry-After"] = safe_value
    return safe_headers


async def http_exception_problem_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    status_code = exc.status_code if 400 <= exc.status_code <= 599 else 500
    title, code, detail = _http_problem(status_code)
    return problem_response(
        request,
        status_code=status_code,
        code=code,
        title=title,
        detail=detail,
        safe_headers=_safe_http_exception_headers(exc.headers),
    )


def install_api_exception_handlers(application: FastAPI) -> None:
    """Install kernel handlers without adding routes or authentication behavior."""
    application.add_middleware(SafeExceptionMiddleware)
    application.add_exception_handler(
        ApplicationProblem,
        cast(ExceptionHandler, application_problem_handler),
    )
    application.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, request_validation_problem_handler),
    )
    application.add_exception_handler(
        HTTPException,
        cast(ExceptionHandler, http_exception_problem_handler),
    )
