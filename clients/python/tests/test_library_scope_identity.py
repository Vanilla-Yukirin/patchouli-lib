from __future__ import annotations

import pytest

from patchouli_client import LibraryScope, ProtocolError, WhoAmI
from patchouli_mcp.server import _jsonable


def _identity() -> dict[str, object]:
    return {
        "caller_id": "caller",
        "credential_id": "credential",
        "kind": "agent",
        "expires_at": None,
        "policy_version": 1,
        "grants": [],
        "policy_mode": "library_grants",
        "library_grants": [{"library_id": "one", "actions": ["read"]}],
    }


def test_scope_is_optional_for_old_servers_and_preserved_for_new_servers() -> None:
    payload = _identity()
    assert WhoAmI.from_dict(payload).library_scope is None
    scope = {
        "read_mode": "exclude",
        "write_mode": "include",
        "read_libraries": ["excluded"],
        "write_libraries": ["one"],
        "revision": 3,
    }
    payload["library_scope"] = scope
    result = WhoAmI.from_dict(payload)
    assert result.library_scope == LibraryScope("exclude", "include", ("excluded",), ("one",), 3)
    wire = _jsonable(result)
    assert isinstance(wire, dict)
    assert wire["library_scope"] == scope
    assert "credential_id" not in wire


@pytest.mark.parametrize(
    "field,value",
    [
        ("read_mode", "all"),
        ("write_mode", "all"),
        ("read_libraries", [""]),
        ("write_libraries", [""]),
        ("revision", -1),
        ("revision", True),
    ],
)
def test_invalid_scope_is_rejected(field: str, value: object) -> None:
    scope: dict[str, object] = {
        "read_mode": "include",
        "write_mode": "exclude",
        "read_libraries": [],
        "write_libraries": [],
        "revision": 0,
    }
    scope[field] = value
    with pytest.raises(ProtocolError):
        LibraryScope.from_dict(scope)
