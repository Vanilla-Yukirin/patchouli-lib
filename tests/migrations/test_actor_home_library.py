"""Synthetic-only checks for the actor-home storage migration."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, inspect, text, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.models import AuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewCredential
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.backup.validation import validate_database
from patchouli_lib.content.models import (
    Page,
    PageLifecycleEvent,
    PageLifecycleGuard,
    PageOccurrenceCorrection,
    PageOccurrenceCorrectionGuard,
)
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from content.helpers import (  # noqa: E402
    insert_page_graph,
    page_graph_values,
    seed_library_structure,
)

OLD_REVISION = "20260929_0016"
NEW_REVISION = "20260929_0017"
TABLES = (
    "auth_audit_events",
    "idempotency_records",
    "page_occurrence_corrections",
    "page_occurrence_correction_guards",
    "page_lifecycle_events",
    "page_lifecycle_guards",
)
CALLER = "4" * 32
CREDENTIAL = "5" * 32


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(ROOT / "alembic.ini"))


def _seed_identity(engine: Engine, library_id: str) -> None:
    token = generate_token()
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        repository.add_caller(
            NewCaller(
                id=CALLER,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic Agent",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository.add_credential(
            NewCredential(
                id=CREDENTIAL,
                library_id=library_id,
                caller_id=CALLER,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )


def _audit_sql(*, new: bool) -> str:
    home_column = ", actor_home_library_id" if new else ""
    home_value = ", :home" if new else ""
    return (
        "INSERT INTO auth_audit_events "
        f"(id, library_id{home_column}, actor_caller_id, actor_credential_id, "
        "action, resource_type, resource_id, outcome, request_id, occurred_at) "
        f"VALUES (:id, :target{home_value}, :actor, :credential, "
        "'content.archive.delete', 'page', 'synthetic-page', 'succeeded', "
        "'req_synthetic_actor_home', 4000000)"
    )


def _idempotency_sql(*, new: bool) -> str:
    home_column = ", actor_home_library_id" if new else ""
    home_value = ", :home" if new else ""
    return (
        "INSERT INTO idempotency_records "
        f"(library_id{home_column}, caller_id, method, route_template, key_digest, "
        "request_fingerprint, response_status, response_media_type, response_body, "
        "response_etag, original_request_id, original_request_timestamp) "
        f"VALUES (:target{home_value}, :actor, 'POST', "
        "'/api/v1/sections/{section_id}/books/{book_id}/pages', :key, :fingerprint, "
        "201, 'application/json', :body, '\"synthetic\"', :request_id, "
        "'1970-01-01T00:00:04.000000Z')"
    )


def _idempotency_values(target: str, home: str) -> dict[str, object]:
    return {
        "target": target,
        "home": home,
        "actor": CALLER,
        "key": hashlib.sha256(target.encode()).digest(),
        "fingerprint": hashlib.sha256(b"synthetic request").digest(),
        "body": b'{"stored":"exact bytes"}',
        "request_id": "req_" + "a" * 32,
    }


def _record_occurrence_and_lifecycle(
    engine: Engine,
    *,
    library_id: str,
    section_id: str,
    book_id: str,
    actor_home_library_id: str | None,
    page_byte: int,
) -> None:
    page, revision, identifier, counter, source = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        page_byte=page_byte,
    )
    guard_home_column = ", actor_home_library_id" if actor_home_library_id else ""
    guard_home_value = ", :home" if actor_home_library_id else ""
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, (page, revision, identifier, counter, source))
        connection.execute(
            text(
                "INSERT INTO page_occurrence_correction_guards "
                "(library_id, page_uid, sequence, old_occurred_at, new_occurred_at, "
                f"actor_caller_id{guard_home_column}, corrected_at) "
                "VALUES (:target, :page_uid, 1, :old_time, :new_time, "
                f":actor{guard_home_value}, 3000000)"
            ),
            {
                "target": library_id,
                "page_uid": page.page_uid,
                "old_time": page.occurred_at,
                "new_time": page.occurred_at + 1,
                "actor": CALLER,
                "home": actor_home_library_id,
            },
        )
        connection.execute(
            update(Page)
            .where(Page.library_id == library_id, Page.page_uid == page.page_uid)
            .values(occurred_at=page.occurred_at + 1, updated_at=3_000_000)
        )
        connection.execute(
            text(
                "INSERT INTO page_lifecycle_guards "
                "(library_id, page_uid, sequence, action, section_id, old_deleted_at, "
                "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
                f"actor_caller_id{guard_home_column}, request_id) "
                "VALUES (:target, :page_uid, 1, 'delete', :section, NULL, "
                f"3000000, 4000000, 1, :new_time, :actor{guard_home_value}, :request_id)"
            ),
            {
                "target": library_id,
                "page_uid": page.page_uid,
                "section": section_id,
                "new_time": page.occurred_at + 1,
                "actor": CALLER,
                "home": actor_home_library_id,
                "request_id": "req_" + "b" * 32,
            },
        )
        connection.execute(
            update(Page)
            .where(Page.library_id == library_id, Page.page_uid == page.page_uid)
            .values(deleted_at=4_000_000, updated_at=4_000_000)
        )


def test_empty_upgrade_and_downgrade_match_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite"
    database_url, config = _config(database_path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    assert (
        validate_database(database_path, schema_revision=OLD_REVISION).schema_revision
        == OLD_REVISION
    )
    with closing(sqlite3.connect(database_path)) as raw:
        old_schema = raw.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' "
            "ORDER BY type, name"
        ).fetchall()
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(database_url)
    try:
        inspector = inspect(engine)
        for model in (
            AuditEvent,
            IdempotencyRecord,
            PageOccurrenceCorrection,
            PageOccurrenceCorrectionGuard,
            PageLifecycleEvent,
            PageLifecycleGuard,
        ):
            actual = {column["name"] for column in inspector.get_columns(model.__tablename__)}
            assert actual == set(model.__table__.columns.keys())
            foreign_keys = inspector.get_foreign_keys(model.__tablename__)
            if model.__tablename__ == "auth_audit_events":
                actor_table = "auth_credentials"
                actor_columns = [
                    "actor_credential_id",
                    "actor_caller_id",
                    "actor_home_library_id",
                ]
                referred_columns = ["id", "caller_id", "library_id"]
            else:
                actor_table = "auth_callers"
                actor_column = (
                    "caller_id"
                    if model.__tablename__ == "idempotency_records"
                    else "actor_caller_id"
                )
                actor_columns = [actor_column, "actor_home_library_id"]
                referred_columns = ["id", "library_id"]
            assert any(
                key["referred_table"] == actor_table
                and key["constrained_columns"] == actor_columns
                and key["referred_columns"] == referred_columns
                for key in foreign_keys
            )
            if model.__tablename__ in {"auth_audit_events", "idempotency_records"}:
                assert any(
                    key["referred_table"] == "libraries"
                    and key["constrained_columns"] == ["library_id"]
                    and key["referred_columns"] == ["id"]
                    for key in foreign_keys
                )
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        command.downgrade(config, OLD_REVISION)
        assert (
            validate_database(database_path, schema_revision=OLD_REVISION).schema_revision
            == OLD_REVISION
        )
        with closing(sqlite3.connect(database_path)) as raw:
            assert (
                raw.execute(
                    "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' "
                    "ORDER BY type, name"
                ).fetchall()
                == old_schema
            )
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == OLD_REVISION
            )
            assert "actor_home_library_id" not in {
                column["name"] for column in inspect(engine).get_columns("auth_audit_events")
            }
    finally:
        engine.dispose()


def test_fresh_to_0017_keeps_fk_enabled_on_alembic_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _config(tmp_path / "fresh.sqlite", monkeypatch)
    observed_stamps: list[tuple[str, int]] = []
    original = cast(Callable[..., object], Connection.execute)

    def observe_stamp(
        connection: Connection, statement: object, *args: object, **kwargs: object
    ) -> object:
        result = original(connection, statement, *args, **kwargs)
        sql = str(statement)
        if sql.startswith("UPDATE alembic_version"):
            observed_stamps.append(
                (sql, connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
            )
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", observe_stamp)
        command.upgrade(config, NEW_REVISION)
    assert observed_stamps
    assert observed_stamps[-1][1] == 1
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == NEW_REVISION
            )
    finally:
        engine.dispose()


def test_populated_history_is_exact_and_cross_library_actor_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "populated.sqlite"
    database_url, config = _config(database_path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    engine = build_engine(database_url)
    try:
        home, home_section, home_book = seed_library_structure(engine)
        target, target_section, target_book = seed_library_structure(
            engine, prefix="6", label="Second"
        )
        _seed_identity(engine, home)
        _record_occurrence_and_lifecycle(
            engine,
            library_id=home,
            section_id=home_section,
            book_id=home_book,
            actor_home_library_id=None,
            page_byte=0x11,
        )
        with immediate_transaction(engine) as connection:
            connection.execute(
                text(_audit_sql(new=False)),
                {"id": "a" * 32, "target": home, "actor": CALLER, "credential": CREDENTIAL},
            )
            connection.execute(text(_idempotency_sql(new=False)), _idempotency_values(home, home))
        with closing(sqlite3.connect(database_path)) as raw:
            old_rows = {table: raw.execute(f"SELECT * FROM {table}").fetchall() for table in TABLES}

        command.upgrade(config, NEW_REVISION)
        with closing(sqlite3.connect(database_path)) as raw:
            for table in TABLES:
                rows = raw.execute(f"SELECT * FROM {table}").fetchall()
                assert [row[:1] + row[2:] for row in rows] == old_rows[table]
                assert all(row[1] == home for row in rows)
            assert raw.execute("PRAGMA foreign_key_check").fetchall() == []

        command.downgrade(config, OLD_REVISION)
        with closing(sqlite3.connect(database_path)) as raw:
            for table in TABLES:
                assert raw.execute(f"SELECT * FROM {table}").fetchall() == old_rows[table]
            assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        command.upgrade(config, NEW_REVISION)

        with immediate_transaction(engine) as connection:
            connection.execute(
                text(_audit_sql(new=True)),
                {
                    "id": "b" * 32,
                    "target": target,
                    "home": home,
                    "actor": CALLER,
                    "credential": CREDENTIAL,
                },
            )
            connection.execute(text(_idempotency_sql(new=True)), _idempotency_values(target, home))
        _record_occurrence_and_lifecycle(
            engine,
            library_id=target,
            section_id=target_section,
            book_id=target_book,
            actor_home_library_id=home,
            page_byte=0x22,
        )
        with engine.connect() as connection:
            for table in (
                "auth_audit_events",
                "idempotency_records",
                "page_occurrence_corrections",
                "page_lifecycle_events",
            ):
                assert (
                    connection.exec_driver_sql(
                        f"SELECT count(*) FROM {table} WHERE library_id = ? "
                        "AND actor_home_library_id = ?",
                        (target, home),
                    ).scalar_one()
                    == 1
                )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            connection.execute(
                text(_audit_sql(new=True)),
                {
                    "id": "c" * 32,
                    "target": target,
                    "home": target,
                    "actor": CALLER,
                    "credential": CREDENTIAL,
                },
            )
        with pytest.raises(RuntimeError, match="Cannot downgrade cross-Library"):
            command.downgrade(config, OLD_REVISION)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == NEW_REVISION
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
    finally:
        engine.dispose()


def test_inconsistent_old_foreign_key_fails_without_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "invalid-old.sqlite"
    database_url, config = _config(database_path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    engine = build_engine(database_url)
    try:
        home, _section, _book = seed_library_structure(engine)
        _seed_identity(engine, home)
        with immediate_transaction(engine) as connection:
            connection.execute(
                text(_audit_sql(new=False)),
                {"id": "a" * 32, "target": home, "actor": CALLER, "credential": CREDENTIAL},
            )
        with closing(sqlite3.connect(database_path)) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("UPDATE auth_audit_events SET actor_credential_id = ?", ("f" * 32,))
            raw.commit()
        with pytest.raises(RuntimeError, match="relationships are inconsistent"):
            command.upgrade(config, NEW_REVISION)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == OLD_REVISION
            )
            assert not inspect(engine).has_table("__patchouli_0017_auth_audit_events")
    finally:
        engine.dispose()


def test_mid_rebuild_failure_rolls_back_all_shadow_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _config(tmp_path / "rollback.sqlite", monkeypatch)
    command.upgrade(config, OLD_REVISION)
    engine = build_engine(database_url)
    try:
        home, _section, _book = seed_library_structure(engine)
        _seed_identity(engine, home)
        with immediate_transaction(engine) as connection:
            connection.execute(
                text(_audit_sql(new=False)),
                {"id": "a" * 32, "target": home, "actor": CALLER, "credential": CREDENTIAL},
            )
        original = cast(Callable[..., object], Connection.exec_driver_sql)

        def reject_second_table(
            connection: Connection, statement: str, *args: object, **kwargs: object
        ) -> object:
            if statement.startswith('CREATE TABLE "__patchouli_0017_idempotency_records"'):
                raise RuntimeError("synthetic table rebuild interruption")
            return original(connection, statement, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Connection, "exec_driver_sql", reject_second_table)
            with pytest.raises(RuntimeError, match="synthetic table rebuild interruption"):
                command.upgrade(config, NEW_REVISION)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == OLD_REVISION
            )
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM auth_audit_events").scalar_one()
                == 1
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
        assert not inspect(engine).has_table("__patchouli_0017_auth_audit_events")
        assert "actor_home_library_id" not in {
            column["name"] for column in inspect(engine).get_columns("auth_audit_events")
        }
    finally:
        engine.dispose()
