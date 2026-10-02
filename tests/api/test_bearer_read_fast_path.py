"""Read admission avoids writer reservation and write fallback rechecks authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Connection, event, select, update
from test_auth_routes import AuthApiFixture, _raw_request
from test_auth_routes import auth_api as auth_api

from patchouli_lib.api import authentication as authentication_module
from patchouli_lib.api.authentication import AuthenticatedRequestContext, BearerAuthentication
from patchouli_lib.api.errors import ApplicationProblem
from patchouli_lib.auth.models import Caller, Credential
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuthenticatedCaller, SectionAction
from patchouli_lib.database import immediate_transaction


def _authenticate(fixture: AuthApiFixture) -> AuthenticatedRequestContext:
    request = _raw_request([(b"authorization", f"Bearer {fixture.agent_token}".encode("ascii"))])
    return BearerAuthentication(fixture.engine, clock=fixture.clock)(request)


def _last_used(fixture: AuthApiFixture) -> int | None:
    with fixture.engine.connect() as connection:
        return connection.execute(
            select(Credential.last_used_at).where(Credential.id == fixture.agent_credential_id)
        ).scalar_one()


def test_coalesce_boundary_and_clock_regression_reserve_only_due_writes(
    auth_api: AuthApiFixture,
) -> None:
    begins: list[str] = []

    def record(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        if statement in {"BEGIN", "BEGIN IMMEDIATE"}:
            begins.append(statement)

    event.listen(auth_api.engine, "before_cursor_execute", record)
    _authenticate(auth_api)
    assert begins == ["BEGIN", "BEGIN IMMEDIATE"]
    assert _last_used(auth_api) == 400_000_000
    for now in (699_999_999, 250_000_000):
        begins.clear()
        auth_api.clock.value = now
        _authenticate(auth_api)
        assert begins == ["BEGIN"]
        assert _last_used(auth_api) == 400_000_000
    begins.clear()
    auth_api.clock.value = 700_000_000
    _authenticate(auth_api)
    assert begins == ["BEGIN", "BEGIN IMMEDIATE"]
    assert _last_used(auth_api) == 700_000_000


def test_twenty_coalesced_reads_finish_with_a_reserved_writer_and_see_one_snapshot(
    auth_api: AuthApiFixture,
) -> None:
    _authenticate(auth_api)
    auth_api.clock.value = 500_000_000

    def short_timeout(raw: sqlite3.Connection, _record: object) -> None:
        raw.execute("PRAGMA busy_timeout = 50")

    event.listen(auth_api.engine, "connect", short_timeout)
    with immediate_transaction(auth_api.engine) as writer:
        repository = AuthRepository(writer)
        assert repository.remove_grant(
            auth_api.library_id,
            auth_api.agent_caller_id,
            auth_api.first_section_id,
            SectionAction.PAGE_READ,
        )
        stored = repository.get_credential(
            auth_api.library_id, auth_api.agent_caller_id, auth_api.agent_credential_id
        )
        assert stored is not None
        repository.revoke_credential(stored, revoked_at=500_000_000)
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(_authenticate, auth_api) for _ in range(20)]
            results = [future.result(timeout=2) for future in futures]
        assert all(
            any(grant.action is SectionAction.PAGE_READ for grant in context.grants)
            and context.authenticated.credential.revoked_at is None
            for context in results
        )
    with pytest.raises(ApplicationProblem) as rejected:
        _authenticate(auth_api)
    assert rejected.value.code == "invalid_token"
    assert _last_used(auth_api) == 400_000_000


def test_fast_read_keeps_caller_and_grants_in_one_snapshot_during_committed_revocation(
    auth_api: AuthApiFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _authenticate(auth_api)
    auth_api.clock.value = 500_000_000
    # Synthetic WAL lets the writer commit while the tested reader stays open.
    # This is not a change to the application's journal configuration.
    with auth_api.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode = WAL").scalar_one() == "wal"
    original = BearerAuthentication._context

    def concurrent_change(
        connection: Connection, authenticated: AuthenticatedCaller
    ) -> AuthenticatedRequestContext:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection) and raw.in_transaction
        with immediate_transaction(auth_api.engine) as writer:
            repository = AuthRepository(writer)
            stored = repository.get_credential(
                auth_api.library_id, auth_api.agent_caller_id, auth_api.agent_credential_id
            )
            assert stored is not None
            repository.revoke_credential(stored, revoked_at=500_000_000)
            assert repository.remove_grant(
                auth_api.library_id,
                auth_api.agent_caller_id,
                auth_api.first_section_id,
                SectionAction.PAGE_READ,
            )
        return original(connection, authenticated)

    with monkeypatch.context() as context:
        context.setattr(BearerAuthentication, "_context", staticmethod(concurrent_change))
        admitted = _authenticate(auth_api)
    assert admitted.authenticated.credential.revoked_at is None
    assert any(grant.action is SectionAction.PAGE_READ for grant in admitted.grants)
    with pytest.raises(ApplicationProblem) as rejected:
        _authenticate(auth_api)
    assert rejected.value.code == "invalid_token"


@pytest.mark.parametrize("change", ["revoke", "disable", "expire", "grants"])
def test_due_fallback_closes_reader_and_reauthenticates_with_current_grants(
    auth_api: AuthApiFixture, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    active = 0
    entries = 0

    def checkout(_raw: object, _record: object, _proxy: object) -> None:
        nonlocal active
        active += 1

    def checkin(_raw: object, _record: object) -> None:
        nonlocal active
        active -= 1

    event.listen(auth_api.engine, "checkout", checkout)
    event.listen(auth_api.engine, "checkin", checkin)

    @contextmanager
    def changed_writer(_engine: object) -> Iterator[Connection]:
        nonlocal entries
        assert active == 0  # No read snapshot or checked-out connection survives fallback.
        entries += 1
        with immediate_transaction(auth_api.engine) as connection:
            raw = connection.connection.driver_connection
            assert isinstance(raw, sqlite3.Connection) and raw.in_transaction
            if change == "revoke":
                connection.execute(
                    update(Credential)
                    .where(Credential.id == auth_api.agent_credential_id)
                    .values(revoked_at=400_000_000, updated_at=400_000_000)
                )
            elif change == "disable":
                connection.execute(
                    update(Caller)
                    .where(Caller.id == auth_api.agent_caller_id)
                    .values(disabled_at=400_000_000, updated_at=400_000_000)
                )
            elif change == "expire":
                connection.execute(
                    update(Credential)
                    .where(Credential.id == auth_api.agent_credential_id)
                    .values(expires_at=400_000_000)
                )
            else:
                assert AuthRepository(connection).remove_grant(
                    auth_api.library_id,
                    auth_api.agent_caller_id,
                    auth_api.first_section_id,
                    SectionAction.PAGE_READ,
                )
            yield connection

    monkeypatch.setattr(authentication_module, "immediate_transaction", changed_writer)
    if change == "grants":
        context = _authenticate(auth_api)
        assert not any(grant.action is SectionAction.PAGE_READ for grant in context.grants)
        assert _last_used(auth_api) == 400_000_000
    else:
        with pytest.raises(ApplicationProblem) as rejected:
            _authenticate(auth_api)
        assert rejected.value.code == "invalid_token"
        assert _last_used(auth_api) is None
    assert entries == 1 and active == 0
