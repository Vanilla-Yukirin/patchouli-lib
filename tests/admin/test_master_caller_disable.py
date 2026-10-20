"""Disabling an identity preserves history and atomically invalidates every Token."""

from time import time, time_ns

import pytest
from helpers import MASTER_TOKEN, ORIGIN, AdminWeb, create_structure
from helpers import admin_web as admin_web
from sqlalchemy import func, select

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.models import AgentTokenValue, AuditEvent, Credential, MasterAuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.service import AuthenticationError, AuthenticationService, CredentialIssuer
from patchouli_lib.database import immediate_transaction
from patchouli_lib.tags.models import Tag
from patchouli_lib.tags.service import TagService


def issue(web: AdminWeb) -> dict[str, str]:
    library_id, _, _ = create_structure(web)
    value = web.post(
        "/admin/api/credentials",
        {
            "agent_name": "Synthetic Device",
            "credential_ttl_seconds": 3600,
            "read_mode": "include",
            "write_mode": "include",
            "write_libraries": [library_id],
        },
    )
    assert value.status_code == 201, value.text
    return dict(value.json())


def test_disable_all_agent_tokens_preserves_identity_credentials_and_activity(
    admin_web: AdminWeb,
) -> None:
    web = admin_web
    issued = issue(web)
    home, caller_id, token = issued["library_id"], issued["caller_id"], issued["token"]
    with immediate_transaction(web.engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(home, caller_id)
        assert caller is not None
        second = CredentialIssuer(repository).issue(
            caller, expires_at=time_ns() // 1000 + 3_600_000_000
        )
        tag, created = TagService(connection).create_tag(
            token, library_id=home, name="Preserved history", request_id="req_synthetic_tag_1"
        )
        assert created
    path = f"/admin/api/credentials/{caller_id}/disable?library_id={home}"
    assert web.post(path).status_code == 200
    assert web.post(path).status_code == 200
    for value in (token, second.value):
        assert (
            web.client.get(
                "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {value}"}
            ).status_code
            == 401
        )
    for credential in (issued["credential_id"], second.credential.id):
        reveal = web.post(
            f"/admin/api/credentials/{caller_id}/{credential}/reveal?library_id={home}"
        )
        assert (
            reveal.status_code == 410
            and token not in reveal.text
            and second.value not in reveal.text
        )
    with web.engine.connect() as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(home, caller_id)
        assert caller is not None and caller.disabled_at is not None and caller.policy_version == 2
        assert set(
            connection.scalars(select(Credential.id).where(Credential.caller_id == caller_id))
        ) == {issued["credential_id"], second.credential.id}
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
            select(MasterAuditEvent.action, MasterAuditEvent.target_id).where(
                MasterAuditEvent.action == "auth.agent_identity.disable"
            )
        ).all()
        assert [tuple(row) for row in events] == [("auth.agent_identity.disable", caller_id)]


def test_disable_rechecks_scope_generation_and_expiry(admin_web: AdminWeb) -> None:
    web = admin_web
    issued = issue(web)
    home, caller_id = issued["library_id"], issued["caller_id"]
    path = f"/admin/api/credentials/{caller_id}/disable?library_id={home}"
    assert (
        web.client.post(
            path, json={}, headers={**web.headers, "Origin": "https://other.invalid"}
        ).status_code
        == 403
    )
    assert web.client.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert web.post(path.replace(home, "f" * 32)).status_code == 404
    service = AdminActionService(web.engine)
    expired = MasterAdminSession(
        expires_at=int(time()) - 1,
        csrf_token=web.csrf,
        identity_id=web.session.identity_id,
        session_generation=web.session.session_generation,
    )
    with pytest.raises(AuthenticationError):
        service.disable_agent_as_master(home, caller_id, master_session=expired)
    with immediate_transaction(web.engine) as connection:
        MasterTokenRepository(connection).rotate(
            MASTER_TOKEN, MASTER_TOKEN + " rotated", now=time_ns() // 1000
        )
    with pytest.raises(AuthenticationError):
        service.disable_agent_as_master(home, caller_id, master_session=web.session)
    assert web.post(path).status_code == 401
    with web.engine.connect() as connection:
        repository = AuthRepository(connection)
        assert (
            AuthenticationService(repository).authenticate(issued["token"]).caller.disabled_at
            is None
        )


def test_disable_rolls_back_when_master_audit_cannot_commit(
    admin_web: AdminWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = admin_web
    issued = issue(web)

    def reject(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic private audit error")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject)
    response = web.post(
        f"/admin/api/credentials/{issued['caller_id']}/disable?library_id={issued['library_id']}"
    )
    assert response.status_code == 500 and "synthetic private audit error" not in response.text
    with web.engine.connect() as connection:
        repository = AuthRepository(connection)
        assert (
            AuthenticationService(repository).authenticate(issued["token"]).caller.disabled_at
            is None
        )
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 1
