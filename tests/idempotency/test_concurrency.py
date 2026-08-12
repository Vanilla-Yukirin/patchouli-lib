from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import Engine, text

from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency import IdempotencyRepository, IdempotencyService

from .conftest import request_for, response_for, validated_caller


def test_begin_immediate_serializes_same_key_to_one_durable_success(
    idempotency_engine: Engine,
) -> None:
    with idempotency_engine.begin() as connection:
        connection.execute(text("CREATE TABLE synthetic_domain_mutations (id TEXT PRIMARY KEY)"))

    first_has_written = Event()
    release_first = Event()
    second_started = Event()
    second_entered_transaction = Event()
    request = request_for()

    def first_writer() -> str:
        with immediate_transaction(idempotency_engine) as connection:
            caller = validated_caller(connection)
            service = IdempotencyService(IdempotencyRepository(connection))
            assert service.lookup(caller, request) is None
            connection.execute(
                text("INSERT INTO synthetic_domain_mutations (id) VALUES ('only-mutation')")
            )
            service.record_success(caller, request, response_for())
            first_has_written.set()
            assert release_first.wait(timeout=5)
        return "committed"

    def lost_response_retry() -> str:
        assert first_has_written.wait(timeout=5)
        second_started.set()
        with immediate_transaction(idempotency_engine) as connection:
            second_entered_transaction.set()
            caller = validated_caller(connection)
            replay = IdempotencyService(IdempotencyRepository(connection)).lookup(caller, request)
            assert replay is not None
            return replay.original_request_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_writer)
        second = executor.submit(lost_response_retry)
        assert second_started.wait(timeout=5)
        assert not second_entered_transaction.wait(timeout=0.2)
        release_first.set()
        assert first.result(timeout=5) == "committed"
        assert second.result(timeout=5) == f"req_{'c' * 32}"

    with idempotency_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM synthetic_domain_mutations")).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 1
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
