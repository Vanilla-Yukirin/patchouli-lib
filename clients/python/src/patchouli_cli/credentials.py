from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Protocol, TextIO, cast

from patchouli_cli.errors import credential_error
from patchouli_client import BearerToken

_TOKEN_LIMIT = 8_192
_KEYRING_SERVICE = "patchouli-client"


class SecretStore(Protocol):
    def get_token(self, profile: str) -> str | None: ...


class KeyringSecretStore:
    """Optional adapter; importing the CLI never requires keyring."""

    def get_token(self, profile: str) -> str | None:
        try:
            module = importlib.import_module("keyring")
        except ImportError:
            return None
        try:
            get_password = cast(Callable[[str, str], str | None], module.get_password)
            value = get_password(_KEYRING_SERVICE, profile)
        except Exception as exc:
            raise credential_error("operating-system secret store could not be read") from exc
        if value is not None and not isinstance(value, str):
            raise credential_error("operating-system secret store returned an invalid value")
        return value


@dataclass(frozen=True, slots=True)
class ResolvedToken:
    token: BearerToken
    source: str

    def __repr__(self) -> str:
        return f"ResolvedToken(token=<redacted>, source={self.source!r})"


def resolve_token(
    *,
    profile: str,
    token_stdin: bool,
    environ: Mapping[str, str],
    stdin: BinaryIO | TextIO,
    secret_store: SecretStore,
) -> ResolvedToken:
    value: str | None
    if token_stdin:
        value = _read_token_stdin(stdin)
        source = "stdin"
    elif "PATCHOULI_TOKEN" in environ:
        value = environ["PATCHOULI_TOKEN"]
        source = "environment"
    else:
        value = secret_store.get_token(profile)
        source = "keyring"
        if value is None:
            raise credential_error(
                "no caller credential is available; use keyring, PATCHOULI_TOKEN, or --token-stdin"
            )
    assert value is not None
    if len(value) > _TOKEN_LIMIT:
        raise credential_error("caller credential exceeds the safe limit")
    try:
        return ResolvedToken(token=BearerToken(value), source=source)
    except ValueError as exc:
        raise credential_error("caller credential has an invalid format") from exc


def _read_token_stdin(stdin: BinaryIO | TextIO) -> str:
    value = stdin.read(_TOKEN_LIMIT + 1)
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise credential_error("stdin caller credential must be visible ASCII") from exc
    if len(value) > _TOKEN_LIMIT:
        raise credential_error("stdin caller credential exceeds the safe limit")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if "\n" in value or "\r" in value:
        raise credential_error("stdin caller credential must contain exactly one line")
    return value
