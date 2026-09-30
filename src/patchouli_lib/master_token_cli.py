"""Local-only setup, rotation and lost-token recovery for administration.

The token is never accepted as an argument, printed, or included in an error.
An empty database does not enable an HTTP registration path.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from getpass import getpass
from pathlib import Path
from typing import TextIO

from sqlalchemy.engine import make_url

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.config import Settings
from patchouli_lib.database import (
    CURRENT_SCHEMA_REVISION,
    build_engine,
    check_database,
    immediate_transaction,
)

_MAX_INPUT_CHARACTERS = 1_024
_USAGE = (
    "Usage: patchouli-master-token {initialize|rotate} [--stdin]\n"
    "       patchouli-master-token recover --confirm-local-reset [--stdin]"
)
_BAD_INPUT = "Invalid master token command or input."
_FAILED = "Master token operation failed. No token was printed."


class _InputError(ValueError):
    pass


def _write_error(stream: TextIO, message: str) -> None:
    try:
        stream.write(f"{message}\n")
        stream.flush()
    except BaseException:
        pass


def _read_line(stream: TextIO) -> str:
    value = stream.readline(_MAX_INPUT_CHARACTERS + 2)
    if not value:
        raise _InputError
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if len(value) > _MAX_INPUT_CHARACTERS:
        raise _InputError
    return value


def _read_tokens(command: str, use_stdin: bool, stream: TextIO, errors: TextIO) -> tuple[str, ...]:
    prompts = (
        ("New master token: ", "Confirm new master token: ")
        if command in {"initialize", "recover"}
        else ("Current master token: ", "New master token: ", "Confirm new master token: ")
    )
    if use_stdin:
        values = tuple(_read_line(stream) for _ in prompts)
        if stream.read(1):
            raise _InputError
    else:
        if stream is not sys.stdin or not stream.isatty():
            raise _InputError
        values = tuple(getpass(prompt, stream=errors) for prompt in prompts)
    if values[-1] != values[-2]:
        raise _InputError
    return values[:-1]


def _parse(arguments: Sequence[str]) -> tuple[str, bool] | None:
    if list(arguments) == ["--help"]:
        return None
    if len(arguments) == 1 and arguments[0] in {"initialize", "rotate"}:
        return arguments[0], False
    if (
        len(arguments) == 2
        and arguments[0] in {"initialize", "rotate"}
        and arguments[1] == "--stdin"
    ):
        return arguments[0], True
    if arguments and arguments[0] == "recover":
        if list(arguments[1:]) == ["--confirm-local-reset"]:
            return "recover", False
        if list(arguments[1:]) == ["--confirm-local-reset", "--stdin"]:
            return "recover", True
    raise _InputError


def _require_existing_database(database_url: str) -> None:
    database = make_url(database_url).database
    if database is None or database == ":memory:" or not Path(database).expanduser().is_file():
        raise RuntimeError("The master token CLI requires an existing database.")


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Initialize, rotate or recover locally, without delivering token text."""

    arguments = sys.argv[1:] if argv is None else argv
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    try:
        parsed = _parse(arguments)
        if parsed is None:
            output_stream.write(f"{_USAGE}\n")
            return 0
        command, use_stdin = parsed
        tokens = _read_tokens(command, use_stdin, input_stream, error_stream)
    except (EOFError, UnicodeError, ValueError, OSError):
        _write_error(error_stream, _BAD_INPUT)
        return 2

    engine = None
    try:
        settings = Settings()
        _require_existing_database(settings.database_url)
        engine = build_engine(settings.database_url)
        check_database(engine)
        with immediate_transaction(engine) as connection:
            # Recheck under the write lock: an independent migration may have
            # changed the schema after the initial readiness check.
            revision = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            if revision != CURRENT_SCHEMA_REVISION:
                raise RuntimeError("Schema changed before master token operation.")
            repository = MasterTokenRepository(connection)
            if command == "initialize":
                repository.initialize_from_local_cli(tokens[0], now=utc_microseconds())
            elif command == "rotate":
                if repository.rotate(tokens[0], tokens[1], now=utc_microseconds()) is None:
                    raise RuntimeError("Master token operation was not accepted.")
            elif repository.recover_from_local_cli(tokens[0], now=utc_microseconds()) is None:
                raise RuntimeError("Master token operation was not accepted.")
    except Exception:
        _write_error(error_stream, _FAILED)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
