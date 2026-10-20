from __future__ import annotations

import pytest

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
