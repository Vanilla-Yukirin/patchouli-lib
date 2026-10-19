from __future__ import annotations

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, func, select, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.contracts import MasterProvisionAgentInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.onboarding_routes import TicketAttemptLimiter, create_onboarding_router
from patchouli_lib.auth.models import (
    Caller,
    Credential,
    CredentialLibraryPolicy,
    MasterAuditEvent,
    MasterIdentity,
    OnboardingTicket,
)
from patchouli_lib.auth.onboarding import InvalidOnboardingTicket, OnboardingTicketService
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.database import immediate_transaction
from patchouli_lib.operator.service import ResourceNotFoundError

NOW = 2_000_000_000


@pytest.fixture
def master(auth_engine: Engine) -> MasterAdminSession:
    with immediate_transaction(auth_engine) as connection:
        state = MasterTokenRepository(connection).initialize_from_local_cli(
            "synthetic long master token for ticket tests", now=1_000_000
        )
    return MasterAdminSession(
        expires_at=10_000,
        csrf_token="synthetic session fingerprint",
        identity_id=state.identity_id,
        session_generation=1,
    )


def _request(name: str = "Synthetic device", **values: object) -> MasterProvisionAgentInput:
    return MasterProvisionAgentInput.model_validate(
        {"agent_name": name, "never_expires": True, **values}
    )


def _counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (Caller, Credential, CredentialLibraryPolicy)
        )


def test_ticket_only_persists_hash_and_expires_exactly(
    auth_engine: Engine, master: MasterAdminSession, scoped_library: tuple[str, str, str]
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    assert len(ticket.code) == 16 and ticket.code.isascii() and ticket.code.isalnum()
    assert ticket.code not in repr(ticket)
    assert ticket.expires_at == NOW + 300_000_000
    with auth_engine.connect() as connection:
        row = connection.execute(select(OnboardingTicket.__table__)).mappings().one()
        assert row["code_hash"] == hashlib.sha256(ticket.code.encode()).digest()
        assert ticket.code not in repr(dict(row))
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 1
    assert service.discover(ticket.code) == (
        {"id": scoped_library[0], "name": "Synthetic Auth Library"},
    )
    OnboardingTicketService(auth_engine, clock=lambda: ticket.expires_at - 1).discover(ticket.code)
    with pytest.raises(InvalidOnboardingTicket):
        OnboardingTicketService(auth_engine, clock=lambda: ticket.expires_at).discover(ticket.code)
    with pytest.raises(InvalidOnboardingTicket):
        OnboardingTicketService(auth_engine, clock=lambda: NOW - 1).discover(ticket.code)


def test_one_use_and_independent_modes(
    auth_engine: Engine, master: MasterAdminSession, scoped_library: tuple[str, str, str]
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    result = service.redeem(
        ticket.code,
        _request(read_mode="exclude", write_mode="include", write_libraries=[scoped_library[0]]),
    )
    assert result.value.startswith("plb1.")
    assert result.value not in repr(result)
    with auth_engine.connect() as connection:
        policy = connection.execute(select(CredentialLibraryPolicy.__table__)).mappings().one()
        assert policy["read_mode"] == "exclude" and policy["write_mode"] == "include"
        assert connection.scalar(select(Caller.kind)) == "agent"
        assert connection.scalar(select(Credential.expires_at)) is None
        assert connection.scalar(select(OnboardingTicket.credential_id)) == result.credential_id
    for action in (
        lambda: service.discover(ticket.code),
        lambda: service.redeem(ticket.code, _request("Another")),
    ):
        with pytest.raises(InvalidOnboardingTicket):
            action()
    assert _counts(auth_engine) == (1, 1, 1)


def test_concurrent_redemption_has_exactly_one_winner(
    auth_engine: Engine, master: MasterAdminSession, scoped_library: tuple[str, str, str]
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    barrier = Barrier(2)

    def attempt(index: int) -> bool:
        barrier.wait()
        try:
            service.redeem(ticket.code, _request(f"Device {index}"))
        except InvalidOnboardingTicket:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, (1, 2))) == [False, True]
    assert _counts(auth_engine) == (1, 1, 1)


def test_generation_change_invalidates_create_discovery_and_exchange(
    auth_engine: Engine, master: MasterAdminSession
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    with immediate_transaction(auth_engine) as connection:
        connection.execute(update(MasterIdentity).values(session_generation=2))
    with pytest.raises(AuthenticationError):
        service.create(master_session=master)
    with pytest.raises(InvalidOnboardingTicket):
        service.discover(ticket.code)
    with pytest.raises(InvalidOnboardingTicket):
        service.redeem(ticket.code, _request())


def test_no_library_and_unknown_library_do_not_consume(
    auth_engine: Engine, master: MasterAdminSession
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    with pytest.raises(ResourceNotFoundError):
        service.redeem(ticket.code, _request())
    assert service.discover(ticket.code) == ()
    assert _counts(auth_engine) == (0, 0, 0)


def test_audit_failure_rolls_back_credential_and_consumption(
    auth_engine: Engine,
    master: MasterAdminSession,
    scoped_library: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    original = MasterAuditRepository.add_success

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", fail)
    with pytest.raises(RuntimeError):
        service.redeem(ticket.code, _request())
    assert _counts(auth_engine) == (0, 0, 0)
    monkeypatch.setattr(MasterAuditRepository, "add_success", original)
    service.redeem(ticket.code, _request())


def test_bad_scope_and_duplicate_name_allow_later_valid_retry(
    auth_engine: Engine, master: MasterAdminSession, scoped_library: tuple[str, str, str]
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    with pytest.raises(ResourceNotFoundError):
        service.redeem(ticket.code, _request(read_libraries=["f" * 32]))
    assert _counts(auth_engine) == (0, 0, 0)
    service.redeem(ticket.code, _request())
    second = service.create(master_session=master)
    with pytest.raises(IntegrityError):
        service.redeem(second.code, _request())
    service.redeem(
        second.code, _request("Different device", never_expires=False, credential_ttl_seconds=60)
    )
    assert _counts(auth_engine) == (2, 2, 2)


def test_routes_redact_errors_and_limit_body_and_attempts(
    auth_engine: Engine,
    master: MasterAdminSession,
    scoped_library: tuple[str, str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    ticket = service.create(master_session=master)
    app = FastAPI()
    install_api_exception_handlers(app)
    app.include_router(create_onboarding_router(auth_engine, clock=lambda: NOW))
    with TestClient(app) as client:
        base = "/api/v1/onboarding"
        for data in (
            {"code": ticket.code, "name": "Device", "kind": "operator"},
            {"code": ticket.code, "name": "Device", "never_expires": "true"},
            {"code": ticket.code, "name": "Device"},
        ):
            response = client.post(base + "/redeem", json=data)
            assert response.status_code == 422
            assert ticket.code not in response.text
            assert "no-store" in response.headers["cache-control"]
        response = client.post(
            base + "/libraries", content=b"x" * 131073, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        assert client.post(base + "/libraries", content="bad").status_code == 415
        assert client.post(base + "/libraries", json={"code": "not-a-ticket"}).status_code == 401
        response = client.post(base + "/libraries", json={"code": ticket.code})
        assert response.status_code == 200
        assert set(response.json()["libraries"][0]) == {"id", "name"}
        response = client.post(
            base + "/redeem",
            json={
                "code": ticket.code,
                "name": "Device",
                "never_expires": True,
                "read_libraries": [scoped_library[0]],
            },
        )
        assert response.status_code == 201, response.text
        token = response.json()["token"]
        assert response.headers["cache-control"] == "no-store"
        assert token not in caplog.text and ticket.code not in caplog.text
        assert client.post(base + "/libraries", json={"code": ticket.code}).status_code == 401
        for _ in range(60):
            response = client.post(base + "/libraries", json={"code": "invalid"})
        assert response.status_code == 429


def test_limiter_expires_without_growing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    import patchouli_lib.api.onboarding_routes as routes

    now = [1.0]
    monkeypatch.setattr(routes, "monotonic", lambda: now[0])
    limiter = TicketAttemptLimiter(limit=2)
    assert limiter.admit() and limiter.admit() and not limiter.admit()
    now[0] = 61
    assert limiter.admit()


def test_expired_master_session_cannot_issue_ticket(
    auth_engine: Engine, master: MasterAdminSession
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: master.expires_at * 1_000_000)
    with pytest.raises(AuthenticationError):
        service.create(master_session=master)
    with auth_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(OnboardingTicket)) == 0


@pytest.mark.parametrize("expires_while_waiting", [False, True])
def test_creation_observes_time_after_write_lock_acquisition(
    auth_engine: Engine,
    master: MasterAdminSession,
    monkeypatch: pytest.MonkeyPatch,
    expires_while_waiting: bool,
) -> None:
    import patchouli_lib.auth.onboarding as onboarding

    before_lock = master.expires_at * 1_000_000 - 2_000_000
    after_lock = before_lock + (2_000_000 if expires_while_waiting else 1_000_000)
    now = [before_lock]

    @contextmanager
    def delayed_transaction(engine: Engine) -> Iterator[Connection]:
        with immediate_transaction(engine) as connection:
            # Deterministically model the clock advancing while BEGIN IMMEDIATE
            # waits for another writer, without a timing-dependent sleep.
            now[0] = after_lock
            yield connection

    monkeypatch.setattr(onboarding, "immediate_transaction", delayed_transaction)
    service = OnboardingTicketService(auth_engine, clock=lambda: now[0])
    if expires_while_waiting:
        with pytest.raises(AuthenticationError):
            service.create(master_session=master)
        with auth_engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(OnboardingTicket)) == 0
    else:
        ticket = service.create(master_session=master)
        assert ticket.expires_at == after_lock + 300_000_000
        with auth_engine.connect() as connection:
            assert connection.scalar(select(OnboardingTicket.created_at)) == after_lock


def test_route_failures_preserve_ticket_and_return_safe_problems(
    auth_engine: Engine, master: MasterAdminSession, scoped_library: tuple[str, str, str]
) -> None:
    service = OnboardingTicketService(auth_engine, clock=lambda: NOW)
    app = FastAPI()
    install_api_exception_handlers(app)
    app.include_router(create_onboarding_router(auth_engine, clock=lambda: NOW))
    with TestClient(app) as client:
        path = "/api/v1/onboarding/redeem"
        ticket = service.create(master_session=master)
        payload = {"code": ticket.code, "name": "Device", "never_expires": True}
        response = client.post(path, json={**payload, "read_libraries": ["f" * 32]})
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_library_scope"
        assert ticket.code not in response.text
        response = client.post(
            path, json={**payload, "never_expires": False, "credential_ttl_seconds": 253402300799}
        )
        assert response.status_code == 422
        assert _counts(auth_engine) == (0, 0, 0)
        assert client.post(path, json=payload).status_code == 201
        assert client.post(path, json=payload).status_code == 401
        second = service.create(master_session=master)
        duplicate = client.post(path, json={**payload, "code": second.code})
        assert duplicate.status_code == 409
        assert second.code not in duplicate.text
        assert (
            client.post(path, json={**payload, "code": second.code, "name": "Renamed"}).status_code
            == 201
        )
        malformed = client.post(
            path,
            content=b'{"code":"' + ticket.code.encode(),
            headers={"Content-Type": "application/json"},
        )
        assert malformed.status_code == 422 and ticket.code not in malformed.text
