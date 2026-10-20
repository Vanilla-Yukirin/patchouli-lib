"""The JSON search carries opaque cursors without exposing query text in URLs."""

from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from helpers import AdminWeb
from helpers import admin_web as admin_web

from patchouli_lib.database import immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index


def test_master_search_pages_exact_scope_and_rejects_stale_cursor(admin_web: AdminWeb) -> None:
    web = admin_web
    library_id, section_id, book_id = seed_library_structure(web.engine)
    expected = set()
    with immediate_transaction(web.engine) as connection:
        for number in range(1, 22):
            graph = page_graph_values(
                library_id=library_id,
                section_id=section_id,
                book_id=book_id,
                page_byte=number,
                revision_hex=f"{number:02x}",
                title=f"Page {number:02d}",
                content_md=b"needle <script>plain text</script>",
            )
            insert_page_graph(connection, graph, include_source=False)
            expected.add(graph[0].page_id)
    rebuild_search_index(web.engine)
    query = {"keywords": ["needle"], "libraries": [library_id], "limit": 20}
    first = web.post("/admin/api/search", query)
    assert first.status_code == 200, first.text
    assert "needle" not in str(first.request.url)
    body = first.json()
    assert len(body["items"]) == 20 and body["next_cursor"]
    assert body["items"][0]["snippet"]["text"] == "needle <script>plain text</script>"
    second = web.post("/admin/api/search", {**query, "cursor": body["next_cursor"]})
    assert second.status_code == 200 and len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None
    assert {item["page_id"] for item in body["items"] + second.json()["items"]} == expected
    invalid = web.post("/admin/api/search", {**query, "cursor": body["next_cursor"] + "x"})
    assert invalid.status_code in {400, 422}
    restarted = web.post("/admin/api/search", query)
    assert restarted.status_code == 200 and len(restarted.json()["items"]) == 20
