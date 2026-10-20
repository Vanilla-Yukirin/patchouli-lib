"""Scoped read-model pagination and audit validation, independent of HTML rendering."""

import pytest
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from helpers import AdminWeb
from helpers import admin_web as admin_web
from sqlalchemy import insert

from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.auth.models import AuditEvent, Caller, Credential, MasterAuditEvent
from patchouli_lib.database import immediate_transaction
from patchouli_lib.tags.models import Tag


def test_book_page_list_uses_bounded_keyset_without_crossing_scope(admin_web: AdminWeb) -> None:
    engine = admin_web.engine
    library, section, book = seed_library_structure(engine)
    other_library, other_section, other_book = seed_library_structure(
        engine, prefix="4", label="Other"
    )
    expected = set()
    with immediate_transaction(engine) as connection:
        for number in range(1, 23):
            graph = page_graph_values(
                library_id=library,
                section_id=section,
                book_id=book,
                page_byte=number,
                revision_hex=f"{number:02x}",
                title=f"Page {number:02d}",
            )
            insert_page_graph(connection, graph, include_source=False)
            expected.add(graph[0].page_id)
        foreign = page_graph_values(
            library_id=other_library,
            section_id=other_section,
            book_id=other_book,
            title="Foreign page",
        )
        insert_page_graph(connection, foreign, include_source=False)
    model = AdminReadModel(engine)
    first = model.get_book_page(library, section, book)
    assert first is not None and len(first.pages) == 20 and first.next_cursor
    second = model.get_book_page(library, section, book, first.next_cursor)
    assert second is not None and len(second.pages) == 2 and second.next_cursor is None
    assert {item.id for item in first.pages + second.pages} == expected
    assert model.get_book_page(other_library, section, book) is None
    assert model.get_book_page(library, other_section, book) is None
    with pytest.raises(ValueError):
        model.get_book_page(library, section, book, "malformed")
    assert (
        model.get_book_page(
            library,
            section,
            book,
            first.next_cursor.replace(first.pages[-1].id, foreign[0].page_id),
        )
        is None
    )


def test_activity_keyset_merges_sources_and_keeps_actor_scope(admin_web: AdminWeb) -> None:
    engine = admin_web.engine
    library, _, _ = seed_library_structure(engine)
    caller, credential = "b" * 32, "c" * 32
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Caller),
            dict(
                id=caller,
                library_id=library,
                kind="agent",
                name="Synthetic",
                description="",
                policy_version=1,
                created_at=1,
                updated_at=1,
            ),
        )
        connection.execute(
            insert(Credential),
            dict(
                id=credential,
                library_id=library,
                caller_id=caller,
                selector="a" * 22,
                token_version=1,
                verifier=b"v" * 32,
                expires_at=4_000_000,
                created_at=1,
                updated_at=1,
            ),
        )
        for number in range(1, 23):
            tag_id = f"{number:032x}"
            connection.execute(
                insert(Tag),
                dict(
                    library_id=library,
                    id=tag_id,
                    display_name=f"Tag {number}",
                    match_key=f"tag {number}",
                    created_at=1,
                ),
            )
            connection.execute(
                insert(AuditEvent),
                dict(
                    id=tag_id,
                    library_id=library,
                    actor_home_library_id=library,
                    actor_caller_id=caller,
                    actor_credential_id=credential,
                    action="tag.create",
                    resource_type="tag",
                    resource_id=tag_id,
                    outcome="succeeded",
                    request_id="synthetic-paging",
                    occurred_at=3_000_000,
                ),
            )
        connection.execute(
            insert(MasterAuditEvent),
            dict(
                id="f" * 32,
                identity_id=admin_web.session.identity_id,
                session_generation=1,
                session_fingerprint=b"s" * 32,
                action="tag.create",
                target_type="tag",
                target_id=f"{library}:{1:032x}",
                occurred_at=3_000_000,
            ),
        )
    model = AdminReadModel(engine)
    first = model.content_activity_page()
    assert len(first.items) == 20 and first.next_cursor
    second = model.content_activity_page(before=first.next_cursor)
    assert len(second.items) == 3 and second.next_cursor is None
    assert len({(item.actor_id, item.tag_id) for item in first.items + second.items}) == 23
    actor_first = model.content_activity_page(actor=(library, caller))
    assert len(actor_first.items) == 20 and actor_first.next_cursor
    actor_second = model.content_activity_page(
        actor=(library, caller), before=actor_first.next_cursor
    )
    assert len(actor_second.items) == 2
    assert all(item.actor_id == caller for item in actor_first.items + actor_second.items)
    assert len(model.recent_content_activity(limit=2)) == 2
    for invalid in ("bad", "01:a:" + "a" * 32, "1:x:" + "a" * 32, f"{1 << 63}:a:" + "a" * 32):
        with pytest.raises(ValueError):
            model.content_activity_page(before=invalid)
    for limit in (0, 51):
        with pytest.raises(ValueError):
            model.recent_content_activity(limit=limit)


@pytest.mark.parametrize(
    ("action", "target_type", "target_id"),
    [
        ("content.archive.restore", "tag", "malformed"),
        ("content.archive.restore", "page", f"{'a' * 32}:bad"),
        ("tag.create", "tag", "malformed"),
        ("tag.create", "tag", f"{'a' * 32}:bad"),
        ("tag.page.attach", "page_tag", "malformed"),
        ("tag.page.attach", "page_tag", f"{'a' * 32}:bad:{'c' * 32}"),
    ],
)
def test_activity_rejects_malformed_master_target_instead_of_truncating_page(
    admin_web: AdminWeb, action: str, target_type: str, target_id: str
) -> None:
    with immediate_transaction(admin_web.engine) as connection:
        connection.execute(
            insert(MasterAuditEvent),
            dict(
                id="b" * 32,
                identity_id=admin_web.session.identity_id,
                session_generation=1,
                session_fingerprint=b"s" * 32,
                action=action,
                target_type=target_type,
                target_id=target_id,
                occurred_at=3_000_000,
            ),
        )
    with pytest.raises(RuntimeError, match="Invalid content activity audit target"):
        AdminReadModel(admin_web.engine).content_activity_page()
