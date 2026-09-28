"""Browser Tag writes require both the admin session and a live Operator token."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, update

from patchouli_lib.admin.contracts import BootstrapInput, ProvisionAgentInput
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import AuditEvent, Caller, Credential
from patchouli_lib.auth.schemas import SectionAction
from patchouli_lib.config import Settings
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import MarkdownContent, NewPage, NewPageIdentifier, NewRevision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.tags.models import PageTag, Tag
from patchouli_lib.tags.service import TagService

_ORIGIN = "https://admin.example.invalid"
_PASSWORD = "synthetic browser password"
_PASSWORD_HASH = hash_password(_PASSWORD, salt_factory=lambda size: b"t" * size, iterations=300_000)


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[tuple[TestClient, Engine]]:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'tag-write.db').as_posix()}",
            "admin_password_hash": _PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
        }
    )
    app = create_app(settings)
    Caller.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client, app.state.engine


def _login(client: TestClient) -> str:
    assert (
        client.post(
            "/admin/login", data={"password": _PASSWORD}, headers={"Origin": _ORIGIN}
        ).status_code
        == 303
    )
    response = client.get("/admin/libraries")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _seed(engine: Engine, *, suffix: str) -> tuple[str, str, str, str]:
    operator = AdminActionService(engine).bootstrap(
        BootstrapInput(
            library_name=f"Synthetic {suffix} Library",
            section_name=f"Synthetic {suffix} Section",
            book_name=f"Synthetic {suffix} Book",
            operator_name=f"Synthetic {suffix} Operator",
            credential_ttl_seconds=3_600,
        )
    )
    with engine.connect() as connection:
        library = LibraryRepository(connection)
        section = library.find_section_by_name(operator.library_id, f"Synthetic {suffix} Section")
        assert section is not None
        book = library.find_book_by_name(
            operator.library_id, section.id, f"Synthetic {suffix} Book"
        )
        assert book is not None
    return operator.library_id, section.id, book.id, operator.value


def _seed_page(
    engine: Engine, library_id: str, section_id: str, book_id: str, *, title: str = "Synthetic Page"
) -> str:
    occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
    identifier = generate_page_id(occurrence, title)
    uid = b"p" * 16
    content = MarkdownContent.from_bytes(b"# Synthetic content\n")
    revision_id = "rev_" + uid.hex()
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        repository.add_page(
            NewPage(
                library_id=library_id,
                page_uid=uid,
                section_id=section_id,
                book_id=book_id,
                page_id=identifier.value,
                id_scheme=PAGE_ID_SCHEME,
                id_timestamp_micros=(occurrence.utc_microseconds // 1_000) * 1_000,
                base_slug=identifier.base_slug,
                collision_ordinal=identifier.collision_ordinal,
                title=title,
                page_type="archive",
                occurred_at=occurrence.utc_microseconds,
                current_revision_id=revision_id,
                current_revision_number=1,
                created_at=2_000_000,
                updated_at=2_000_000,
            )
        )
        repository.add_revision(
            NewRevision(
                library_id=library_id,
                revision_id=revision_id,
                page_uid=uid,
                revision_number=1,
                created_at=2_000_000,
                **content.model_dump(),
            )
        )
        repository.add_identifier(
            NewPageIdentifier(
                library_id=library_id,
                identifier_digest=page_id_registry_digest(identifier.value),
                identifier_text=identifier.value,
                id_scheme=PAGE_ID_SCHEME,
                identifier_kind="canonical",
                page_uid=uid,
                created_at=2_000_000,
            )
        )
    return identifier.value


def _post(client: TestClient, path: str, csrf: str, **fields: str):  # type: ignore[no-untyped-def]
    return client.post(path, data={"csrf_token": csrf, **fields}, headers={"Origin": _ORIGIN})


def _count(engine: Engine, table: type[Tag] | type[PageTag] | type[AuditEvent]) -> int:
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table)) or 0


def test_tag_creation_requires_operator_and_is_idempotent(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, _, operator = _seed(engine, suffix="One")
    other_library, _, _, other_operator = _seed(engine, suffix="Two")
    agent = AdminActionService(engine).provision_agent(
        ProvisionAgentInput(
            library_name="Synthetic One Library",
            section_name="Synthetic One Section",
            agent_name="Synthetic Agent",
            credential_ttl_seconds=3_600,
            grants=(SectionAction.QUERY,),
            operator_token=SecretStr(operator),
        )
    )
    base = f"/admin/libraries/{library}/tags"
    assert (
        client.post(base, data={"name": "Private"}, headers={"Origin": _ORIGIN}).status_code == 401
    )
    csrf = _login(client)
    directory = client.get(base)
    assert directory.status_code == 200
    assert directory.headers["cache-control"] == "no-store, max-age=0"
    assert 'name="operator_token"' in directory.text
    assert operator not in directory.text
    assert 'action="' + base + '"' in directory.text

    for token, expected in ((agent.value, 403), (other_operator, 403), ("invalid", 403)):
        rejected = _post(client, base, csrf, name="Private", operator_token=token)
        assert rejected.status_code == expected
        assert token not in rejected.text
        assert _count(engine, Tag) == 0
    with immediate_transaction(engine) as connection:
        connection.execute(
            update(Credential)
            .where(Credential.library_id == other_library)
            .values(expires_at=Credential.updated_at + 1)
        )
    expired = _post(
        client,
        f"/admin/libraries/{other_library}/tags",
        csrf,
        name="Late",
        operator_token=other_operator,
    )
    assert expired.status_code == 403
    assert _count(engine, Tag) == 0

    created = _post(client, base, csrf, name="<script>alert(1)</script>", operator_token=operator)
    assert created.status_code == 303
    assert operator not in created.headers["location"]
    assert created.headers["cache-control"] == "no-store, max-age=0"
    detail = client.get(created.headers["location"])
    assert "标签已创建。" in detail.text or "Tag created." in detail.text
    assert "Tag created." not in client.get(created.headers["location"]).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail.text
    assert "<script>alert(1)</script>" not in detail.text
    assert operator not in detail.text
    duplicate = _post(client, base, csrf, name="<script>alert(1)</script>", operator_token=operator)
    assert duplicate.status_code == 303
    assert "result=" not in duplicate.headers["location"]
    assert "nothing changed" in client.get(duplicate.headers["location"]).text
    assert "nothing changed" not in client.get(duplicate.headers["location"]).text
    assert _count(engine, Tag) == 1
    with engine.connect() as connection:
        actions = connection.scalars(select(AuditEvent.action)).all()
    assert actions.count("tag.create") == 1
    assert section not in created.headers["location"]


def test_tag_forms_reject_wrong_origin_csrf_and_duplicate_fields(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, _, _, operator = _seed(engine, suffix="One")
    csrf = _login(client)
    base = f"/admin/libraries/{library}/tags"
    wrong_origin = client.post(
        base,
        data={"csrf_token": csrf, "name": "Blocked", "operator_token": operator},
        headers={"Origin": "https://different.invalid"},
    )
    assert wrong_origin.status_code == 403
    wrong_csrf = _post(client, base, "wrong", name="Blocked", operator_token=operator)
    assert wrong_csrf.status_code == 403
    duplicate = client.post(
        base,
        content=f"csrf_token={csrf}&name=First&name=Second&operator_token={operator}",
        headers={"Origin": _ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate.status_code == 422
    assert _count(engine, Tag) == 0


def test_tag_success_notice_cannot_be_forged_by_query_or_cookie(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, _, _, operator = _seed(engine, suffix="One")
    csrf = _login(client)
    created = _post(
        client,
        f"/admin/libraries/{library}/tags",
        csrf,
        name="Synthetic Tag",
        operator_token=operator,
    )
    detail_path = created.headers["location"]
    assert "Tag created." in client.get(detail_path).text
    assert "Tag created." not in client.get(detail_path + "?result=created").text
    client.cookies.set("patchouli_admin_tag_result", "forged.payload", path="/admin")
    assert "Tag created." not in client.get(detail_path).text


def test_page_tag_attach_detach_scoping_and_noop_feedback(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    library, section, book, operator = _seed(engine, suffix="One")
    other_library, other_section, other_book, other_operator = _seed(engine, suffix="Two")
    page_id = _seed_page(engine, library, section, book)
    other_page_id = _seed_page(engine, other_library, other_section, other_book, title="Other Page")
    csrf = _login(client)
    tag_create = _post(
        client,
        f"/admin/libraries/{library}/tags",
        csrf,
        name="Important",
        operator_token=operator,
    )
    assert tag_create.status_code == 303
    tag_id = tag_create.headers["location"].split("/tags/", 1)[1].split("?", 1)[0]
    other_tag_create = _post(
        client,
        f"/admin/libraries/{other_library}/tags",
        csrf,
        name="Other Library Tag",
        operator_token=other_operator,
    )
    other_tag_id = other_tag_create.headers["location"].split("/tags/", 1)[1].split("?", 1)[0]
    agent = AdminActionService(engine).provision_agent(
        ProvisionAgentInput(
            library_name="Synthetic One Library",
            section_name="Synthetic One Section",
            agent_name="Synthetic writer",
            credential_ttl_seconds=3_600,
            grants=(SectionAction.PAGE_READ, SectionAction.ARCHIVE_WRITE),
            operator_token=SecretStr(operator),
        )
    )
    page_path = f"/admin/libraries/{library}/sections/{section}/books/{book}/pages/{page_id}"
    page = client.get(page_path)
    assert 'name="tag_id"' in page.text
    assert operator not in page.text
    assert "Important" in page.text

    for path, token, candidate_tag in (
        (page_path, other_operator, tag_id),
        (page_path, agent.value, tag_id),
        (page_path, operator, other_tag_id),
        (
            f"/admin/libraries/{library}/sections/{section}/books/{other_book}/pages/{page_id}",
            operator,
            tag_id,
        ),
        (
            f"/admin/libraries/{library}/sections/{section}/books/{book}/pages/{other_page_id}",
            operator,
            tag_id,
        ),
    ):
        rejected = _post(
            client,
            path + "/tags",
            csrf,
            tag_id=candidate_tag,
            operation="attach",
            operator_token=token,
        )
        assert rejected.status_code in {403, 404}
        assert token not in rejected.text
        assert _count(engine, PageTag) == 0
    origin_rejected = client.post(
        page_path + "/tags",
        data={
            "csrf_token": csrf,
            "tag_id": tag_id,
            "operation": "attach",
            "operator_token": operator,
        },
        headers={"Origin": "https://different.invalid"},
    )
    assert origin_rejected.status_code == 403
    csrf_rejected = _post(
        client,
        page_path + "/tags",
        "wrong",
        tag_id=tag_id,
        operation="attach",
        operator_token=operator,
    )
    assert csrf_rejected.status_code == 403
    assert _count(engine, PageTag) == 0
    attached = _post(
        client,
        page_path + "/tags",
        csrf,
        tag_id=tag_id,
        operation="attach",
        operator_token=operator,
    )
    assert attached.status_code == 303
    assert "result=" not in attached.headers["location"]
    assert "Tag attached." in client.get(attached.headers["location"]).text
    assert _count(engine, PageTag) == 1
    repeated = _post(
        client,
        page_path + "/tags",
        csrf,
        tag_id=tag_id,
        operation="attach",
        operator_token=operator,
    )
    assert "result=" not in repeated.headers["location"]
    assert "nothing changed" in client.get(repeated.headers["location"]).text
    removed = _post(
        client,
        page_path + "/tags",
        csrf,
        tag_id=tag_id,
        operation="detach",
        operator_token=operator,
    )
    assert "result=" not in removed.headers["location"]
    assert "Tag removed." in client.get(removed.headers["location"]).text
    assert _count(engine, PageTag) == 0
    repeated_remove = _post(
        client,
        page_path + "/tags",
        csrf,
        tag_id=tag_id,
        operation="detach",
        operator_token=operator,
    )
    assert "result=" not in repeated_remove.headers["location"]
    with engine.connect() as connection:
        actions = connection.scalars(select(AuditEvent.action)).all()
    assert actions.count("tag.page.attach") == 1
    assert actions.count("tag.page.detach") == 1
    assert "No content activity yet." in client.get("/admin").text


def test_page_tag_attachment_rolls_back_if_audit_fails(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    library, section, book, operator = _seed(engine, suffix="One")
    page_id = _seed_page(engine, library, section, book)
    csrf = _login(client)
    created = _post(
        client,
        f"/admin/libraries/{library}/tags",
        csrf,
        name="Needs audit",
        operator_token=operator,
    )
    tag_id = created.headers["location"].split("/tags/", 1)[1].split("?", 1)[0]

    def reject_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(TagService, "_audit", reject_audit)
    path = f"/admin/libraries/{library}/sections/{section}/books/{book}/pages/{page_id}/tags"
    response = _post(
        client,
        path,
        csrf,
        tag_id=tag_id,
        operation="attach",
        operator_token=operator,
    )
    assert response.status_code == 500
    assert operator not in response.text
    assert _count(engine, PageTag) == 0


def test_tag_write_rolls_back_when_audit_fails(
    browser: tuple[TestClient, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, engine = browser
    library, _, _, operator = _seed(engine, suffix="One")
    csrf = _login(client)

    def reject_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(TagService, "_audit", reject_audit)
    response = _post(
        client,
        f"/admin/libraries/{library}/tags",
        csrf,
        name="Must rollback",
        operator_token=operator,
    )
    assert response.status_code == 500
    assert operator not in response.text
    assert _count(engine, Tag) == 0
