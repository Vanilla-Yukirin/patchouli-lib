from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import IssuedToken, generate_token
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.identifiers import parse_occurrence_time

from .helpers import seed_library_structure

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPERATION_TIME = parse_occurrence_time("2026-08-13T10:00:01.000000Z").utc_microseconds


@dataclass(frozen=True, slots=True)
class ArchiveScope:
    library_id: str
    section_id: str
    book_id: str
    caller_id: str
    credential_id: str
    token: IssuedToken


def alembic_config() -> Config:
    return Config(str(REPOSITORY_ROOT / "alembic.ini"))


def configure_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, alembic_config()


@pytest.fixture
def content_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url, config = configure_database(tmp_path / "content.db", monkeypatch)
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def archive_scope(content_engine: Engine) -> ArchiveScope:
    library_id, section_id, book_id = seed_library_structure(content_engine)
    caller_id = "4" * 32
    credential_id = "5" * 32
    token = generate_token()
    with immediate_transaction(content_engine) as connection:
        repository = AuthRepository(connection)
        repository.add_caller(
            NewCaller(
                id=caller_id,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic Archive Agent",
                created_at=OPERATION_TIME - 1_000_000,
                updated_at=OPERATION_TIME - 1_000_000,
            )
        )
        repository.add_credential(
            NewCredential(
                id=credential_id,
                library_id=library_id,
                caller_id=caller_id,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                expires_at=OPERATION_TIME + 10_000_000,
                created_at=OPERATION_TIME - 1_000_000,
                updated_at=OPERATION_TIME - 1_000_000,
            )
        )
        repository.add_grant(
            NewSectionGrant(
                library_id=library_id,
                caller_id=caller_id,
                section_id=section_id,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=OPERATION_TIME - 1_000_000,
            )
        )
    return ArchiveScope(
        library_id,
        section_id,
        book_id,
        caller_id,
        credential_id,
        token,
    )
