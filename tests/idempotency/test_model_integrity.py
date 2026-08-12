from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency import IdempotencyRepository, IdempotencyService

from .conftest import (
    CALLER_A,
    CALLER_C,
    LIBRARY_A,
    LIBRARY_B,
    request_for,
    response_for,
    validated_caller,
)


def _record_values() -> dict[str, object]:
    request = request_for()
    response = response_for()
    return {
        "library_id": LIBRARY_A,
        "caller_id": CALLER_A,
        **request.model_dump(),
        **response.model_dump(),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "post"),
        ("route_template", "/api/v1/sections/expanded/pages"),
        ("key_digest", b"x" * 31),
        ("request_fingerprint", b"x" * 31),
        ("response_status", 409),
        ("response_media_type", "text/plain"),
        ("response_body", b"not-json"),
        ("response_location", "https://example.invalid/private"),
        ("response_etag", 'W/"weak"'),
        ("original_request_id", "req_invalid"),
        ("original_request_timestamp", "2026-08-13T00:00:00Z"),
        ("original_request_timestamp", "2026-02-30T00:00:00.000000Z"),
        ("original_request_timestamp", "0000-01-01T00:00:00.000000Z"),
        ("original_request_timestamp", "2026-08-13T23:59:60.000000Z"),
    ],
)
def test_database_constraints_fail_closed(
    idempotency_engine: Engine,
    field: str,
    value: object,
) -> None:
    values = _record_values()
    values[field] = value
    columns = ", ".join(values)
    parameters = ", ".join(f":{name}" for name in values)
    with pytest.raises(IntegrityError), immediate_transaction(idempotency_engine) as connection:
        connection.execute(
            text(f"INSERT INTO idempotency_records ({columns}) VALUES ({parameters})"),
            values,
        )


def test_composite_caller_fk_rejects_cross_library_scope(idempotency_engine: Engine) -> None:
    values = _record_values() | {"library_id": LIBRARY_B, "caller_id": CALLER_A}
    columns = ", ".join(values)
    parameters = ", ".join(f":{name}" for name in values)
    with pytest.raises(IntegrityError), immediate_transaction(idempotency_engine) as connection:
        connection.execute(
            text(f"INSERT INTO idempotency_records ({columns}) VALUES ({parameters})"),
            values,
        )


def test_cross_library_caller_has_independent_namespace(idempotency_engine: Engine) -> None:
    request = request_for()
    with immediate_transaction(idempotency_engine) as connection:
        caller_a = validated_caller(connection)
        caller_c = validated_caller(
            connection,
            library_id=LIBRARY_B,
            caller_id=CALLER_C,
            credential_id="b" * 32,
        )
        service = IdempotencyService(IdempotencyRepository(connection))
        service.record_success(caller_a, request, response_for())
        service.record_success(caller_c, request, response_for(body=b'{"result":"library-b"}'))
        assert (
            connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 2
        )


def test_records_are_immutable_and_have_no_automatic_expiry(idempotency_engine: Engine) -> None:
    with immediate_transaction(idempotency_engine) as connection:
        caller = validated_caller(connection)
        IdempotencyService(IdempotencyRepository(connection)).record_success(
            caller,
            request_for(),
            response_for(),
        )

    with pytest.raises(IntegrityError), immediate_transaction(idempotency_engine) as connection:
        connection.execute(text("UPDATE idempotency_records SET response_status = 202"))
    with pytest.raises(IntegrityError), immediate_transaction(idempotency_engine) as connection:
        connection.execute(text("DELETE FROM idempotency_records"))


def test_raw_presented_key_is_absent_from_schema_rows_and_database_file(
    idempotency_engine: Engine,
) -> None:
    raw_key = "SYNTHETIC-RAW-IDEMPOTENCY-MARKER-DO-NOT-STORE"
    with immediate_transaction(idempotency_engine) as connection:
        caller = validated_caller(connection)
        IdempotencyService(IdempotencyRepository(connection)).record_success(
            caller,
            request_for(key=raw_key),
            response_for(),
        )
        columns = {
            column["name"] for column in inspect(connection).get_columns("idempotency_records")
        }
        assert "idempotency_key" not in columns
        row = connection.execute(text("SELECT * FROM idempotency_records")).mappings().one()
        assert all(raw_key not in str(value) for value in row.values())

    database_path = Path(idempotency_engine.url.database or "")
    idempotency_engine.dispose()
    assert raw_key.encode("ascii") not in database_path.read_bytes()


def test_fault_rollback_leaves_neither_domain_mutation_nor_replay(
    idempotency_engine: Engine,
) -> None:
    with idempotency_engine.begin() as connection:
        connection.execute(text("CREATE TABLE synthetic_domain_mutations (id TEXT PRIMARY KEY)"))

    with (
        pytest.raises(RuntimeError, match="synthetic fault"),
        immediate_transaction(idempotency_engine) as connection,
    ):
        caller = validated_caller(connection)
        connection.execute(
            text("INSERT INTO synthetic_domain_mutations (id) VALUES ('mutation-one')")
        )
        IdempotencyService(IdempotencyRepository(connection)).record_success(
            caller,
            request_for(),
            response_for(),
        )
        raise RuntimeError("synthetic fault")

    with idempotency_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM synthetic_domain_mutations")).scalar_one()
            == 0
        )
        assert (
            connection.execute(text("SELECT count(*) FROM idempotency_records")).scalar_one() == 0
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
