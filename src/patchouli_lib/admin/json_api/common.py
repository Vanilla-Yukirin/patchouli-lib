"""Small shared admission and serialization helpers for the browser API."""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from ipaddress import IPv6Address
from time import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, Engine
from starlette.responses import JSONResponse, Response

from patchouli_lib.admin.master_setup_session import MasterSetupSessionCodec
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import AdminSessionCodec, MasterAdminSession
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.config import Settings

SESSION_COOKIE = "patchouli_admin_session"
SETUP_COOKIE = "patchouli_master_setup"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
MAX_JSON_BYTES = 2 * 1024 * 1024


class ApiError(HTTPException):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(status, {"code": code, "message": code.replace("_", " ")})


def encode(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return jsonable_encoder(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable_encoder(asdict(value))
    return jsonable_encoder(value)


def reply(value: Any, status: int = 200, *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(encode(value), status_code=status, headers={**HEADERS, **(headers or {})})


def origin_parts(value: str) -> tuple[str, str, int] | None:
    if not value.isascii() or any(ord(c) <= 32 or c in "\\,?#\x7f" for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.netloc.endswith(":")
        ):
            return None
        host = parts.hostname.casefold()
        if ":" in host:
            if "%" in host:
                return None
            host = str(IPv6Address(host))
        port = parts.port
        if port == 0:
            return None
        return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def require_origin(request: Request) -> None:
    origins, hosts = request.headers.getlist("origin"), request.headers.getlist("host")
    if (
        len(origins) != 1
        or len(hosts) != 1
        or origin_parts(origins[0]) is None
        or origin_parts(origins[0]) != origin_parts(f"{request.scope['scheme']}://{hosts[0]}")
    ):
        raise ApiError(403, "origin_rejected")


def require_csrf(request: Request, expected: str) -> None:
    values = request.headers.getlist("x-csrf-token")
    if len(values) != 1 or not values[0].isascii() or not hmac.compare_digest(values[0], expected):
        raise ApiError(403, "csrf_rejected")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def decode_json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        if not isinstance(value, dict):
            raise ValueError
        # Reject non-finite JSON constants and escaped lone UTF-16 surrogates.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError(422, "invalid_json") from None


async def body_json(request: Request, model: type[BaseModel] | None = None) -> Any:
    if request.headers.get("content-type", "").partition(";")[0].strip() != "application/json":
        raise ApiError(415, "json_required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_JSON_BYTES:
            raise ApiError(413, "body_too_large")
    value = decode_json(bytes(body))
    if model is None:
        return value
    try:
        return model.model_validate(value)
    except ValidationError:
        # Validation errors may contain a submitted secret; never serialize them.
        raise ApiError(422, "invalid_input") from None


class BrowserAuth:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine, self.settings = engine, settings
        secret = settings.admin_session_signing_secret
        if secret is None:
            raise ValueError("Browser administration requires a session secret.")
        raw = secret.get_secret_value().encode("utf-8")
        self.codec = AdminSessionCodec(raw, ttl_seconds=settings.admin_session_ttl_seconds)
        self.setup_codec = MasterSetupSessionCodec(raw)

    def secure(self, request: Request) -> bool:
        return request.scope["scheme"] == "https" or not self.settings.admin_allow_private_http

    def require_transport(self, request: Request) -> None:
        if request.scope["scheme"] != "https" and not self.settings.admin_allow_private_http:
            raise ApiError(403, "https_required")

    def session(self, request: Request) -> MasterAdminSession:
        self.require_transport(request)
        # Bearer has no standing in this namespace, even with a valid cookie.
        if request.headers.getlist("authorization"):
            raise ApiError(401, "master_session_required")
        session = self.codec.verify_master(request.cookies.get(SESSION_COOKIE, ""))
        if session is None:
            raise ApiError(401, "sign_in_required")
        return session

    def authorize(self, connection: Connection, session: MasterAdminSession) -> None:
        if session.expires_at <= int(time()) or not MasterTokenRepository(
            connection
        ).is_session_generation_current(session.identity_id, session.session_generation):
            raise AuthenticationError

    def write_session(self, request: Request) -> MasterAdminSession:
        require_origin(request)
        session = self.session(request)
        require_csrf(request, session.csrf_token)
        return session

    @contextmanager
    def read(self, request: Request) -> Iterator[tuple[Connection, MasterAdminSession]]:
        session = self.session(request)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            try:
                self.authorize(connection, session)
                yield connection, session
            finally:
                connection.rollback()

    def set_cookie(
        self, response: Response, request: Request, value: str, *, setup: bool = False
    ) -> None:
        response.set_cookie(
            SETUP_COOKIE if setup else SESSION_COOKIE,
            value,
            max_age=300 if setup else self.settings.admin_session_ttl_seconds,
            path="/admin",
            secure=self.secure(request),
            httponly=True,
            samesite="strict",
        )

    def clear_cookie(self, response: Response, request: Request, *, setup: bool = False) -> None:
        response.delete_cookie(
            SETUP_COOKIE if setup else SESSION_COOKIE,
            path="/admin",
            secure=self.secure(request),
            httponly=True,
            samesite="strict",
        )
