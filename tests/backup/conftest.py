from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, insert, text

from patchouli_lib.content.models import (
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageSource,
    Revision,
)
from patchouli_lib.content.schemas import (
    ArchiveCitation,
    ArchivePageView,
    ArchiveResponseBody,
    ArchiveRevisionView,
    MarkdownContent,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
)
from patchouli_lib.content.service import CREATE_ROUTE_TEMPLATE, page_current_etag
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP_VERSION = "0.1.0a0"


def _seed_library_structure(engine: Engine) -> tuple[str, str, str]:
    library_id = "1" * 32
    section_id = "2" * 32
    book_id = "3" * 32
    with immediate_transaction(engine) as connection:
        connection.execute(
            text(
                "INSERT INTO libraries (id, name, created_at, updated_at) "
                "VALUES (:id, 'Synthetic Library', 1, 1)"
            ),
            {"id": library_id},
        )
        connection.execute(
            text(
                "INSERT INTO sections "
                "(id, library_id, name, description, created_at, updated_at) "
                "VALUES (:id, :library, 'Synthetic Section', '', 1, 1)"
            ),
            {"id": section_id, "library": library_id},
        )
        connection.execute(
            text(
                "INSERT INTO books "
                "(id, library_id, section_id, name, summary, created_at, updated_at) "
                "VALUES (:id, :library, :section, 'Synthetic Book', '', 1, 1)"
            ),
            {"id": book_id, "library": library_id, "section": section_id},
        )
    return library_id, section_id, book_id


def _page_graph(
    *, library_id: str, section_id: str, book_id: str
) -> tuple[NewPage, NewRevision, NewPageIdentifier, NewPageIdCollisionCounter, NewPageSource]:
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    generated = generate_page_id(occurrence, "Synthetic Archive")
    page_uid = b"\x11" * 16
    revision_id = "rev_" + "22" * 16
    content = MarkdownContent.from_bytes(b"# Synthetic archive\n\nExact bytes.\n")
    page = NewPage(
        library_id=library_id,
        page_uid=page_uid,
        section_id=section_id,
        book_id=book_id,
        page_id=generated.value,
        id_scheme=PAGE_ID_SCHEME,
        id_timestamp_micros=(occurrence.utc_microseconds // 1000) * 1000,
        base_slug=generated.base_slug,
        collision_ordinal=1,
        title="Synthetic Archive",
        page_type="archive",
        occurred_at=occurrence.utc_microseconds,
        current_revision_id=revision_id,
        current_revision_number=1,
        created_at=2_000_000,
        updated_at=2_000_000,
    )
    revision = NewRevision(
        library_id=library_id,
        revision_id=revision_id,
        page_uid=page_uid,
        revision_number=1,
        created_at=2_000_000,
        **content.model_dump(),
    )
    identifier = NewPageIdentifier(
        library_id=library_id,
        identifier_digest=page_id_registry_digest(generated.value),
        identifier_text=generated.value,
        id_scheme=PAGE_ID_SCHEME,
        identifier_kind="canonical",
        page_uid=page_uid,
        created_at=2_000_000,
    )
    counter = NewPageIdCollisionCounter(
        library_id=library_id,
        id_scheme=PAGE_ID_SCHEME,
        id_timestamp_micros=(occurrence.utc_microseconds // 1000) * 1000,
        base_slug=generated.base_slug,
        next_ordinal=2,
    )
    source = NewPageSource(
        library_id=library_id,
        source_id="4" * 32,
        page_uid=page_uid,
        revision_id=revision_id,
        revision_number=1,
        kind="synthetic",
        locator="urn:synthetic:archive",
        captured_at=occurrence.utc_microseconds,
        created_at=2_000_000,
    )
    return page, revision, identifier, counter, source


def _insert_page_graph(
    connection: Connection,
    values: tuple[
        NewPage, NewRevision, NewPageIdentifier, NewPageIdCollisionCounter, NewPageSource
    ],
) -> None:
    page, revision, identifier, counter, source = values
    connection.execute(insert(Page), page.model_dump())
    connection.execute(insert(Revision), revision.model_dump())
    connection.execute(insert(PageIdentifier), identifier.model_dump())
    connection.execute(insert(PageIdCollisionCounter), counter.model_dump())
    connection.execute(insert(PageSource), source.model_dump())


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return Config(str(REPOSITORY_ROOT / "alembic.ini"))


def seed_complete_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    command.upgrade(_config(path, monkeypatch), "head")
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    library_id, section_id, book_id = _seed_library_structure(engine)
    page, revision, identifier, counter, source = _page_graph(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    operator_id = "a" * 32
    agent_id = "b" * 32
    operator_credential = "c" * 32
    rotated_credential = "d" * 32
    active_credential = "e" * 32
    with immediate_transaction(engine) as connection:
        _insert_page_graph(connection, (page, revision, identifier, counter, source))
        connection.execute(
            text(
                "INSERT INTO auth_callers "
                "(id, library_id, kind, name, description, policy_version, created_at, "
                "updated_at, disabled_at) VALUES "
                "(:operator, :library, 'operator', 'Synthetic Operator', '', 1, 10, 10, NULL), "
                "(:agent, :library, 'agent', 'Synthetic Agent', '', 2, 10, 20, NULL)"
            ),
            {"operator": operator_id, "agent": agent_id, "library": library_id},
        )
        connection.execute(
            text(
                "INSERT INTO auth_credentials "
                "(id, library_id, caller_id, selector, token_version, verifier, expires_at, "
                "created_at, updated_at, last_used_at, revoked_at, rotated_at, "
                "rotated_to_credential_id) VALUES "
                "(:operator_credential, :library, :operator, :operator_selector, 1, "
                ":operator_verifier, 1000, 10, 10, NULL, NULL, NULL, NULL), "
                "(:rotated, :library, :agent, :rotated_selector, 1, :rotated_verifier, "
                "1000, 10, 20, NULL, 20, 20, :active), "
                "(:active, :library, :agent, :active_selector, 1, :active_verifier, "
                "1000, 20, 20, NULL, NULL, NULL, NULL)"
            ),
            {
                "operator_credential": operator_credential,
                "library": library_id,
                "operator": operator_id,
                "operator_selector": "A" * 22,
                "operator_verifier": b"A" * 32,
                "rotated": rotated_credential,
                "agent": agent_id,
                "rotated_selector": "B" * 22,
                "rotated_verifier": b"B" * 32,
                "active": active_credential,
                "active_selector": "C" * 22,
                "active_verifier": b"C" * 32,
            },
        )
        connection.execute(
            text(
                "INSERT INTO operator_bootstrap_markers "
                "(library_id, operator_caller_id, initial_credential_id, created_at) "
                "VALUES (:library, :operator, :credential, 10)"
            ),
            {
                "library": library_id,
                "operator": operator_id,
                "credential": operator_credential,
            },
        )
        connection.execute(
            text(
                "INSERT INTO auth_section_grants "
                "(library_id, caller_id, section_id, action, created_at) "
                "VALUES (:library, :caller, :section, 'archive:write', 20)"
            ),
            {"library": library_id, "caller": agent_id, "section": section_id},
        )
        connection.execute(
            text(
                "INSERT INTO auth_audit_events "
                "(id, library_id, actor_caller_id, actor_credential_id, target_caller_id, "
                "section_id, section_action, action, resource_type, resource_id, outcome, "
                "request_id, policy_version_before, policy_version_after, occurred_at) "
                "VALUES (:id, :library, :actor, :credential, NULL, NULL, NULL, "
                "'auth.credential.rotate', 'credential', :resource, 'succeeded', "
                "'req_synthetic_backup', NULL, NULL, 20)"
            ),
            {
                "id": "f" * 32,
                "library": library_id,
                "actor": operator_id,
                "credential": operator_credential,
                "resource": rotated_credential,
            },
        )

        citation = ArchiveCitation(
            section_id=section_id,
            page_id=page.page_id,
            revision_id=revision.revision_id,
            revision_number=revision.revision_number,
            href=(f"/api/v1/sections/{section_id}/pages/{page.page_id}/revisions/1"),
        )
        body = ArchiveResponseBody(
            page=ArchivePageView(
                section_id=section_id,
                book_id=book_id,
                page_id=page.page_id,
                title=page.title,
                type="archive",
                occurred_at="2026-08-13T10:00:00.123456Z",
                current_revision_id=revision.revision_id,
                current_revision_number=1,
            ),
            revision=ArchiveRevisionView(
                page_id=page.page_id,
                revision_id=revision.revision_id,
                revision_number=1,
                created_at="1970-01-01T00:00:02.000000Z",
                content_sha256=revision.content_sha256.hex(),
                content=revision.content_md.decode("utf-8"),
            ),
            citation=citation,
        )
        connection.execute(
            text(
                "INSERT INTO idempotency_records "
                "(library_id, caller_id, method, route_template, key_digest, "
                "request_fingerprint, response_status, response_media_type, response_body, "
                "response_location, response_etag, original_request_id, "
                "original_request_timestamp) VALUES "
                "(:library, :caller, 'POST', :route, :key, :fingerprint, 201, "
                "'application/json', :body, :location, :etag, :request_id, :timestamp)"
            ),
            {
                "library": library_id,
                "caller": agent_id,
                "route": CREATE_ROUTE_TEMPLATE,
                "key": hashlib.sha256(b"synthetic key").digest(),
                "fingerprint": hashlib.sha256(b"synthetic request").digest(),
                "body": body.model_dump_json().encode("utf-8"),
                "location": f"/api/v1/sections/{section_id}/pages/{page.page_id}",
                "etag": page_current_etag(
                    page.page_uid,
                    revision.revision_id,
                    revision.revision_number,
                ),
                "request_id": "req_" + "1" * 32,
                "timestamp": "1970-01-01T00:00:02.000000Z",
            },
        )
    return engine


@pytest.fixture
def complete_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    engine = seed_complete_database(tmp_path / "source.sqlite", monkeypatch)
    try:
        yield engine
    finally:
        engine.dispose()
