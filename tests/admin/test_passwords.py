from __future__ import annotations

import sys
from io import StringIO

import pytest

import patchouli_lib.admin_password_cli as admin_password_cli
from patchouli_lib.admin.passwords import (
    PASSWORD_SCHEME,
    hash_password,
    parse_password_hash,
    password_matches,
)

_PASSWORD = "synthetic admin password"


def _hash(password: str = _PASSWORD) -> str:
    return hash_password(
        password,
        salt_factory=lambda size: b"s" * size,
        iterations=300_000,
    )


def test_password_hash_is_salted_parseable_and_verifiable() -> None:
    encoded = _hash()
    iterations, salt, digest = parse_password_hash(encoded)

    assert encoded.startswith(f"{PASSWORD_SCHEME}$300000$")
    assert _PASSWORD not in encoded
    assert iterations == 300_000
    assert salt == b"s" * 16
    assert len(digest) == 32
    assert password_matches(_PASSWORD, encoded)
    assert not password_matches("different synthetic password", encoded)
    assert not password_matches("short", encoded)
    assert not password_matches("x" * 1_025, encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-a-hash",
        "pbkdf2_sha256$1$bad$bad",
        "pbkdf2_sha256$0300000$c3Nzc3Nzc3Nzc3Nzc3Nzcw$" + ("a" * 43),
        "unknown$300000$c3Nzc3Nzc3Nzc3Nzc3Nzcw$" + ("a" * 43),
        "x" * 257,
    ],
)
def test_password_hash_parser_rejects_malformed_or_unsafe_values(encoded: str) -> None:
    with pytest.raises(ValueError, match="password hash|Password hash|Admin password"):
        parse_password_hash(encoded)
    assert not password_matches(_PASSWORD, encoded)


def test_password_hash_generation_rejects_bad_inputs_and_salt_factory() -> None:
    with pytest.raises(ValueError, match="length"):
        hash_password("short")
    with pytest.raises(ValueError, match="iteration"):
        hash_password(_PASSWORD, iterations=299_999)
    with pytest.raises(ValueError, match="salt"):
        hash_password(_PASSWORD, salt_factory=lambda size: b"x")


def test_password_cli_reads_confirmation_from_stdin_and_emits_only_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        admin_password_cli,
        "hash_password",
        lambda password: _hash(password),
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = admin_password_cli.main(
        [],
        stdin=StringIO(f"{_PASSWORD}\n{_PASSWORD}\n"),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue() == _hash() + "\n"
    assert _PASSWORD not in stdout.getvalue()
    assert stderr.getvalue() == ""


class _InteractiveInput(StringIO):
    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        del size
        raise AssertionError("interactive password input must not wait for trailing data")


def test_password_cli_suppresses_interactive_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presented = iter((_PASSWORD, _PASSWORD))
    interactive_input = _InteractiveInput()
    prompts: list[str] = []

    def read_without_echo(prompt: str) -> str:
        prompts.append(prompt)
        return next(presented)

    monkeypatch.setattr(sys, "stdin", interactive_input)
    monkeypatch.setattr(admin_password_cli, "getpass", read_without_echo)
    monkeypatch.setattr(
        admin_password_cli,
        "hash_password",
        lambda password: _hash(password),
    )
    stdout = StringIO()

    exit_code = admin_password_cli.main([], stdout=stdout, stderr=StringIO())

    assert exit_code == 0
    assert prompts == [
        "Administration password: ",
        "Confirm administration password: ",
    ]
    assert _PASSWORD not in stdout.getvalue()


def test_password_cli_handles_interactive_eof_without_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interactive_input = _InteractiveInput()

    def end_of_input(prompt: str) -> str:
        del prompt
        raise EOFError

    monkeypatch.setattr(sys, "stdin", interactive_input)
    monkeypatch.setattr(admin_password_cli, "getpass", end_of_input)
    stderr = StringIO()

    exit_code = admin_password_cli.main([], stdout=StringIO(), stderr=stderr)

    assert exit_code == 2
    assert stderr.getvalue() == "Invalid password input.\n"


@pytest.mark.parametrize(
    ("argv", "stdin_text"),
    [
        (["secret-on-argv"], ""),
        ([], "different password\nnot the same value\n"),
        ([], f"{_PASSWORD}\n{_PASSWORD}\nextra"),
        ([], "short\nshort\n"),
        ([], ("x" * 1_026) + "\n"),
    ],
)
def test_password_cli_rejects_unsafe_input_without_echo(
    argv: list[str],
    stdin_text: str,
) -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = admin_password_cli.main(
        argv,
        stdin=StringIO(stdin_text),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Invalid password input.\n"
    assert _PASSWORD not in stderr.getvalue()


class _BrokenOutput(StringIO):
    def write(self, value: str) -> int:
        super().write(value[:2])
        return 2


def test_password_cli_redacts_output_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin_password_cli,
        "hash_password",
        lambda password: _hash(password),
    )
    stderr = StringIO()

    exit_code = admin_password_cli.main(
        [],
        stdin=StringIO(f"{_PASSWORD}\n{_PASSWORD}\n"),
        stdout=_BrokenOutput(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert stderr.getvalue() == "Password hash output failed.\n"
    assert _PASSWORD not in stderr.getvalue()
