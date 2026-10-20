"""Small session DTOs shared with generated browser client types."""

from pydantic import BaseModel


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
