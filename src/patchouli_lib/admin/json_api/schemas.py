"""Small response DTOs shared with generated browser client types."""

from typing import Literal

from pydantic import BaseModel

from patchouli_lib.content.schemas import StrongPageETag


class SessionState(BaseModel):
    authenticated: bool
    setup_required: bool
    setup_available: bool
    csrf_token: str | None
    style_nonce: str
    setup_csrf_token: str | None


class SessionAuth(BaseModel):
    authenticated: bool
    csrf_token: str | None = None
    requires_sign_in: bool = False


class PageDeleteResult(BaseModel):
    changed: Literal[True]
    restore_etag: StrongPageETag
