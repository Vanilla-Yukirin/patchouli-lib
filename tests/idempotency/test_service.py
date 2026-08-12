import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency import (
    IDEMPOTENCY_CONFLICT_DETAIL,
    IdempotencyConflictError,
    IdempotencyPersistenceError,
    IdempotencyRepository,
    IdempotencyService,
)

from .conftest import (
    CALLER_B,
    CREDENTIAL_A1,
    CREDENTIAL_A2,
    LIBRARY_A,
    request_for,
    response_for,
    validated_caller,
)


def test_same_fingerprint_returns_exact_replay_wire(idempotency_engine: Engine) -> None:
    request = request_for()
    response = response_for()
    with immediate_transaction(idempotency_engine) as connection:
        caller = validated_caller(connection)
        service = IdempotencyService(IdempotencyRepository(connection))
        assert service.lookup(caller, request) is None
        stored = service.record_success(caller, request, response)
        replay = service.lookup(caller, request)

    assert stored.request_fingerprint == request.request_fingerprint
    assert replay is not None
    assert replay.model_dump() == response.model_dump()
    assert replay.presentation_headers() == {
        "Content-Type": "application/json",
        "Location": "/api/v1/sections/sec_synthetic/pages/page_synthetic",
        "ETag": '"revision-synthetic-1"',
        "X-Request-ID": f"req_{'c' * 32}",
        "Idempotency-Replayed": "true",
    }


def test_different_fingerprint_returns_fixed_conflict_without_mutation(
    idempotency_engine: Engine,
) -> None:
    first = request_for(fingerprint_part=b"first")
    changed = request_for(fingerprint_part=b"changed")
    with immediate_transaction(idempotency_engine) as connection:
        caller = validated_caller(connection)
        service = IdempotencyService(IdempotencyRepository(connection))
        service.record_success(caller, first, response_for())

    with immediate_transaction(idempotency_engine) as connection:
        caller = validated_caller(connection)
        service = IdempotencyService(IdempotencyRepository(connection))
        try:
            service.lookup(caller, changed)
        except IdempotencyConflictError as exc:
            assert str(exc) == IDEMPOTENCY_CONFLICT_DETAIL
            assert changed.request_fingerprint.hex() not in repr(exc)
        else:
            raise AssertionError("A changed fingerprint must conflict.")
        assert (
            connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 1
        )


def test_different_stable_callers_can_reuse_one_presented_key(
    idempotency_engine: Engine,
) -> None:
    request = request_for()
    with immediate_transaction(idempotency_engine) as connection:
        service = IdempotencyService(IdempotencyRepository(connection))
        caller_a = validated_caller(connection)
        caller_b = validated_caller(
            connection,
            library_id=LIBRARY_A,
            caller_id=CALLER_B,
            credential_id="a" * 32,
        )
        service.record_success(caller_a, request, response_for())
        service.record_success(caller_b, request, response_for(body=b'{"result":"caller-b"}'))
        assert (
            connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 2
        )


def test_credential_rotation_keeps_the_stable_caller_namespace(
    idempotency_engine: Engine,
) -> None:
    request = request_for()
    with immediate_transaction(idempotency_engine) as connection:
        first_context = validated_caller(connection, credential_id=CREDENTIAL_A1)
        service = IdempotencyService(IdempotencyRepository(connection))
        service.record_success(first_context, request, response_for())

    with immediate_transaction(idempotency_engine) as connection:
        repository = AuthRepository(connection)
        first_credential = repository.get_credential(
            LIBRARY_A, first_context.caller_id, CREDENTIAL_A1
        )
        assert first_credential is not None
        rotated = repository.mark_credential_rotated(
            first_credential,
            CREDENTIAL_A2,
            rotated_at=2_000_000,
        )
        assert rotated is not None

    with immediate_transaction(idempotency_engine) as connection:
        second_context = validated_caller(connection, credential_id=CREDENTIAL_A2)
        assert first_context == second_context
        service = IdempotencyService(IdempotencyRepository(connection))
        assert service.lookup(second_context, request) is not None


def test_repository_and_service_never_commit(idempotency_engine: Engine) -> None:
    request = request_for()
    response = response_for()
    with idempotency_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        caller = validated_caller(connection)
        IdempotencyService(IdempotencyRepository(connection)).record_success(
            caller,
            request,
            response,
        )
        assert connection.in_transaction()
        assert (
            connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 1
        )
        connection.rollback()

    with idempotency_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 0
        )


def test_persistence_failure_does_not_render_response_body(
    idempotency_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SYNTHETIC_PRIVATE_RESPONSE_MARKER"

    def fail_with_sensitive_parameters(_: object) -> None:
        raise OperationalError(
            "INSERT INTO idempotency_records (response_body) VALUES (?)",
            (marker.encode(),),
            RuntimeError("synthetic persistence failure"),
        )

    with immediate_transaction(idempotency_engine) as connection:
        caller = validated_caller(connection)
        repository = IdempotencyRepository(connection)
        monkeypatch.setattr(repository, "add", fail_with_sensitive_parameters)
        service = IdempotencyService(repository)
        with pytest.raises(IdempotencyPersistenceError) as exc_info:
            service.record_success(
                caller,
                request_for(),
                response_for(body=f'{{"private":"{marker}"}}'.encode()),
            )

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert marker not in rendered
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
