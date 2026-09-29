from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.library_policy import LegacySectionPolicy, resolve_library_policy
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.service import AuthenticationService
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.backup import (
    BackupArtifactIdentity,
    BackupDatabaseError,
    create_backup,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import FILE_SET_SCHEMA_REVISION
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = "1" * 32
SECTION = "2" * 32
BOOK = "3" * 32
CALLER = "4" * 32
CREDENTIAL = "5" * 32


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(ROOT / "alembic.ini"))


def _seed_legacy_credential(database_url: str) -> str:
    engine = build_engine(database_url)
    token = generate_token()
    try:
        with immediate_transaction(engine) as connection:
            LibrarySeedService(
                LibraryRepository(connection),
                id_factory=iter((LIBRARY, SECTION, BOOK)).__next__,
                clock=lambda: 1_000_000,
            ).seed(
                LibraryStructureSeed(
                    library_name="Synthetic Existing Library",
                    section_name="Synthetic Existing Section",
                    book_name="Synthetic Existing Book",
                )
            )
            repository = AuthRepository(connection)
            repository.add_caller(
                NewCaller(
                    id=CALLER,
                    library_id=LIBRARY,
                    kind=CallerKind.AGENT,
                    name="Synthetic Existing Agent",
                    created_at=2_000_000,
                    updated_at=2_000_000,
                )
            )
            repository.add_credential(
                NewCredential(
                    id=CREDENTIAL,
                    library_id=LIBRARY,
                    caller_id=CALLER,
                    selector=token.selector,
                    token_version=token.version,
                    verifier=token.verifier,
                    expires_at=10_000_000,
                    created_at=2_000_000,
                    updated_at=2_000_000,
                )
            )
            repository.add_grant(
                NewSectionGrant(
                    library_id=LIBRARY,
                    caller_id=CALLER,
                    section_id=SECTION,
                    action=SectionAction.PAGE_READ,
                    created_at=2_000_000,
                )
            )
    finally:
        engine.dispose()
    return token.value


def test_existing_credential_migrates_without_implicit_grants_and_empty_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, config = _config(tmp_path / "library-policy.db", monkeypatch)
    command.upgrade(config, "20260929_0013")
    token = _seed_legacy_credential(url)
    assert (
        validate_database(
            tmp_path / "library-policy.db", schema_revision=FILE_SET_SCHEMA_REVISION
        ).schema_revision
        == FILE_SET_SCHEMA_REVISION
    )

    command.upgrade(config, "head")
    assert validate_database(tmp_path / "library-policy.db").schema_revision == "20260929_0014"
    engine = build_engine(url)
    try:
        assert {
            "auth_credential_library_policies",
            "auth_credential_library_grants",
        }.issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.scalar(select(CredentialLibraryPolicy)) is None
            assert connection.scalar(select(CredentialLibraryGrant)) is None
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "20260929_0014"
            )
            assert isinstance(
                resolve_library_policy(
                    connection,
                    credential_id=CREDENTIAL,
                    caller_id=CALLER,
                    home_library_id=LIBRARY,
                    target_library_id=LIBRARY,
                    active_at=3_000_000,
                ),
                LegacySectionPolicy,
            )
        with immediate_transaction(engine) as connection:
            authenticated = AuthenticationService(
                AuthRepository(connection), clock=lambda: 3_000_000
            ).authorize_content(
                token,
                library_id=LIBRARY,
                section_id=SECTION,
                action=SectionAction.PAGE_READ,
            )
            assert authenticated.caller.id == CALLER
    finally:
        engine.dispose()
    command.check(config)

    command.downgrade(config, "20260929_0013")
    engine = build_engine(url)
    try:
        assert "auth_credential_library_policies" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    command.check(config)


def test_downgrade_refuses_to_discard_credential_policy_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, config = _config(tmp_path / "library-policy-nonempty.db", monkeypatch)
    command.upgrade(config, "head")
    _seed_legacy_credential(url)
    engine = build_engine(url)
    try:
        with immediate_transaction(engine) as connection:
            operator_token = generate_token()
            repository = AuthRepository(connection)
            repository.add_caller(
                NewCaller(
                    id="6" * 32,
                    library_id=LIBRARY,
                    kind=CallerKind.OPERATOR,
                    name="Synthetic Operator",
                    created_at=2_000_000,
                    updated_at=2_000_000,
                )
            )
            repository.add_credential(
                NewCredential(
                    id="7" * 32,
                    library_id=LIBRARY,
                    caller_id="6" * 32,
                    selector=operator_token.selector,
                    token_version=operator_token.version,
                    verifier=operator_token.verifier,
                    expires_at=10_000_000,
                    created_at=2_000_000,
                    updated_at=2_000_000,
                )
            )
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            connection.execute(
                insert(CredentialLibraryPolicy),
                {
                    "credential_id": "7" * 32,
                    "caller_id": "6" * 32,
                    "home_library_id": LIBRARY,
                    "mode": "library_grants",
                    "created_at": 3_000_000,
                },
            )
        with immediate_transaction(engine) as connection:
            connection.execute(
                insert(CredentialLibraryPolicy),
                {
                    "credential_id": CREDENTIAL,
                    "caller_id": CALLER,
                    "home_library_id": LIBRARY,
                    "mode": "library_grants",
                    "created_at": 3_000_000,
                },
            )
        assert (
            validate_database(tmp_path / "library-policy-nonempty.db").schema_revision
            == "20260929_0014"
        )
        corrupt = tmp_path / "policy-operator.db"
        shutil.copyfile(tmp_path / "library-policy-nonempty.db", corrupt)
        with closing(sqlite3.connect(corrupt)) as connection:
            connection.execute("DELETE FROM auth_section_grants WHERE caller_id = ?", (CALLER,))
            connection.execute("UPDATE auth_callers SET kind = 'operator' WHERE id = ?", (CALLER,))
            connection.commit()
        with pytest.raises(BackupDatabaseError):
            validate_database(corrupt)
        for statement in (
            update(CredentialLibraryPolicy).values(created_at=4_000_000),
            delete(CredentialLibraryPolicy),
        ):
            with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
                connection.execute(statement)
        with pytest.raises(RuntimeError, match="discard credential Library policy data"):
            command.downgrade(config, "20260929_0013")
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "20260929_0014"
            )
            assert connection.scalar(select(CredentialLibraryPolicy)) is not None
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_0014_policy_grant_survives_verified_backup_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "policy-backup-source.db"
    url, config = _config(source, monkeypatch)
    command.upgrade(config, "head")
    _seed_legacy_credential(url)
    engine = build_engine(url)
    try:
        with immediate_transaction(engine) as connection:
            connection.execute(
                insert(CredentialLibraryPolicy),
                {
                    "credential_id": CREDENTIAL,
                    "caller_id": CALLER,
                    "home_library_id": LIBRARY,
                    "mode": "library_grants",
                    "created_at": 3_000_000,
                },
            )
            connection.execute(
                insert(CredentialLibraryGrant),
                {
                    "credential_id": CREDENTIAL,
                    "caller_id": CALLER,
                    "home_library_id": LIBRARY,
                    "target_library_id": LIBRARY,
                    "action": "read",
                    "created_at": 3_000_000,
                },
            )
        bundle = tmp_path / "policy-bundle"
        artifact = BackupArtifactIdentity("synthetic/policy@immutable", "sha256:" + "1" * 64)
        result = create_backup(
            engine,
            bundle,
            artifact_identity=artifact,
            app_version="0.0.0-test",
        )
        assert result.manifest.schema_revision == "20260929_0014"
        assert verify_backup_bundle(bundle, app_version="0.0.0-test") == result.manifest
        restored = tmp_path / "policy-restored.db"
        restore_backup(bundle, restored, app_version="0.0.0-test")
        assert validate_database(restored).schema_revision == "20260929_0014"
        restored_engine = build_engine(f"sqlite:///{restored.as_posix()}")
        try:
            with restored_engine.connect() as connection:
                grant = connection.execute(
                    select(
                        CredentialLibraryGrant.credential_id,
                        CredentialLibraryGrant.target_library_id,
                        CredentialLibraryGrant.action,
                    )
                ).one()
                assert tuple(grant) == (
                    CREDENTIAL,
                    LIBRARY,
                    "read",
                )
        finally:
            restored_engine.dispose()
    finally:
        engine.dispose()
