"""Independent read/write Library scopes in master forms and lifecycle operations."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from sqlalchemy import insert, select
from test_routes import (
    AdminWeb,
    _credential_from,
    _initialize_master,
    _login_master,
    _metadata_from,
    _post,
)
from test_routes import (
    admin_web as admin_web,
)

from patchouli_lib.admin.contracts import MasterRotateAgentCredentialInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.auth.library_policy import target_library_grants_digest
from patchouli_lib.auth.models import (
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.library.models import Library


def _prepare(web: AdminWeb) -> tuple[str, str, str]:
    _initialize_master(web)
    _, csrf = _login_master(web)
    ids = []
    for name in ("Scope Alpha", "Scope Beta"):
        response = _post(web.client, "/admin/libraries", data={"csrf_token": csrf, "name": name})
        assert response.status_code == 303
        ids.append(response.headers["location"].rsplit("/", 1)[-1])
    return csrf, ids[0], ids[1]


def _issue(web: AdminWeb, csrf: str, **scope: object) -> tuple[str, str, str, str]:
    response = _post(
        web.client,
        "/admin/agents/create",
        data={
            "csrf_token": csrf,
            "agent_name": "Scope Device",
            "never_expires": "true",
            **scope,
        },
    )
    assert response.status_code == 200, response.text
    return (*_metadata_from(response.text), _credential_from(response.text))


def test_scope_form_requires_library_and_has_independent_selection_controls(
    admin_web: AdminWeb,
) -> None:
    _initialize_master(admin_web)
    _, csrf = _login_master(admin_web)
    page = admin_web.client.get("/admin/agents?lang=zh-CN")
    assert "请先创建一个知识库" in page.text
    assert 'action="/admin/agents/create"' not in page.text
    assert (
        _post(
            admin_web.client,
            "/admin/agents/create",
            data={
                "csrf_token": csrf,
                "agent_name": "Empty",
                "never_expires": "true",
                "read_mode": "exclude",
                "write_mode": "exclude",
            },
        ).status_code
        == 404
    )


def test_all_read_selected_write_tracks_future_library_and_rotation(admin_web: AdminWeb) -> None:
    csrf, first, second = _prepare(admin_web)
    page = admin_web.client.get("/admin/agents?lang=zh-CN")
    assert 'name="home_library_id"' not in page.text
    assert page.text.count("data-library-scope") == 2
    assert 'name="read_mode" value="exclude"' in page.text
    home, caller, credential, token = _issue(
        admin_web,
        csrf,
        read_mode="exclude",
        write_mode="include",
        write_libraries=[second],
    )
    assert home == first
    future = _post(
        admin_web.client, "/admin/libraries", data={"csrf_token": csrf, "name": "Future"}
    )
    future_id = future.headers["location"].rsplit("/", 1)[-1]
    identity = admin_web.client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    rights = {item["library_id"]: item["actions"] for item in identity.json()["library_grants"]}
    assert rights == {first: ["read"], second: ["read", "write"], future_id: ["read"]}
    cookie = admin_web.client.cookies.get("patchouli_admin_session")
    assert cookie is not None
    session = AdminSessionCodec(b"s" * 32, ttl_seconds=600).verify_master(cookie)
    assert session is not None
    replacement = AdminActionService(admin_web.engine).rotate_agent_credential_as_master(
        home,
        caller,
        credential,
        MasterRotateAgentCredentialInput(never_expires=True),
        master_session=session,
    )
    with admin_web.engine.connect() as connection:
        scope = AuthRepository(connection).get_credential_library_scope(
            home_library_id=home,
            caller_id=caller,
            credential_id=replacement.credential_id,
        )
        assert scope is not None
        assert scope.read_mode == "exclude" and scope.read_libraries == ()
        assert scope.write_mode == "include" and scope.write_libraries == (second,)


def test_scope_editor_cas_aba_csrf_and_legacy_exclusion(admin_web: AdminWeb) -> None:
    csrf, first, second = _prepare(admin_web)
    home, caller, credential, _ = _issue(admin_web, csrf)
    base = f"/admin/libraries/{home}/callers/{caller}/credentials/{credential}/grants"
    page = admin_web.client.get(base)
    assert 'name="expected_scope_revision" value="0"' in page.text
    original = {
        "csrf_token": csrf,
        "expected_scope_revision": "0",
        "read_mode": "exclude",
        "read_libraries": [second],
        "write_mode": "include",
    }
    assert _post(admin_web.client, base, data={**original, "csrf_token": "bad"}).status_code == 403
    assert (
        _post(admin_web.client, base, data=original, origin="https://other.invalid").status_code
        == 403
    )
    assert _post(admin_web.client, base, data=original).status_code == 303
    assert _post(admin_web.client, base, data=original).status_code == 409
    legacy = {
        "csrf_token": csrf,
        "expected_digest": target_library_grants_digest(home, caller, credential, first, (), 0),
        "read": "true",
    }
    assert _post(admin_web.client, base + f"/{first}", data=legacy).status_code == 409
    assert (
        _post(
            admin_web.client,
            base,
            data={
                "csrf_token": csrf,
                "expected_scope_revision": "1",
                "read_mode": "include",
                "write_mode": "include",
            },
        ).status_code
        == 303
    )
    assert _post(admin_web.client, base, data=original).status_code == 409
    stale = _post(admin_web.client, base + f"/{first}", data=legacy)
    assert stale.status_code == 409
    assert (
        "完整读取／写入范围编辑器" in stale.text or "complete read/write scope editor" in stale.text
    )
    with admin_web.engine.connect() as connection:
        assert connection.scalar(select(CredentialLibraryPolicy.scope_revision)) == 2
        assert connection.execute(select(CredentialLibraryGrant)).all() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"read_mode": "all"},
        {"read_libraries": ["f" * 32]},
        {"read_libraries": ["a" * 32, "a" * 32]},
    ],
)
def test_scope_creation_rejects_invalid_input_without_creating_agent(
    admin_web: AdminWeb,
    overrides: dict[str, object],
) -> None:
    csrf, _, _ = _prepare(admin_web)
    response = _post(
        admin_web.client,
        "/admin/agents/create",
        data={
            "csrf_token": csrf,
            "agent_name": "Rejected",
            "never_expires": "true",
            **overrides,
        },
    )
    assert response.status_code in (404, 422)
    with admin_web.engine.connect() as connection:
        assert connection.execute(select(Caller)).all() == []


def test_scope_noop_and_failed_audit_leave_policy_unchanged(
    admin_web: AdminWeb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf, _, second = _prepare(admin_web)
    home, caller, credential, _ = _issue(admin_web, csrf)
    base = f"/admin/libraries/{home}/callers/{caller}/credentials/{credential}/grants"
    form = {
        "csrf_token": csrf,
        "expected_scope_revision": "0",
        "read_mode": "include",
        "write_mode": "include",
    }
    assert _post(admin_web.client, base, data=form).status_code == 303
    with admin_web.engine.connect() as connection:
        assert connection.scalar(select(CredentialLibraryPolicy.scope_revision)) == 0

    def reject_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    failed = _post(
        admin_web.client, base, data={**form, "read_mode": "exclude", "write_libraries": [second]}
    )
    assert failed.status_code == 500
    assert "Synthetic audit failure" not in failed.text
    with admin_web.engine.connect() as connection:
        policy = connection.execute(select(CredentialLibraryPolicy)).mappings().one()
        assert policy["scope_revision"] == 0 and policy["read_mode"] == "include"
        assert connection.execute(select(CredentialLibraryGrant)).all() == []


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("library_count", [127, 160])
def test_large_scope_forms_preserve_selections_and_effective_grants(
    admin_web: AdminWeb, operation: str, library_count: int
) -> None:
    _initialize_master(admin_web)
    _, csrf = _login_master(admin_web)
    library_ids = [f"{index:032x}" for index in range(1, library_count + 1)]
    with admin_web.engine.begin() as connection:
        connection.execute(
            insert(Library),
            [
                {"id": library_id, "name": f"Library {index}", "created_at": 1, "updated_at": 1}
                for index, library_id in enumerate(library_ids)
            ],
        )
    fields: dict[str, object] = {
        "csrf_token": csrf,
        "read_mode": "include",
        "write_mode": "include",
        "read_libraries": library_ids,
        "write_libraries": library_ids,
    }
    if operation == "create":
        path = "/admin/agents/create"
        fields.update(agent_name="Large scope", never_expires="true")
    else:
        home, caller, credential, _ = _issue(admin_web, csrf)
        path = f"/admin/libraries/{home}/callers/{caller}/credentials/{credential}/grants"
        fields["expected_scope_revision"] = "0"
    encoded = urlencode(fields, doseq=True)
    assert encoded.count("&") + 1 > 256
    assert len(encoded.encode()) < 16_384
    response = _post(admin_web.client, path, data=fields)
    assert response.status_code == (200 if operation == "create" else 303), response.text
    if operation == "create":
        home, caller, credential = _metadata_from(response.text)
    with admin_web.engine.connect() as connection:
        repository = AuthRepository(connection)
        scope = repository.get_credential_library_scope(home, caller, credential)
        assert scope is not None
        assert scope.read_mode == scope.write_mode == "include"
        assert scope.read_libraries == scope.write_libraries == tuple(library_ids)
        assert scope.revision == (0 if operation == "create" else 1)
        grants = repository.list_credential_library_grants(
            home_library_id=home, caller_id=caller, credential_id=credential
        )
        assert {item.library_id: item.actions for item in grants} == {
            library_id: ("read", "write") for library_id in library_ids
        }


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("invalid", ["oversized", "duplicate"])
def test_scope_forms_keep_byte_and_duplicate_limits_without_mutation(
    admin_web: AdminWeb, operation: str, invalid: str
) -> None:
    csrf, _, second = _prepare(admin_web)
    fields = {
        "csrf_token": csrf,
        "read_mode": "exclude",
        "write_mode": "include",
        "write_libraries": second,
    }
    if operation == "create":
        path = "/admin/agents/create"
        fields.update(agent_name="Rejected scope", never_expires="true")
    else:
        home, caller, credential, _ = _issue(admin_web, csrf)
        path = f"/admin/libraries/{home}/callers/{caller}/credentials/{credential}/grants"
        fields["expected_scope_revision"] = "0"
    encoded = urlencode(fields)
    encoded += "&read_mode=" + ("x" * 16_384 if invalid == "oversized" else "include")
    models = (Caller, Credential, CredentialLibraryPolicy, CredentialLibraryGrant)
    with admin_web.engine.connect() as connection:
        before = [connection.execute(select(model)).all() for model in models]
    response = admin_web.client.post(
        path,
        content=encoded,
        headers={
            "Origin": "https://admin.example.invalid",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == (413 if invalid == "oversized" else 422)
    with admin_web.engine.connect() as connection:
        assert [connection.execute(select(model)).all() for model in models] == before
