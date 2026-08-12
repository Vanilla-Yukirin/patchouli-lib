from __future__ import annotations

import pytest
from sqlalchemy import Engine, delete, insert, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.models import AuditEvent, Caller, Credential, SectionGrant
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuditOutcome,
    CallerKind,
    NewAuditEvent,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService


def _add_caller(
    repository: AuthRepository,
    *,
    caller_id: str,
    library_id: str,
    kind: CallerKind = CallerKind.AGENT,
) -> None:
    repository.add_caller(
        NewCaller(
            id=caller_id,
            library_id=library_id,
            kind=kind,
            name=f"Synthetic {caller_id[0]} Caller",
            created_at=2_000_000,
            updated_at=2_000_000,
        )
    )


def _add_credential(
    repository: AuthRepository,
    *,
    credential_id: str,
    caller_id: str,
    library_id: str,
) -> str:
    issued = generate_token()
    repository.add_credential(
        NewCredential(
            id=credential_id,
            library_id=library_id,
            caller_id=caller_id,
            selector=issued.selector,
            token_version=issued.version,
            verifier=issued.verifier,
            expires_at=4_000_000,
            created_at=2_000_000,
            updated_at=2_000_000,
        )
    )
    return issued.value


def _seed_second_library(engine: Engine) -> tuple[str, str]:
    identifiers = iter(("4" * 32, "5" * 32, "6" * 32))
    with immediate_transaction(engine) as connection:
        result = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name="Second Synthetic Auth Library",
                section_name="Second Synthetic Section",
                book_name="Second Synthetic Book",
            )
        )
    return result.library.id, result.section.id


def test_two_connections_enforce_orphan_credential_foreign_key(
    auth_engine: Engine,
) -> None:
    for index, connection in enumerate(
        (auth_engine.connect(), auth_engine.connect()),
        start=1,
    ):
        try:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            issued = generate_token()
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                connection.execute(
                    insert(Credential),
                    {
                        "id": str(index) * 32,
                        "library_id": "a" * 32,
                        "caller_id": "b" * 32,
                        "selector": issued.selector,
                        "token_version": issued.version,
                        "verifier": issued.verifier,
                        "expires_at": 2_000_000,
                        "created_at": 1_000_000,
                        "updated_at": 1_000_000,
                    },
                )
            connection.rollback()
        finally:
            connection.close()


def test_composite_foreign_keys_reject_cross_library_credential_and_grant(
    auth_engine: Engine,
    scoped_library: tuple[str, str, str],
) -> None:
    first_library, first_section, _ = scoped_library
    second_library, second_section = _seed_second_library(auth_engine)
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        _add_caller(repository, caller_id="7" * 32, library_id=first_library)

    issued = generate_token()
    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(auth_engine) as connection,
    ):
        AuthRepository(connection).add_credential(
            NewCredential(
                id="8" * 32,
                library_id=second_library,
                caller_id="7" * 32,
                selector=issued.selector,
                token_version=issued.version,
                verifier=issued.verifier,
                expires_at=4_000_000,
                created_at=2_000_000,
                updated_at=2_000_000,
            )
        )

    for library_id, section_id in (
        (second_library, first_section),
        (first_library, second_section),
    ):
        with (
            pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
            immediate_transaction(auth_engine) as connection,
        ):
            AuthRepository(connection).add_grant(
                NewSectionGrant(
                    library_id=library_id,
                    caller_id="7" * 32,
                    section_id=section_id,
                    action=SectionAction.QUERY,
                    created_at=2_000_000,
                )
            )

    with auth_engine.connect() as connection:
        assert connection.scalar(select(SectionGrant)) is None


def test_audit_actor_credential_scope_is_enforced(
    auth_engine: Engine,
    scoped_library: tuple[str, str, str],
) -> None:
    first_library, first_section, _ = scoped_library
    second_library, second_section = _seed_second_library(auth_engine)
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        _add_caller(repository, caller_id="7" * 32, library_id=first_library)
        _add_caller(repository, caller_id="8" * 32, library_id=second_library)
        _add_credential(
            repository,
            credential_id="9" * 32,
            caller_id="7" * 32,
            library_id=first_library,
        )

    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(auth_engine) as connection,
    ):
        AuthRepository(connection).add_audit_event(
            NewAuditEvent(
                id="a" * 32,
                library_id=second_library,
                actor_caller_id="8" * 32,
                actor_credential_id="9" * 32,
                action="auth.synthetic",
                resource_type="caller",
                resource_id="8" * 32,
                outcome=AuditOutcome.SUCCEEDED,
                request_id="req_cross_scope",
                occurred_at=2_000_000,
            )
        )

    for target_caller_id, section_id in (
        ("8" * 32, first_section),
        ("7" * 32, second_section),
    ):
        with (
            pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
            immediate_transaction(auth_engine) as connection,
        ):
            AuthRepository(connection).add_audit_event(
                NewAuditEvent(
                    id="b" * 32,
                    library_id=first_library,
                    actor_caller_id="7" * 32,
                    actor_credential_id="9" * 32,
                    target_caller_id=target_caller_id,
                    section_id=section_id,
                    section_action=SectionAction.QUERY,
                    action="auth.grant.add",
                    resource_type="section_grant",
                    resource_id=section_id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id="req_cross_scope_grant_audit",
                    occurred_at=2_000_000,
                )
            )


def test_auth_parents_and_credentials_use_restrict_deletes(
    auth_engine: Engine,
    scoped_library: tuple[str, str, str],
) -> None:
    library_id, section_id, _ = scoped_library
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        _add_caller(repository, caller_id="7" * 32, library_id=library_id)
        _add_credential(
            repository,
            credential_id="8" * 32,
            caller_id="7" * 32,
            library_id=library_id,
        )
        repository.add_grant(
            NewSectionGrant(
                library_id=library_id,
                caller_id="7" * 32,
                section_id=section_id,
                action=SectionAction.QUERY,
                created_at=2_000_000,
            )
        )
        repository.add_audit_event(
            NewAuditEvent(
                id="9" * 32,
                library_id=library_id,
                actor_caller_id="7" * 32,
                actor_credential_id="8" * 32,
                action="auth.synthetic",
                resource_type="caller",
                resource_id="7" * 32,
                outcome=AuditOutcome.SUCCEEDED,
                request_id="req_restrict",
                occurred_at=2_000_000,
            )
        )

    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(auth_engine) as connection,
    ):
        connection.execute(delete(Caller).where(Caller.id == "7" * 32))

    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(auth_engine) as connection,
    ):
        connection.execute(delete(Credential).where(Credential.id == "8" * 32))

    with auth_engine.connect() as connection:
        assert connection.scalar(select(Caller.id).where(Caller.id == "7" * 32)) is not None
        assert connection.scalar(select(Credential.id).where(Credential.id == "8" * 32))
        assert connection.scalar(select(AuditEvent.id)) == "9" * 32
