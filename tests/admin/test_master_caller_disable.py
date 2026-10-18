"""Master-only identity disable revokes every Agent Token without erasing history."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from time import time, time_ns
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, func, select
from starlette.concurrency import run_in_threadpool as starlette_run_in_threadpool

import patchouli_lib.admin.router as admin_router
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import (
    AgentTokenValue,
    AuditEvent,
    Caller,
    Credential,
    MasterAuditEvent,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.auth.service import AuthenticationError, AuthenticationService, CredentialIssuer
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.tags.models import Tag
from patchouli_lib.tags.service import TagService

_ORIGIN = "https://admin.example.invalid"
_MASTER_TOKEN = "synthetic master token material for disable"
_SIGNING = "s" * 32


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": f"sqlite:///{(tmp_path / 'disable.db').as_posix()}",
                "admin_session_signing_secret": _SIGNING,
                "admin_session_ttl_seconds": 600,
            }
        )
    )
    Caller.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _post(
    client: TestClient, path: str, data: dict[str, str], *, origin: str = _ORIGIN
) -> Response:
    return client.post(path, data=data, headers={"Origin": origin})


def _setup(web: tuple[TestClient, Engine]) -> tuple[str, str, str, str, str]:
    client, engine = web
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli(_MASTER_TOKEN, now=1_000)
    assert _post(client, "/admin/login", {"password": _MASTER_TOKEN}).status_code == 303
    dashboard = client.get("/admin")
    match = re.search(r'name="csrf_token" value="([^"]+)"', dashboard.text)
    assert match is not None
    csrf = match.group(1)
    home = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": "Device Home"})
    other = _post(client, "/admin/libraries", {"csrf_token": csrf, "name": "Other Library"})
    assert home.status_code == other.status_code == 303
    home_id = home.headers["location"].rsplit("/", 1)[-1]
    other_id = other.headers["location"].rsplit("/", 1)[-1]
    issued = _post(
        client,
        "/admin/agents/create",
        {
            "csrf_token": csrf,
            "home_library_id": home_id,
            "agent_name": "Synthetic Device",
            "agent_description": "Synthetic description",
            "credential_ttl_seconds": "3600",
            "grants": f"{home_id}:write",
        },
    )
    assert issued.status_code == 200
    token = re.search(r'<code class="secret">(plb1\.[^<]+)</code>', issued.text)
    assert token is not None
    ids = re.findall(r"<dd>([^<]+)</dd>", issued.text)
    assert len(ids) == 3 and ids[0] == home_id
    return csrf, home_id, other_id, ids[1], token.group(1)


def test_disable_all_agent_tokens_preserves_identity_credentials_and_activity(
    web: tuple[TestClient, Engine],
) -> None:
    client, engine = web
    csrf, home_id, _other_id, caller_id, first_token = _setup(web)
    detail_path = f"/admin/libraries/{home_id}/callers/{caller_id}"
    disable_path = f"{detail_path}/disable"
    now = time_ns() // 1_000
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(home_id, caller_id)
        assert caller is not None
        second = CredentialIssuer(repository).issue(caller, expires_at=now + 3_600_000_000)
        tag, created = TagService(connection).create_tag(
            first_token,
            library_id=home_id,
            name="Synthetic history",
            request_id="req_synthetic_tag_1",
        )
        assert created
    second_token = second.value
    credential_ids = {second.credential.id}
    with engine.connect() as connection:
        credential_ids.update(
            connection.scalars(
                select(Credential.id).where(
                    Credential.library_id == home_id, Credential.caller_id == caller_id
                )
            )
        )
    assert len(credential_ids) == 2
    for token in (first_token, second_token):
        assert (
            client.get(
                "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )
    detail = client.get(f"{detail_path}?lang=zh-CN")
    assert detail.status_code == 200
    assert f'action="{disable_path}"' in detail.text
    assert "我确认此身份的所有 Token 都将停止工作" in detail.text
    assert "Synthetic history" in detail.text
    assert _post(client, disable_path, {"csrf_token": csrf}).status_code == 422
    assert (
        _post(client, disable_path, {"csrf_token": csrf, "confirm_disable": "no"}).status_code
        == 422
    )

    disabled = _post(client, disable_path, {"csrf_token": csrf, "confirm_disable": "yes"})
    assert disabled.status_code == 303
    assert disabled.headers["location"] == detail_path
    for token in (first_token, second_token):
        assert (
            client.get(
                "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 401
        )
    for credential_id in credential_ids:
        reveal = _post(
            client, f"{detail_path}/credentials/{credential_id}/reveal", {"csrf_token": csrf}
        )
        assert reveal.status_code == 410
        assert first_token not in reveal.text and second_token not in reveal.text
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(home_id, caller_id)
        assert caller is not None and caller.disabled_at is not None
        assert caller.policy_version == 2
        assert (
            set(connection.scalars(select(Credential.id).where(Credential.caller_id == caller_id)))
            == credential_ids
        )
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 0
        assert connection.scalar(select(func.count()).select_from(Tag).where(Tag.id == tag.id)) == 1
        assert (
            connection.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.actor_caller_id == caller_id)
            )
            == 1
        )
        events = connection.execute(
            select(
                MasterAuditEvent.action, MasterAuditEvent.target_type, MasterAuditEvent.target_id
            ).where(MasterAuditEvent.action == "auth.agent_identity.disable")
        ).all()
        assert [tuple(row) for row in events] == [
            ("auth.agent_identity.disable", "caller", caller_id)
        ]
        for token in (first_token, second_token):
            with pytest.raises(AuthenticationError):
                AuthenticationService(repository).authenticate(token)
    detail = client.get(f"{detail_path}?lang=zh-CN")
    identities = client.get("/admin/agents?lang=zh-CN")
    assert detail.status_code == identities.status_code == 200
    assert "身份已停用" in detail.text and "身份已停用" in identities.text
    assert "Synthetic history" in detail.text
    assert f'action="{disable_path}"' not in detail.text
    assert (
        _post(client, disable_path, {"csrf_token": csrf, "confirm_disable": "yes"}).status_code
        == 303
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(MasterAuditEvent)
                .where(MasterAuditEvent.action == "auth.agent_identity.disable")
            )
            == 1
        )


def test_disable_rejects_origin_csrf_wrong_home_and_operator(
    web: tuple[TestClient, Engine],
) -> None:
    client, engine = web
    csrf, home_id, other_id, caller_id, token = _setup(web)
    path = f"/admin/libraries/{home_id}/callers/{caller_id}/disable"
    form = {"csrf_token": csrf, "confirm_disable": "yes"}
    now = time_ns() // 1_000
    with immediate_transaction(engine) as connection:
        operator = AuthRepository(connection).add_caller(
            NewCaller(
                id="b" * 32,
                library_id=home_id,
                kind=CallerKind.OPERATOR,
                name="Synthetic Operator",
                description="",
                created_at=now,
                updated_at=now,
            )
        )
    assert _post(client, path, form, origin="https://elsewhere.invalid").status_code == 403
    assert _post(client, path, {**form, "csrf_token": "bad"}).status_code == 403
    assert _post(client, path, {**form, "extra": "value"}).status_code == 422
    assert (
        _post(client, f"/admin/libraries/{other_id}/callers/{caller_id}/disable", form).status_code
        == 404
    )
    assert (
        _post(client, f"/admin/libraries/{home_id}/callers/{operator.id}/disable", form).status_code
        == 404
    )
    assert (
        _post(client, f"/admin/libraries/{home_id}/callers/{'c' * 32}/disable", form).status_code
        == 404
    )
    assert (
        client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(MasterAuditEvent.id).where(
                    MasterAuditEvent.action == "auth.agent_identity.disable"
                )
            ).all()
            == []
        )


def test_disable_rechecks_master_generation_and_expiry(
    web: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = web
    csrf, home_id, _other_id, caller_id, token = _setup(web)
    path = f"/admin/libraries/{home_id}/callers/{caller_id}/disable"
    form = {"csrf_token": csrf, "confirm_disable": "yes"}
    with pytest.raises(AuthenticationError):
        AdminActionService(engine).disable_agent_as_master(
            home_id,
            caller_id,
            master_session=MasterAdminSession(
                expires_at=int(time()) - 1,
                csrf_token=csrf,
                identity_id="a" * 32,
                session_generation=1,
            ),
        )

    async def rotate_before_action(
        action: Callable[..., Any], *args: object, **kwargs: object
    ) -> object:
        with immediate_transaction(engine) as connection:
            assert (
                MasterTokenRepository(connection).rotate(
                    _MASTER_TOKEN, "synthetic replacement master token material", now=1_001
                )
                is not None
            )
        return await starlette_run_in_threadpool(action, *args, **kwargs)

    monkeypatch.setattr(admin_router, "run_in_threadpool", rotate_before_action)
    assert _post(client, path, form).status_code == 401
    assert _post(client, path, form).status_code == 401
    with engine.connect() as connection:
        caller = AuthRepository(connection).get_caller(home_id, caller_id)
        assert caller is not None and caller.disabled_at is None
        assert (
            connection.execute(
                select(MasterAuditEvent.id).where(
                    MasterAuditEvent.action == "auth.agent_identity.disable"
                )
            ).all()
            == []
        )
        assert (
            AuthenticationService(AuthRepository(connection)).authenticate(token).caller.id
            == caller_id
        )


def test_disable_rolls_back_when_master_audit_cannot_commit(
    web: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = web
    csrf, home_id, _other_id, caller_id, token = _setup(web)

    def reject_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic audit write failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    response = _post(
        client,
        f"/admin/libraries/{home_id}/callers/{caller_id}/disable",
        {"csrf_token": csrf, "confirm_disable": "yes"},
    )
    assert response.status_code == 500
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(home_id, caller_id)
        assert caller is not None and caller.disabled_at is None
        assert AuthenticationService(repository).authenticate(token).caller.id == caller_id
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 1
