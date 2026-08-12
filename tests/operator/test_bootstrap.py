from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.models import (
    AuditEvent,
    BootstrapMarker,
    Caller,
    Credential,
    SectionGrant,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    BootstrapGrant,
    OperatorBootstrap,
    SectionAction,
)
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewSection
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.operator.service import (
    BootstrapAlreadyCompletedError,
    OperatorBootstrapService,
)


@pytest.fixture
def operator_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = build_engine(f"sqlite:///{(tmp_path / 'operator-bootstrap.db').as_posix()}")
    Caller.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def operator_scopes(operator_engine: Engine) -> tuple[str, str, str]:
    identifiers: Iterator[str] = iter(("1" * 32, "2" * 32, "3" * 32))
    with immediate_transaction(operator_engine) as connection:
        structure = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Operator Library",
                section_name="Synthetic Granted Section",
                book_name="Synthetic Operator Book",
            )
        )
        LibraryRepository(connection).add_section(
            NewSection(
                id="4" * 32,
                library_id=structure.library.id,
                name="Synthetic Hidden Section",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    return structure.library.id, structure.section.id, "4" * 32


def test_concurrent_bootstrap_creates_exactly_one_complete_result(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
) -> None:
    library_id, granted_section, _ = operator_scopes
    barrier = Barrier(2)
    request = OperatorBootstrap(
        library_id=library_id,
        operator_name="Concurrent Synthetic Operator",
        credential_expires_at=20_000_000,
        request_id="req_concurrent_bootstrap",
        initial_grants=tuple(
            BootstrapGrant(section_id=granted_section, action=action) for action in SectionAction
        ),
    )

    def attempt() -> str:
        barrier.wait()
        try:
            with immediate_transaction(operator_engine) as connection:
                result = OperatorBootstrapService(
                    AuthRepository(connection),
                    clock=lambda: 2_000_000,
                ).bootstrap(request)
            assert result.credential.value.startswith("plb1.")
            return "created"
        except BootstrapAlreadyCompletedError:
            return "already-complete"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(attempt), executor.submit(attempt))
        outcomes = [future.result() for future in futures]

    assert sorted(outcomes) == ["already-complete", "created"]
    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(SectionGrant)) == 3
        assert connection.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_bootstrap_is_permanent_and_never_replays_initial_secret(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
) -> None:
    library_id, _, _ = operator_scopes
    request = OperatorBootstrap(
        library_id=library_id,
        operator_name="One-Time Synthetic Operator",
        credential_expires_at=20_000_000,
        request_id="req_one_time_bootstrap",
    )
    with immediate_transaction(operator_engine) as connection:
        first = OperatorBootstrapService(
            AuthRepository(connection),
            clock=lambda: 2_000_000,
        ).bootstrap(request)

    with (
        pytest.raises(BootstrapAlreadyCompletedError) as exc_info,
        immediate_transaction(operator_engine) as connection,
    ):
        OperatorBootstrapService(
            AuthRepository(connection),
            clock=lambda: 3_000_000,
        ).bootstrap(request)

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert first.credential.value not in rendered
    assert first.credential.value not in repr(first)
    assert "<redacted>" in repr(first)


def test_bootstrap_failure_rolls_back_marker_identity_credential_grants_and_audit(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
) -> None:
    library_id, _, _ = operator_scopes
    request = OperatorBootstrap(
        library_id=library_id,
        operator_name="Rollback Synthetic Operator",
        credential_expires_at=20_000_000,
        request_id="req_bootstrap_rollback",
        initial_grants=(BootstrapGrant(section_id="f" * 32, action=SectionAction.QUERY),),
    )
    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(operator_engine) as connection,
    ):
        OperatorBootstrapService(
            AuthRepository(connection),
            clock=lambda: 2_000_000,
        ).bootstrap(request)

    with operator_engine.connect() as connection:
        for model in (BootstrapMarker, Caller, Credential, SectionGrant, AuditEvent):
            assert connection.scalar(select(func.count()).select_from(model)) == 0
