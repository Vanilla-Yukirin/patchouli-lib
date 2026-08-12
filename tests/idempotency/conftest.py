from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import SectionAction
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.idempotency import (
    IdempotencyRequest,
    OriginalResponse,
    TransactionValidatedCaller,
    digest_idempotency_key,
    digest_request_fingerprint,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LIBRARY_A = "1" * 32
CALLER_A = "2" * 32
CALLER_B = "3" * 32
LIBRARY_B = "4" * 32
CALLER_C = "5" * 32
SECTION_A = "6" * 32
BOOK_A = "7" * 32
CREDENTIAL_A1 = "8" * 32
CREDENTIAL_A2 = "9" * 32
CREDENTIAL_B = "a" * 32
CREDENTIAL_C = "b" * 32
SECTION_B = "c" * 32
BOOK_B = "d" * 32
ROUTE_TEMPLATE = "/api/v1/sections/{section_id}/books/{book_id}/pages"


def configure_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(REPOSITORY_ROOT / "alembic.ini"))


def seed_idempotency_prerequisites(engine: Engine) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            text(
                "INSERT INTO libraries (id, name, created_at, updated_at) VALUES "
                "(:library_a, 'Synthetic Library A', 1000000, 1000000), "
                "(:library_b, 'Synthetic Library B', 1000000, 1000000)"
            ),
            {"library_a": LIBRARY_A, "library_b": LIBRARY_B},
        )
        connection.execute(
            text(
                "INSERT INTO sections "
                "(id, library_id, name, description, created_at, updated_at) "
                "VALUES (:section_id, :library_id, 'Synthetic Section', '', 1000000, 1000000)"
            ),
            {"section_id": SECTION_A, "library_id": LIBRARY_A},
        )
        connection.execute(
            text(
                "INSERT INTO sections "
                "(id, library_id, name, description, created_at, updated_at) "
                "VALUES (:section_id, :library_id, 'Synthetic Section B', '', "
                "1000000, 1000000)"
            ),
            {"section_id": SECTION_B, "library_id": LIBRARY_B},
        )
        connection.execute(
            text(
                "INSERT INTO books "
                "(id, library_id, section_id, name, summary, created_at, updated_at) "
                "VALUES (:book_id, :library_id, :section_id, "
                "'Synthetic Book', '', 1000000, 1000000)"
            ),
            {"book_id": BOOK_A, "library_id": LIBRARY_A, "section_id": SECTION_A},
        )
        connection.execute(
            text(
                "INSERT INTO books "
                "(id, library_id, section_id, name, summary, created_at, updated_at) "
                "VALUES (:book_id, :library_id, :section_id, "
                "'Synthetic Book B', '', 1000000, 1000000)"
            ),
            {"book_id": BOOK_B, "library_id": LIBRARY_B, "section_id": SECTION_B},
        )
        for caller_id, library_id, name in (
            (CALLER_A, LIBRARY_A, "Synthetic Caller A"),
            (CALLER_B, LIBRARY_A, "Synthetic Caller B"),
            (CALLER_C, LIBRARY_B, "Synthetic Caller C"),
        ):
            connection.execute(
                text(
                    "INSERT INTO auth_callers "
                    "(id, library_id, kind, name, description, policy_version, "
                    "created_at, updated_at, disabled_at) VALUES "
                    "(:id, :library_id, 'agent', :name, '', 1, 1000000, 1000000, NULL)"
                ),
                {"id": caller_id, "library_id": library_id, "name": name},
            )
        for credential_id, library_id, caller_id, selector in (
            (CREDENTIAL_A1, LIBRARY_A, CALLER_A, "A" * 22),
            (CREDENTIAL_A2, LIBRARY_A, CALLER_A, "B" * 22),
            (CREDENTIAL_B, LIBRARY_A, CALLER_B, "C" * 22),
            (CREDENTIAL_C, LIBRARY_B, CALLER_C, "D" * 22),
        ):
            connection.execute(
                text(
                    "INSERT INTO auth_credentials "
                    "(id, library_id, caller_id, selector, token_version, verifier, "
                    "expires_at, created_at, updated_at, last_used_at, revoked_at, "
                    "rotated_at, rotated_to_credential_id) VALUES "
                    "(:id, :library_id, :caller_id, :selector, 1, :verifier, "
                    "9000000, 1000000, 1000000, NULL, NULL, NULL, NULL)"
                ),
                {
                    "id": credential_id,
                    "library_id": library_id,
                    "caller_id": caller_id,
                    "selector": selector,
                    "verifier": b"v" * 32,
                },
            )
        for library_id, caller_id, section_id in (
            (LIBRARY_A, CALLER_A, SECTION_A),
            (LIBRARY_A, CALLER_B, SECTION_A),
            (LIBRARY_B, CALLER_C, SECTION_B),
        ):
            connection.execute(
                text(
                    "INSERT INTO auth_section_grants "
                    "(library_id, caller_id, section_id, action, created_at) "
                    "VALUES (:library_id, :caller_id, :section_id, 'archive:write', 1000000)"
                ),
                {"library_id": library_id, "caller_id": caller_id, "section_id": section_id},
            )


@pytest.fixture
def idempotency_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    database_url, config = configure_database(tmp_path / "idempotency.db", monkeypatch)
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    seed_idempotency_prerequisites(engine)
    yield engine
    engine.dispose()


def validated_caller(
    connection: Connection,
    *,
    library_id: str = LIBRARY_A,
    caller_id: str = CALLER_A,
    credential_id: str = CREDENTIAL_A1,
) -> TransactionValidatedCaller:
    repository = AuthRepository(connection)
    caller = repository.get_caller(library_id, caller_id)
    credential = repository.get_credential(library_id, caller_id, credential_id)
    section_id = SECTION_A if library_id == LIBRARY_A else SECTION_B
    grant = repository.get_grant(library_id, caller_id, section_id, SectionAction.ARCHIVE_WRITE)
    if (
        caller is None
        or caller.disabled_at is not None
        or credential is None
        or credential.revoked_at is not None
        or credential.rotated_at is not None
        or grant is None
    ):
        raise RuntimeError("Synthetic authorization check failed.")
    return TransactionValidatedCaller(library_id=library_id, caller_id=caller_id)


def request_for(
    *,
    key: str = "synthetic-operation-key",
    fingerprint_part: bytes = b"synthetic-semantic-request",
) -> IdempotencyRequest:
    return IdempotencyRequest(
        method="POST",
        route_template=ROUTE_TEMPLATE,
        key_digest=digest_idempotency_key(key),
        request_fingerprint=digest_request_fingerprint(fingerprint_part),
    )


def response_for(*, body: bytes = b'{"result":"synthetic"}') -> OriginalResponse:
    return OriginalResponse(
        response_status=201,
        response_media_type="application/json",
        response_body=body,
        response_location="/api/v1/sections/sec_synthetic/pages/page_synthetic",
        response_etag='"revision-synthetic-1"',
        original_request_id=f"req_{'c' * 32}",
        original_request_timestamp="2026-08-13T00:00:00.000000Z",
    )
