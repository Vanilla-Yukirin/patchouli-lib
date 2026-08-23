from __future__ import annotations

import sys
from collections.abc import Sequence
from contextlib import suppress
from getpass import getpass
from typing import TextIO

from patchouli_lib.admin.passwords import hash_password

_MAX_PASSWORD_CHARACTERS = 1_024
_SAFE_INPUT_ERROR = "Invalid password input."
_SAFE_OUTPUT_ERROR = "Password hash output failed."


def _read_line(stream: TextIO) -> str:
    value = stream.readline(_MAX_PASSWORD_CHARACTERS + 2)
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if len(value) > _MAX_PASSWORD_CHARACTERS:
        raise ValueError
    return value


def _read_password_pair(stream: TextIO) -> tuple[str, str]:
    if stream is sys.stdin and stream.isatty():
        return (
            getpass("Administration password: "),
            getpass("Confirm administration password: "),
        )
    return _read_line(stream), _read_line(stream)


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Read and confirm one password from stdin, then emit only its verifier."""

    arguments = sys.argv[1:] if argv is None else argv
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    interactive_input = input_stream is sys.stdin and input_stream.isatty()
    if arguments:
        error_stream.write(f"{_SAFE_INPUT_ERROR}\n")
        return 2
    try:
        password, confirmation = _read_password_pair(input_stream)
        if password != confirmation or (not interactive_input and input_stream.read(1)):
            raise ValueError
        encoded = hash_password(password)
    except (EOFError, UnicodeError, ValueError):
        error_stream.write(f"{_SAFE_INPUT_ERROR}\n")
        return 2
    try:
        payload = f"{encoded}\n"
        written = output_stream.write(payload)
        if written != len(payload):
            raise OSError
        output_stream.flush()
    except BaseException:
        with suppress(BaseException):
            error_stream.write(f"{_SAFE_OUTPUT_ERROR}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
