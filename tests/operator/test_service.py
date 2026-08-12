from __future__ import annotations

from collections.abc import Iterator
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from patchouli_lib.auth.models import AuditEvent, BootstrapMarker, Caller, Credential, SectionGrant
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuditOutcome,
    BootstrapGrant,
    BootstrappedOperator,
    CallerKind,
    LocalOperatorRecovery,
    NewAuditEvent,
    OperatorBootstrap,
    SectionAction,
)
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
    CredentialExpiryError,
    CredentialIssuer,
    CredentialPersistenceError,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewSection
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.operator.service import (
    CredentialLifecycleError,
    LocalOperatorRecoveryService,
    OperatorBootstrapService,
    OperatorRecoveryUnavailableError,
    OperatorService,
    ResourceNotFoundError,
)


@pytest.fixture
def operator_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = build_engine(f"sqlite:///{(tmp_path / 'operator-service.db').as_posix()}")
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


@pytest.fixture
def bootstrapped_operator(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
) -> BootstrappedOperator:
    library_id, granted_section, _ = operator_scopes
    identifiers = iter(("a" * 32, "b" * 32, "c" * 32))
    with immediate_transaction(operator_engine) as connection:
        return OperatorBootstrapService(
            AuthRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 2_000_000,
        ).bootstrap(
            OperatorBootstrap(
                library_id=library_id,
                operator_name="Synthetic Local Operator",
                credential_expires_at=20_000_000,
                request_id="req_bootstrap_fixture",
                initial_grants=(
                    BootstrapGrant(
                        section_id=granted_section,
                        action=SectionAction.QUERY,
                    ),
                ),
            )
        )


def _operator_service(repository: AuthRepository, identifiers: list[str]) -> OperatorService:
    values = iter(identifiers)
    return OperatorService(
        repository,
        id_factory=lambda: next(values),
        clock=lambda: 3_000_000,
    )


def _create_agent_and_credential(
    engine: Engine,
    bootstrap: BootstrappedOperator,
    library_id: str,
) -> tuple[str, str, str]:
    with immediate_transaction(engine) as connection:
        service = _operator_service(
            AuthRepository(connection),
            ["d" * 32, "e" * 32, "f" * 32, "0" * 32],
        )
        caller = service.create_agent_caller(
            bootstrap.credential.value,
            library_id=library_id,
            name="Synthetic Archive Agent",
            request_id="req_create_agent",
        )
        issued = service.create_credential(
            bootstrap.credential.value,
            library_id=library_id,
            caller_id=caller.id,
            expires_at=15_000_000,
            request_id="req_create_agent_credential",
        )
    return caller.id, issued.credential.id, issued.value


def test_exact_grants_and_admin_agent_plane_separation(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, granted_section, hidden_section = operator_scopes
    caller_id, _, agent_token = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )

    with immediate_transaction(operator_engine) as connection:
        service = _operator_service(AuthRepository(connection), ["1" * 32])
        grant = service.add_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            section_id=granted_section,
            action=SectionAction.QUERY,
            request_id="req_add_query_grant",
        )
        repeated = service.add_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            section_id=granted_section,
            action=SectionAction.QUERY,
            request_id="req_repeat_query_grant",
        )
        assert repeated == grant

    with operator_engine.connect() as connection:
        auth = AuthenticationService(AuthRepository(connection), clock=lambda: 4_000_000)
        authenticated = auth.authorize_content(
            agent_token,
            library_id=library_id,
            section_id=granted_section,
            action=SectionAction.QUERY,
        )
        assert authenticated.caller.id == caller_id
        assert authenticated.caller.kind is CallerKind.AGENT

        for section_id, action in (
            (granted_section, SectionAction.PAGE_READ),
            (granted_section, SectionAction.ARCHIVE_WRITE),
            (hidden_section, SectionAction.QUERY),
        ):
            with pytest.raises(AuthorizationError):
                auth.authorize_content(
                    agent_token,
                    library_id=library_id,
                    section_id=section_id,
                    action=action,
                )

        with pytest.raises(AuthorizationError):
            auth.authorize_content(
                bootstrapped_operator.credential.value,
                library_id=library_id,
                section_id=granted_section,
                action=SectionAction.QUERY,
            )
        with pytest.raises(AuthorizationError):
            auth.require_operator(agent_token, library_id=library_id)


def test_local_recovery_preserves_marker_and_operator_and_retires_old_credentials(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    old_token = bootstrapped_operator.credential.value
    identifiers = iter(("5" * 32, "6" * 32))
    with immediate_transaction(operator_engine) as connection:
        recovered = LocalOperatorRecoveryService(
            AuthRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 4_000_000,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=20_000_000,
                request_id="req_local_operator_recovery",
            )
        )

    assert recovered.marker == bootstrapped_operator.marker
    assert recovered.caller == bootstrapped_operator.caller
    assert recovered.retired_credential_ids == (bootstrapped_operator.credential.credential.id,)
    assert recovered.credential.value != old_token
    assert recovered.credential.value not in repr(recovered)
    assert "<redacted>" in repr(recovered)

    with operator_engine.connect() as connection:
        repository = AuthRepository(connection)
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        assert (
            connection.scalar(
                select(func.count()).select_from(Caller).where(Caller.kind == "operator")
            )
            == 1
        )
        old = repository.get_credential(
            library_id,
            bootstrapped_operator.caller.id,
            bootstrapped_operator.credential.credential.id,
        )
        assert old is not None
        assert old.revoked_at == 4_000_000
        auth = AuthenticationService(repository, clock=lambda: 5_000_000)
        with pytest.raises(AuthenticationError):
            auth.authenticate(old_token)
        assert (
            auth.require_operator(recovered.credential.value, library_id=library_id).caller.id
            == bootstrapped_operator.caller.id
        )
        event = (
            connection.execute(
                select(AuditEvent.__table__).where(AuditEvent.action == "auth.operator.recovery")
            )
            .mappings()
            .one()
        )
        assert event["actor_caller_id"] == bootstrapped_operator.caller.id
        assert event["actor_credential_id"] == recovered.credential.credential.id
        assert event["resource_id"] == bootstrapped_operator.caller.id

        database_rendering = " ".join(
            (
                repr(connection.exec_driver_sql("SELECT * FROM auth_credentials").all()),
                repr(connection.exec_driver_sql("SELECT * FROM auth_audit_events").all()),
            )
        )
    secret = recovered.credential.value.rsplit(".", maxsplit=1)[1]
    assert recovered.credential.value not in database_rendering
    assert secret not in database_rendering


def test_repeated_local_recovery_reissues_distinct_secret_and_retires_lost_response(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    with immediate_transaction(operator_engine) as connection:
        first_ids = iter(("5" * 32, "6" * 32))
        first = LocalOperatorRecoveryService(
            AuthRepository(connection),
            id_factory=lambda: next(first_ids),
            clock=lambda: 4_000_000,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=20_000_000,
                request_id="req_first_lost_recovery",
            )
        )

    with immediate_transaction(operator_engine) as connection:
        second_ids = iter(("7" * 32, "8" * 32))
        second = LocalOperatorRecoveryService(
            AuthRepository(connection),
            id_factory=lambda: next(second_ids),
            clock=lambda: 5_000_000,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=21_000_000,
                request_id="req_second_recovery",
            )
        )

    assert second.credential.value != first.credential.value
    assert second.credential.value not in repr(second)
    assert first.credential.value not in repr(second)
    assert second.retired_credential_ids == (first.credential.credential.id,)
    with operator_engine.connect() as connection:
        repository = AuthRepository(connection)
        first_stored = repository.get_credential(
            library_id,
            bootstrapped_operator.caller.id,
            first.credential.credential.id,
        )
        assert first_stored is not None
        assert first_stored.revoked_at == 5_000_000
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        assert connection.scalar(select(func.count()).select_from(Credential)) == 3
        assert (
            connection.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "auth.operator.recovery")
            )
            == 2
        )


def test_local_recovery_requires_no_active_operator_credential(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    with operator_engine.connect() as connection, pytest.raises(AuthenticationError):
        AuthenticationService(
            AuthRepository(connection),
            clock=lambda: 21_000_000,
        ).authenticate(bootstrapped_operator.credential.value)

    with immediate_transaction(operator_engine) as connection:
        identifiers = iter(("5" * 32, "6" * 32))
        recovered = LocalOperatorRecoveryService(
            AuthRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 21_000_000,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=30_000_000,
                request_id="req_expired_operator_recovery",
            )
        )

    assert recovered.caller.id == bootstrapped_operator.caller.id
    assert recovered.retired_credential_ids == ()
    with operator_engine.connect() as connection:
        authenticated = AuthenticationService(
            AuthRepository(connection),
            clock=lambda: 22_000_000,
        ).require_operator(recovered.credential.value, library_id=library_id)
        assert authenticated.caller.id == bootstrapped_operator.caller.id


def test_local_recovery_rolls_back_new_credential_retirement_and_audit(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    recovered_token: str | None = None
    with (
        pytest.raises(RuntimeError, match="synthetic recovery rollback"),
        immediate_transaction(operator_engine) as connection,
    ):
        identifiers = iter(("5" * 32, "6" * 32))
        recovered = LocalOperatorRecoveryService(
            AuthRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 4_000_000,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=20_000_000,
                request_id="req_rollback_recovery",
            )
        )
        recovered_token = recovered.credential.value
        raise RuntimeError("synthetic recovery rollback")

    assert recovered_token is not None
    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        assert (
            AuthenticationService(
                AuthRepository(connection),
                clock=lambda: 5_000_000,
            )
            .require_operator(bootstrapped_operator.credential.value, library_id=library_id)
            .caller.id
            == bootstrapped_operator.caller.id
        )
        database_rendering = repr(
            connection.exec_driver_sql("SELECT * FROM auth_credentials").all()
        )
    assert recovered_token not in database_rendering


def test_local_recovery_without_permanent_marker_uses_fixed_safe_error(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
) -> None:
    library_id, _, _ = operator_scopes
    with (
        pytest.raises(OperatorRecoveryUnavailableError) as exc_info,
        immediate_transaction(operator_engine) as connection,
    ):
        LocalOperatorRecoveryService(
            AuthRepository(connection),
            clock=lambda: 4_000_000,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=20_000_000,
                request_id="req_unavailable_recovery",
            )
        )
    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert library_id not in rendered
    assert rendered.count("Local operator recovery is not available.") == 2


def test_local_recovery_invalid_expiry_uses_fixed_safe_error_without_side_effects(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    with (
        pytest.raises(OperatorRecoveryUnavailableError) as exc_info,
        immediate_transaction(operator_engine) as connection,
    ):
        LocalOperatorRecoveryService(
            AuthRepository(connection),
            clock=lambda: 4_000_000,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=4_000_000,
                request_id="req_invalid_recovery_expiry",
            )
        )

    assert str(exc_info.value) == "Local operator recovery is not available."
    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        authenticated = AuthenticationService(
            AuthRepository(connection),
            clock=lambda: 5_000_000,
        ).require_operator(bootstrapped_operator.credential.value, library_id=library_id)
        assert authenticated.caller.id == bootstrapped_operator.caller.id


def test_rotation_revocation_expiry_and_replay_safe_lifecycle(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    caller_id, credential_id, original_token = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )

    with immediate_transaction(operator_engine) as connection:
        rotated = _operator_service(
            AuthRepository(connection),
            ["1" * 32, "2" * 32],
        ).rotate_credential(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            credential_id=credential_id,
            expires_at=16_000_000,
            request_id="req_rotate_agent_credential",
        )

    with operator_engine.connect() as connection:
        auth = AuthenticationService(AuthRepository(connection), clock=lambda: 4_000_000)
        with pytest.raises(AuthenticationError):
            auth.authenticate(original_token)
        assert auth.authenticate(rotated.value).credential.id == rotated.credential.id

    with (
        pytest.raises(CredentialLifecycleError) as exc_info,
        immediate_transaction(operator_engine) as connection,
    ):
        _operator_service(AuthRepository(connection), ["3" * 32]).rotate_credential(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            credential_id=credential_id,
            expires_at=17_000_000,
            request_id="req_repeat_rotation",
        )
    assert rotated.value not in f"{exc_info.value!s} {exc_info.value!r}"

    with immediate_transaction(operator_engine) as connection:
        revoked = _operator_service(AuthRepository(connection), ["4" * 32]).revoke_credential(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            credential_id=rotated.credential.id,
            request_id="req_revoke_agent_credential",
        )
    assert revoked.revoked_at == 3_000_000
    with operator_engine.connect() as connection, pytest.raises(AuthenticationError):
        AuthenticationService(
            AuthRepository(connection),
            clock=lambda: 4_000_000,
        ).authenticate(rotated.value)

    with operator_engine.connect() as connection:
        stored_count = connection.scalar(select(func.count()).select_from(Credential))
        assert stored_count == 3


def test_expiry_last_used_coalescing_and_finite_expiry_validation(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    caller_id, _, agent_token = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )
    with operator_engine.connect() as connection:
        repository = AuthRepository(connection)
        authenticated = AuthenticationService(
            repository,
            clock=lambda: 4_000_000,
            last_used_coalesce_microseconds=2_000_000,
        ).authenticate(agent_token)
        assert authenticated.credential.last_used_at is None
        connection.rollback()

    with immediate_transaction(operator_engine) as connection:
        authenticated = AuthenticationService(
            AuthRepository(connection),
            clock=lambda: 6_000_000,
            last_used_coalesce_microseconds=2_000_000,
        ).authenticate(agent_token)
        assert authenticated.credential.last_used_at == 6_000_000

    with operator_engine.connect() as connection:
        with pytest.raises(AuthenticationError):
            AuthenticationService(
                AuthRepository(connection),
                clock=lambda: 15_000_000,
            ).authenticate(agent_token)
        caller = AuthRepository(connection).get_caller(library_id, caller_id)
        assert caller is not None
        with pytest.raises(CredentialExpiryError):
            CredentialIssuer(
                AuthRepository(connection),
                clock=lambda: 7_000_000,
            ).issue(caller, expires_at=7_000_000)


def test_credential_creation_retry_issues_a_distinct_secret_without_replay(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    caller_id, first_credential_id, first_token = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )
    with immediate_transaction(operator_engine) as connection:
        second = _operator_service(
            AuthRepository(connection),
            ["1" * 32, "2" * 32],
        ).create_credential(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            expires_at=16_000_000,
            request_id="req_reissue_after_lost_response",
        )

    assert second.credential.id != first_credential_id
    assert second.value != first_token
    assert first_token not in repr(second)
    assert second.value not in repr(second)
    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Credential)) == 3


def test_credential_persistence_error_redacts_selector_and_verifier(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_id, _, _ = operator_scopes
    caller_id, _, _ = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )
    issued = generate_token()
    monkeypatch.setattr(
        import_module("patchouli_lib.auth.service"), "generate_token", lambda: issued
    )

    with immediate_transaction(operator_engine) as connection:
        caller = AuthRepository(connection).get_caller(library_id, caller_id)
        assert caller is not None
        CredentialIssuer(
            AuthRepository(connection),
            id_factory=lambda: "5" * 32,
            clock=lambda: 4_000_000,
        ).issue(caller, expires_at=10_000_000)

    with (
        pytest.raises(CredentialPersistenceError) as exc_info,
        immediate_transaction(operator_engine) as connection,
    ):
        caller = AuthRepository(connection).get_caller(library_id, caller_id)
        assert caller is not None
        CredentialIssuer(
            AuthRepository(connection),
            id_factory=lambda: "6" * 32,
            clock=lambda: 4_000_000,
        ).issue(caller, expires_at=10_000_000)

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert issued.value not in rendered
    assert issued.selector not in rendered
    assert issued.verifier.hex() not in rendered
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_non_integrity_persistence_error_is_redacted(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_id, _, _ = operator_scopes
    caller_id, _, _ = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )
    issued = generate_token()
    monkeypatch.setattr(
        import_module("patchouli_lib.auth.service"), "generate_token", lambda: issued
    )

    def fail_with_sensitive_parameters(_: object) -> None:
        raise OperationalError(
            "INSERT INTO auth_credentials (selector, verifier) VALUES (?, ?)",
            (issued.selector, issued.verifier),
            RuntimeError("synthetic persistence failure"),
        )

    with operator_engine.connect() as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(library_id, caller_id)
        assert caller is not None
        monkeypatch.setattr(repository, "add_credential", fail_with_sensitive_parameters)
        with pytest.raises(CredentialPersistenceError) as exc_info:
            CredentialIssuer(
                repository,
                id_factory=lambda: "5" * 32,
                clock=lambda: 4_000_000,
            ).issue(caller, expires_at=10_000_000)

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert issued.value not in rendered
    assert issued.selector not in rendered
    assert issued.verifier.hex() not in rendered
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_caller_disable_and_grant_removal_take_effect_immediately(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, section_id, _ = operator_scopes
    caller_id, _, agent_token = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )
    with immediate_transaction(operator_engine) as connection:
        service = _operator_service(AuthRepository(connection), ["1" * 32, "2" * 32])
        service.add_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            section_id=section_id,
            action=SectionAction.PAGE_READ,
            request_id="req_add_read_grant",
        )
        assert service.remove_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            section_id=section_id,
            action=SectionAction.PAGE_READ,
            request_id="req_remove_read_grant",
        )
        assert not service.remove_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            section_id=section_id,
            action=SectionAction.PAGE_READ,
            request_id="req_repeat_remove_read_grant",
        )

    with operator_engine.connect() as connection:
        auth = AuthenticationService(AuthRepository(connection), clock=lambda: 4_000_000)
        with pytest.raises(AuthorizationError):
            auth.authorize_content(
                agent_token,
                library_id=library_id,
                section_id=section_id,
                action=SectionAction.PAGE_READ,
            )

    with immediate_transaction(operator_engine) as connection:
        disabled = _operator_service(AuthRepository(connection), ["3" * 32]).disable_caller(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            request_id="req_disable_agent",
        )
    assert disabled.disabled_at == 3_000_000
    with immediate_transaction(operator_engine) as connection:
        repeated = _operator_service(AuthRepository(connection), []).disable_caller(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=caller_id,
            request_id="req_repeat_disable_agent",
        )
    assert repeated == disabled
    with operator_engine.connect() as connection, pytest.raises(AuthenticationError):
        AuthenticationService(
            AuthRepository(connection),
            clock=lambda: 4_000_000,
        ).authenticate(agent_token)


def test_disable_caller_rejects_bootstrapped_operator_without_mutation(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
) -> None:
    library_id, granted_section, _ = operator_scopes
    identifiers = iter(("a" * 32, "b" * 32, "c" * 32))
    with immediate_transaction(operator_engine) as connection:
        bootstrapped_operator = OperatorBootstrapService(
            AuthRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 2_000_000,
        ).bootstrap(
            OperatorBootstrap(
                library_id=library_id,
                operator_name="Synthetic Long-Lived Operator",
                credential_expires_at=1_000_000_000,
                request_id="req_long_lived_bootstrap",
                initial_grants=(
                    BootstrapGrant(
                        section_id=granted_section,
                        action=SectionAction.QUERY,
                    ),
                ),
            )
        )

    def auth_state(connection: Connection) -> dict[str, tuple[dict[str, object], ...]]:
        return {
            "markers": tuple(
                dict(row)
                for row in connection.execute(
                    select(BootstrapMarker.__table__).order_by(BootstrapMarker.library_id)
                )
                .mappings()
                .all()
            ),
            "callers": tuple(
                dict(row)
                for row in connection.execute(select(Caller.__table__).order_by(Caller.id))
                .mappings()
                .all()
            ),
            "credentials": tuple(
                dict(row)
                for row in connection.execute(select(Credential.__table__).order_by(Credential.id))
                .mappings()
                .all()
            ),
            "grants": tuple(
                dict(row)
                for row in connection.execute(
                    select(SectionGrant.__table__).order_by(
                        SectionGrant.caller_id,
                        SectionGrant.section_id,
                        SectionGrant.action,
                    )
                )
                .mappings()
                .all()
            ),
            "audit": tuple(
                dict(row)
                for row in connection.execute(select(AuditEvent.__table__).order_by(AuditEvent.id))
                .mappings()
                .all()
            ),
        }

    with operator_engine.connect() as connection:
        before = auth_state(connection)

    assert before["credentials"][0]["last_used_at"] is None
    with (
        immediate_transaction(operator_engine) as connection,
        pytest.raises(AuthenticationError) as auth_exc_info,
    ):
        OperatorService(
            AuthRepository(connection),
            clock=lambda: 400_000_000,
        ).disable_caller(
            "not-a-token",
            library_id=library_id,
            caller_id=bootstrapped_operator.caller.id,
            request_id="req_unauthenticated_operator_disable",
        )
    assert str(auth_exc_info.value) == "Invalid or inactive credential."

    with (
        immediate_transaction(operator_engine) as connection,
        pytest.raises(ResourceNotFoundError) as exc_info,
    ):
        OperatorService(
            AuthRepository(connection),
            id_factory=lambda: "1" * 32,
            clock=lambda: 400_000_000,
        ).disable_caller(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=bootstrapped_operator.caller.id,
            request_id="req_reject_operator_disable",
        )

    with operator_engine.connect() as connection:
        after = auth_state(connection)

    assert after == before
    assert len(after["markers"]) == 1
    assert len(after["callers"]) == 1
    assert len(after["credentials"]) == 1
    assert after["credentials"][0]["last_used_at"] is None
    assert len(after["grants"]) == 1
    assert len(after["audit"]) == 1
    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert rendered.count("Resource was not found.") == 2
    assert bootstrapped_operator.credential.value not in rendered
    assert bootstrapped_operator.credential.value.rsplit(".", maxsplit=1)[1] not in rendered


def test_grant_audit_preserves_complete_distinguishable_identity_after_removal(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, section_id, _ = operator_scopes
    first_caller_id, _, _ = _create_agent_and_credential(
        operator_engine,
        bootstrapped_operator,
        library_id,
    )
    with immediate_transaction(operator_engine) as connection:
        caller_ids = iter(("1" * 32, "2" * 32))
        second_caller = OperatorService(
            AuthRepository(connection),
            id_factory=lambda: next(caller_ids),
            clock=lambda: 3_000_000,
        ).create_agent_caller(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            name="Second Synthetic Audit Agent",
            request_id="req_second_audit_agent",
        )

    with immediate_transaction(operator_engine) as connection:
        audit_ids = iter(("3" * 32, "4" * 32, "5" * 32, "6" * 32))
        service = OperatorService(
            AuthRepository(connection),
            id_factory=lambda: next(audit_ids),
            clock=lambda: 4_000_000,
        )
        service.add_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=first_caller_id,
            section_id=section_id,
            action=SectionAction.QUERY,
            request_id="req_first_query_grant",
        )
        service.add_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=first_caller_id,
            section_id=section_id,
            action=SectionAction.PAGE_READ,
            request_id="req_first_read_grant",
        )
        service.add_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=second_caller.id,
            section_id=section_id,
            action=SectionAction.QUERY,
            request_id="req_second_query_grant",
        )
        assert service.remove_grant(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            caller_id=first_caller_id,
            section_id=section_id,
            action=SectionAction.QUERY,
            request_id="req_remove_first_query_grant",
        )

    with operator_engine.connect() as connection:
        rows = (
            connection.execute(
                select(AuditEvent.__table__).where(
                    AuditEvent.action.in_(("auth.grant.add", "auth.grant.remove"))
                )
            )
            .mappings()
            .all()
        )
        identities = {
            (
                row["action"],
                row["target_caller_id"],
                row["section_id"],
                row["section_action"],
            )
            for row in rows
        }
        assert identities == {
            ("auth.grant.add", first_caller_id, section_id, SectionAction.QUERY.value),
            ("auth.grant.add", first_caller_id, section_id, SectionAction.PAGE_READ.value),
            ("auth.grant.add", second_caller.id, section_id, SectionAction.QUERY.value),
            ("auth.grant.remove", first_caller_id, section_id, SectionAction.QUERY.value),
        }
        assert (
            connection.scalar(
                select(func.count())
                .select_from(SectionGrant)
                .where(
                    SectionGrant.caller_id == first_caller_id,
                    SectionGrant.section_id == section_id,
                    SectionGrant.action == SectionAction.QUERY.value,
                )
            )
            == 0
        )
        assert not {
            "token",
            "body",
            "query",
            "source_locator",
            "authorization",
        }.intersection(column.name for column in AuditEvent.__table__.columns)


def test_token_secret_is_absent_from_database_repr_errors_and_audit(
    operator_engine: Engine,
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    token = bootstrapped_operator.credential.value
    secret = token.rsplit(".", maxsplit=1)[1]
    with operator_engine.connect() as connection:
        database_rendering = " ".join(
            repr(connection.exec_driver_sql(f"SELECT * FROM {table}").all())
            for table in (
                "auth_callers",
                "auth_credentials",
                "auth_section_grants",
                "auth_audit_events",
                "operator_bootstrap_markers",
            )
        )
        with pytest.raises(AuthenticationError) as exc_info:
            AuthenticationService(
                AuthRepository(connection),
                clock=lambda: 4_000_000,
            ).authenticate(token[:-1] + ("A" if token[-1] != "A" else "B"))

    rendered = " ".join(
        (
            database_rendering,
            repr(bootstrapped_operator),
            str(exc_info.value),
            repr(exc_info.value),
        )
    )
    assert token not in rendered
    assert secret not in rendered
    assert "verifier" not in {column.name for column in AuditEvent.__table__.columns}
    assert not {
        "token",
        "body",
        "query",
        "source_locator",
        "authorization",
    }.intersection(column.name for column in AuditEvent.__table__.columns)


def test_operator_mutation_rolls_back_caller_and_audit_together(
    operator_engine: Engine,
    operator_scopes: tuple[str, str, str],
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    library_id, _, _ = operator_scopes
    with (
        pytest.raises(RuntimeError, match="synthetic rollback"),
        immediate_transaction(operator_engine) as connection,
    ):
        _operator_service(
            AuthRepository(connection),
            ["d" * 32, "e" * 32],
        ).create_agent_caller(
            bootstrapped_operator.credential.value,
            library_id=library_id,
            name="Rolled Back Synthetic Agent",
            request_id="req_rollback_agent",
        )
        raise RuntimeError("synthetic rollback")

    with operator_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
        assert connection.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_public_failures_do_not_echo_token_or_selector_existence(
    operator_engine: Engine,
    bootstrapped_operator: BootstrappedOperator,
) -> None:
    token = bootstrapped_operator.credential.value
    candidates = ("not-a-token", token[:-1] + ("A" if token[-1] != "A" else "B"))
    rendered_errors: list[str] = []
    with operator_engine.connect() as connection:
        auth = AuthenticationService(AuthRepository(connection), clock=lambda: 4_000_000)
        for candidate in candidates:
            with pytest.raises(AuthenticationError) as exc_info:
                auth.authenticate(candidate)
            rendered_errors.append(f"{exc_info.value!s} {exc_info.value!r}")

    assert len(set(rendered_errors)) == 1
    assert all(candidate not in rendered_errors[0] for candidate in candidates)


def test_audit_request_id_validation_rejects_token_shaped_value() -> None:
    with pytest.raises(ValidationError):
        NewAuditEvent(
            id="1" * 32,
            library_id="2" * 32,
            actor_caller_id="3" * 32,
            actor_credential_id="4" * 32,
            action="auth.synthetic",
            resource_type="caller",
            resource_id="3" * 32,
            outcome=AuditOutcome.SUCCEEDED,
            request_id="plb1.selector.secret",
            occurred_at=1,
        )


def test_grant_audit_schema_rejects_incomplete_or_non_grant_identity() -> None:
    with pytest.raises(ValidationError, match="Grant audit identity must be complete"):
        NewAuditEvent(
            id="1" * 32,
            library_id="2" * 32,
            actor_caller_id="3" * 32,
            actor_credential_id="4" * 32,
            action="auth.grant.add",
            resource_type="section_grant",
            resource_id="5" * 32,
            target_caller_id="6" * 32,
            section_id="5" * 32,
            outcome=AuditOutcome.SUCCEEDED,
            request_id="req_invalid_grant_identity",
            occurred_at=1,
        )
    with pytest.raises(ValidationError, match="only valid for grant events"):
        NewAuditEvent(
            id="1" * 32,
            library_id="2" * 32,
            actor_caller_id="3" * 32,
            actor_credential_id="4" * 32,
            action="auth.caller.create",
            resource_type="section_grant",
            resource_id="5" * 32,
            target_caller_id="6" * 32,
            section_id="5" * 32,
            section_action=SectionAction.QUERY,
            outcome=AuditOutcome.SUCCEEDED,
            request_id="req_invalid_grant_identity",
            occurred_at=1,
        )
