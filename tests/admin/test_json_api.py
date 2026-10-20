"""Focused browser JSON paths, without a browser or duplicated UI matrix."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from patchouli_lib.admin.json_api import create_admin_json_router
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.backup.manifest import BackupArtifactIdentity
from patchouli_lib.backup.service import create_backup, restore_backup, verify_backup_bundle
from patchouli_lib.backup.validation import validate_database
from patchouli_lib.config import Settings
from patchouli_lib.database import CURRENT_SCHEMA_REVISION, build_engine, immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index

ORIGIN = "https://admin.example.invalid"
TOKEN = "synthetic workspace master token material"


@pytest.fixture
def browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, str], Any]]:
    database_url = f"sqlite:///{(tmp_path / 'workspace.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(
        environment="test",
        database_url=database_url,
        admin_session_signing_secret=SecretStr("s" * 32),
        retrieval_cursor_signing_secret=SecretStr("r" * 32),
    )
    engine = build_engine(database_url)
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(connection).initialize_from_local_cli(TOKEN, now=utc_microseconds())
    rebuild_search_index(engine)
    app = FastAPI()
    app.include_router(create_admin_json_router(engine, settings))
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(
            "/admin/api/session", json={"token": TOKEN}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 200
        headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
        yield client, headers, engine
    # Every browser journey must also produce an exact, restorable current-format backup.
    bundle = create_backup(
        engine,
        tmp_path / "verified-backup",
        artifact_identity=BackupArtifactIdentity("synthetic/workspace", "sha256:" + "1" * 64),
        app_version="0.1.0a0",
    )
    verified = verify_backup_bundle(bundle.bundle_path, app_version="0.1.0a0")
    assert verified.schema_revision == CURRENT_SCHEMA_REVISION
    destination = tmp_path / "restored.db"
    restore_backup(bundle.bundle_path, destination, app_version="0.1.0a0")
    with (
        closing(sqlite3.connect(bundle.database_path)) as before,
        closing(sqlite3.connect(destination)) as after,
    ):
        for table in (
            "pages",
            "revisions",
            "revision_files",
            "revision_presentations",
            "library_skills",
            "library_skill_revisions",
            "library_skill_files",
            "admin_workspace_save_receipts",
        ):
            assert (
                before.execute(f"SELECT * FROM {table}").fetchall()
                == after.execute(f"SELECT * FROM {table}").fetchall()
            )
    # An otherwise exact backup must reject a dangling entry or changed package bytes.
    # Only the disposable restored copy is changed; restore the exact trigger SQL so
    # schema-hash validation cannot hide a missing content-graph check.
    with closing(sqlite3.connect(destination)) as tampered:
        if tampered.execute("SELECT 1 FROM revision_presentations LIMIT 1").fetchone():
            trigger_name = "revision_presentations_no_update"
            statement = "UPDATE revision_presentations SET primary_file='missing.md'"
            parameters: tuple[object, ...] = ()
        elif row := tampered.execute(
            "SELECT rowid,content_bytes FROM library_skill_files LIMIT 1"
        ).fetchone():
            trigger_name = "library_skill_files_no_update"
            statement = "UPDATE library_skill_files SET content_bytes=? WHERE rowid=?"
            parameters = (b"X" + row[1][1:], row[0])
        else:
            trigger_name = None
        if trigger_name is not None:
            original_sql = tampered.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (trigger_name,)
            ).fetchone()[0]
            tampered.execute(f"DROP TRIGGER {trigger_name}")
            tampered.execute(statement, parameters)
            tampered.execute(original_sql)
            tampered.commit()
            with pytest.raises(BackupDatabaseError):
                validate_database(destination)
    engine.dispose()


def structure(client: TestClient, headers: dict[str, str]) -> tuple[str, str, str]:
    library = client.post(
        "/admin/api/libraries",
        json={"name": "Library", "description": "Human summary", "readme": "# Rules\n"},
        headers=headers,
    )
    assert library.status_code == 201, library.text
    library_id = library.json()["id"]
    section = client.post(
        f"/admin/api/libraries/{library_id}/sections",
        json={"name": "Section", "description": "Project category"},
        headers=headers,
    )
    assert section.status_code == 201, section.text
    section_id = section.json()["id"]
    book = client.post(
        f"/admin/api/sections/{section_id}/books",
        json={"name": "Book", "summary": "Context"},
        headers=headers,
    )
    assert book.status_code == 201, book.text
    return library_id, section_id, book.json()["id"]


def test_session_boundary_and_structure(browser: tuple[TestClient, dict[str, str], Any]) -> None:
    client, headers, _ = browser
    assert client.get("/admin/api/session").json()["authenticated"] is True
    assert (
        client.post(
            "/admin/api/libraries", json={"name": "Rejected"}, headers={"Origin": ORIGIN}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/api/libraries",
            json={"name": "Rejected"},
            headers={**headers, "Origin": "https://other.example.invalid"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/admin/api/libraries", headers={"Authorization": "Bearer synthetic"}
        ).status_code
        == 401
    )
    library_id, section_id, book_id = structure(client, headers)
    assert (
        client.get(f"/admin/api/libraries/{library_id}").json()["library"]["readme"] == "# Rules\n"
    )
    assert client.get(f"/admin/api/sections/{section_id}").json()["books"][0]["id"] == book_id
    current = client.get(f"/admin/api/libraries/{library_id}").json()["library"]
    form = {
        "name": "Renamed",
        "description": "Summary",
        "readme": "\n# New rules\n",
        "expected_updated_at": current["updated_at"],
    }
    assert (
        client.put(f"/admin/api/libraries/{library_id}", json=form, headers=headers).status_code
        == 200
    )
    assert (
        client.put(f"/admin/api/libraries/{library_id}", json=form, headers=headers).status_code
        == 409
    )
    section = client.get(f"/admin/api/sections/{section_id}").json()["section"]
    book = client.get(f"/admin/api/books/{book_id}").json()["book"]
    for kind, item, summary in (("sections", section, "description"), ("books", book, "summary")):
        edited = client.put(
            f"/admin/api/{kind}/{item['id']}",
            json={
                "name": item["name"],
                summary: "Updated purpose",
                "readme": "# Instructions\n",
                "expected_updated_at": item["updated_at"],
            },
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["readme"] == "# Instructions\n"
    assert len(client.get("/admin/api/dashboard").json()["libraries"]) == 1
    assert client.get("/admin/api/requests").json()["items"] == []
    schema = client.get("/admin/api/openapi.json")
    assert schema.status_code == 200
    assert "SessionState" in schema.json()["components"]["schemas"]
    assert (
        schema.json()["paths"]["/admin/api/session"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]["token"]["writeOnly"]
        is True
    )
    assert (
        client.post(
            "http://admin.example.invalid/admin/api/session",
            json={"token": TOKEN},
            headers={"Origin": "http://admin.example.invalid"},
        ).status_code
        == 403
    )
    rotated = client.post(
        "/admin/api/master-token/rotate",
        json={
            "current_token": TOKEN,
            "new_token": "rotated synthetic workspace master token",
            "confirmation": "rotated synthetic workspace master token",
        },
        headers=headers,
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["requires_sign_in"] is True
    assert client.get("/admin/api/libraries").status_code == 401
    assert (
        client.post(
            "/admin/api/session", json={"token": TOKEN}, headers={"Origin": ORIGIN}
        ).status_code
        == 401
    )
    signed_in = client.post(
        "/admin/api/session",
        json={"token": "rotated synthetic workspace master token"},
        headers={"Origin": ORIGIN},
    )
    assert signed_in.status_code == 200 and signed_in.json()["authenticated"] is True
    signed_out = client.delete(
        "/admin/api/session",
        headers={"Origin": ORIGIN, "X-CSRF-Token": signed_in.json()["csrf_token"]},
    )
    assert signed_out.status_code == 200 and signed_out.json()["authenticated"] is False
    assert client.get("/admin/api/session").json()["authenticated"] is False
    assert client.get("/admin/api/libraries").status_code == 401


def test_manual_file_save_history_and_retry(
    browser: tuple[TestClient, dict[str, str], Any],
) -> None:
    client, headers, _ = browser
    library_id, section_id, book_id = structure(client, headers)
    payload = {
        "primary_file": "entry.md",
        "edits": [{"name": "entry.md", "content": "# First title\n"}],
    }
    created = client.post(
        f"/admin/api/books/{book_id}/pages",
        data={"metadata": json.dumps(payload)},
        files=[("files", ("attachment.bin", b"\x00\xff"))],
        headers={**headers, "Idempotency-Key": "workspace-create-1"},
    )
    assert created.status_code == 201, created.text
    value = created.json()
    path = f"/admin/api/libraries/{library_id}/pages/{value['page_id']}"
    view = client.get(path).json()
    assert view["stored_title"] == "First title"
    assert view["primary_file"] == "entry.md"
    assert len(view["files"]) == 2
    text = next(item for item in view["files"] if item["name"] == "entry.md")
    assert client.get(text["text_href"]).text == "# First title\n"
    attachment = next(item for item in view["files"] if item["name"] == "attachment.bin")
    assert attachment["editable"] is False and attachment["text_href"] is None
    newer = {
        "primary_file": "entry.md",
        "base_etag": value["etag"],
        "edits": [{"name": "entry.md", "content": "# Second title\n"}],
    }
    second = client.put(
        path + "/files", json=newer, headers={**headers, "Idempotency-Key": "workspace-save-2"}
    )
    assert second.status_code == 200, second.text
    stale = {**newer, "edits": [{"name": "entry.md", "content": "# Third title\n"}]}
    saved = client.put(
        path + "/files", json=stale, headers={**headers, "Idempotency-Key": "workspace-save-3"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["overwritten_newer_revision"] is True
    replay = client.put(
        path + "/files", json=stale, headers={**headers, "Idempotency-Key": "workspace-save-3"}
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["revision_id"] == saved.json()["revision_id"]
    assert len(client.get(path).json()["files"]) == 2
    old = client.get(path, params={"revision_number": 1}).json()
    assert old["stored_title"] == "First title"
    assert client.get(attachment["download_href"]).content == b"\x00\xff"
    assert old["revisions"][0]["revision_id"] == saved.json()["revision_id"]
    restore = client.post(
        path + "/revisions/1/restore",
        json={"expected_etag": saved.json()["etag"], "confirm_restore": "yes"},
        headers={**headers, "Idempotency-Key": "workspace-restore-1"},
    )
    assert restore.status_code == 200, restore.text
    restored = client.get(path).json()
    assert restored["primary_file"] == "entry.md" and restored["stored_title"] == "First title"
    assert (
        client.put(
            path + "/files", json=newer, headers={**headers, "Idempotency-Key": "workspace-save-3"}
        ).status_code
        == 409
    )
    # Metadata-only entry changes, attachment renames, and byte uploads share one Save.
    renamed = client.put(
        path + "/files",
        json={"primary_file": "renamed.md", "renames": [{"from": "entry.md", "to": "renamed.md"}]},
        headers={**headers, "Idempotency-Key": "workspace-rename-5"},
    )
    assert renamed.status_code == 200, renamed.text
    assert client.get(path).json()["primary_file"] == "renamed.md"
    unchanged = client.put(
        path + "/files",
        json={"primary_file": "renamed.md"},
        headers={**headers, "Idempotency-Key": "workspace-noop"},
    )
    assert unchanged.status_code == 200 and unchanged.json()["changed"] is False
    activity = client.get("/admin/api/activity").json()["items"]
    saves = [item for item in activity if item["action"] == "content.page.workspace_save"]
    assert [(item["revision_id"], item["stored_title"]) for item in saves] == [
        (renamed.json()["revision_id"], "First title"),
        (saved.json()["revision_id"], "Third title"),
        (second.json()["revision_id"], "Second title"),
        (value["revision_id"], "First title"),
    ]
    restores = [item for item in activity if item["action"] == "content.page.revision_restore"]
    assert len(restores) == 1 and restores[0]["revision_id"] == restore.json()["revision_id"]
    assert client.get("/admin/api/dashboard").json()["activity"][0]["page_id"] == value["page_id"]
    current = client.get(path).json()
    conflict = client.put(
        path + "/files",
        json={
            "primary_file": "renamed.md",
            "renames": [{"from": "renamed.md", "to": "attachment.bin"}],
        },
        headers={**headers, "Idempotency-Key": "workspace-collision"},
    )
    assert conflict.status_code == 409, conflict.text
    invalid_entry = client.put(
        path + "/files",
        json={"primary_file": "missing.md", "remove": ["attachment.bin"]},
        headers={**headers, "Idempotency-Key": "workspace-invalid-entry"},
    )
    assert invalid_entry.status_code == 422, invalid_entry.text
    assert client.get(path).json() == current
    mixed = client.put(
        path + "/files",
        data={
            "metadata": json.dumps(
                {
                    "primary_file": "renamed.md",
                    "remove": ["attachment.bin"],
                    "renames": [{"from": "renamed.md", "to": "attachment.bin"}],
                    "edits": [{"name": "renamed.md", "content": "# First title\n"}],
                }
            )
        },
        files=[("files", ("replacement.bin", b"\x00\xff"))],
        headers={**headers, "Idempotency-Key": "workspace-combined-file-changes"},
    )
    assert mixed.status_code == 200, mixed.text
    modified = client.get(path).json()
    expected = {
        "attachment.bin": b"# First title\n",
        "renamed.md": b"# First title\n",
        "replacement.bin": b"\x00\xff",
    }
    assert {
        item["name"]: client.get(item["download_href"]).content for item in modified["files"]
    } == expected
    assert modified["stored_title"] == "First title" and modified["primary_file"] == "renamed.md"
    assert client.get(attachment["download_href"]).content == b"\x00\xff"
    tag = client.post(
        f"/admin/api/libraries/{library_id}/tags", json={"name": "Draft"}, headers=headers
    )
    assert tag.status_code == 201, tag.text
    tag_id = tag.json()["id"]
    assert (
        client.post(
            path + "/tags", json={"tag_id": tag_id, "operation": "attach"}, headers=headers
        ).json()["changed"]
        is True
    )
    tagged = client.get(f"/admin/api/libraries/{library_id}/tags/{tag_id}")
    assert tagged.status_code == 200 and tagged.json()["pages"][0]["stored_title"] == "First title"
    assert client.get("/admin/api/tags").json()["items"][0]["tag"]["page_count"] == 1
    change = {
        "library_id": library_id,
        "tag_id": tag_id,
        "expected_name": "Draft",
        "new_name": "Ready",
    }
    assert (
        client.post(
            "/admin/api/tags/batch",
            json={"operation": "rename", "changes": [change]},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            path + "/tags", json={"tag_id": tag_id, "operation": "detach"}, headers=headers
        ).json()["changed"]
        is True
    )
    change = {"library_id": library_id, "tag_id": tag_id, "expected_name": "Ready"}
    assert (
        client.post(
            "/admin/api/tags/batch",
            json={"operation": "delete", "changes": [change], "confirm_delete": True},
            headers=headers,
        ).status_code
        == 200
    )
    match = client.post(
        "/admin/api/search",
        json={"keywords": ["First"], "libraries": [library_id]},
        headers=headers,
    )
    assert match.status_code == 200, match.text
    assert match.json()["items"][0]["stored_title"] == "First title"
    current = client.get(path).json()
    assert (
        client.post(
            path + "/occurrence",
            json={"occurred_at": "2026-01-01T00:00:00Z", "expected_etag": current["current_etag"]},
            headers=headers,
        ).status_code
        == 200
    )
    target = client.post(
        f"/admin/api/sections/{section_id}/books", json={"name": "Destination"}, headers=headers
    )
    assert target.status_code == 201, target.text
    current = client.get(path).json()
    moved = client.post(
        path + "/move",
        json={
            "source_section_id": section_id,
            "source_book_id": book_id,
            "target_section_id": section_id,
            "target_book_id": target.json()["id"],
            "expected_etag": current["current_etag"],
        },
        headers={**headers, "Idempotency-Key": "workspace-move"},
    )
    assert moved.status_code == 200, moved.text
    current = client.get(path).json()
    assert current["book"]["id"] == target.json()["id"]
    deleted = client.request(
        "DELETE",
        path,
        json={"expected_etag": current["current_etag"], "confirm_delete": "yes"},
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    trash = client.get("/admin/api/trash", params={"library_id": library_id})
    assert trash.status_code == 200, trash.text
    trashed = trash.json()["pages"][0]
    assert trashed["stored_title"] == "First title"
    assert client.get("/admin/api/trash").json()["libraries"]
    assert (
        client.post(
            path + "/restore", json={"expected_etag": trashed["restore_etag"]}, headers=headers
        ).status_code
        == 200
    )
    assert client.get(path).json()["stored_title"] == "First title"


def test_skill_package_and_credentials(browser: tuple[TestClient, dict[str, str], Any]) -> None:
    client, headers, _ = browser
    library_id, _, _ = structure(client, headers)
    path = f"/admin/api/libraries/{library_id}/skills"
    payload = {
        "name": "project-guide",
        "description": "Library workflow",
        "edits": [
            {
                "name": "SKILL.md",
                "content": (
                    "---\nname: project-guide\ndescription: Library workflow\n---\n# Guide\n"
                ),
            },
            {"name": "scripts/check.py", "content": "print('example')\n"},
        ],
    }
    created = client.post(
        path, json=payload, headers={**headers, "Idempotency-Key": "skill-create-1"}
    )
    assert created.status_code == 201, created.text
    skill = created.json()["skill"]
    detail = client.get(path + "/" + skill["id"])
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["files"]) == 2
    newer = {
        "name": "project-guide",
        "description": "Updated workflow",
        "base_etag": skill["etag"],
        "edits": [{"name": "SKILL.md", "content": "# New guide\n"}],
    }
    updated = client.put(
        path + "/" + skill["id"], json=newer, headers={**headers, "Idempotency-Key": "skill-save-2"}
    )
    assert updated.status_code == 200, updated.text
    restored = client.post(
        path + "/" + skill["id"] + "/revisions/" + skill["revision_id"] + "/restore",
        json={"expected_etag": updated.json()["skill"]["etag"], "confirm_restore": "yes"},
        headers={**headers, "Idempotency-Key": "skill-restore-3"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["skill"]["description"] == "Library workflow"
    assert (
        client.get(path + "/" + skill["id"] + "/download").headers["content-type"]
        == "application/zip"
    )
    exact = client.get(
        path + "/" + skill["id"] + "/manifest",
        params={"revision_id": updated.json()["skill"]["revision_id"]},
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["revision_id"] == updated.json()["skill"]["revision_id"]
    expected_files = {"SKILL.md": b"# New guide\n", "scripts/check.py": b"print('example')\n"}
    for entry in exact.json()["files"]:
        downloaded = client.get(entry["download_href"])
        assert downloaded.status_code == 200
        assert downloaded.content == expected_files[entry["path"]]
        assert len(downloaded.content) == entry["bytes"]
        assert hashlib.sha256(downloaded.content).hexdigest() == entry["sha256"]
    assert len(client.get(path).json()["items"]) == 1
    assert len(client.get(path + "/" + skill["id"] + "/versions").json()["items"]) == 3
    historical = client.get(
        path + "/" + skill["id"], params={"revision_id": skill["revision_id"]}
    ).json()
    assert client.get(historical["files"][0]["text_href"]).text.startswith("---")
    activity = client.get("/admin/api/activity").json()["items"]
    assert [item["revision_id"] for item in activity] == [
        restored.json()["skill"]["revision_id"],
        updated.json()["skill"]["revision_id"],
        skill["revision_id"],
    ]
    assert all(
        item["skill_id"] == skill["id"]
        and item["stored_title"] == "project-guide"
        and item["page_id"] is None
        for item in activity
    )
    credential = client.post(
        "/admin/api/credentials",
        json={
            "agent_name": "Synthetic Agent",
            "agent_description": "Dedicated integration",
            "never_expires": True,
            "read_mode": "exclude",
            "write_mode": "include",
            "write_libraries": [library_id],
        },
        headers=headers,
    )
    assert credential.status_code == 201, credential.text
    identity = credential.json()
    assert isinstance(identity["token"], str)
    shown = client.get(
        f"/admin/api/credentials/{identity['caller_id']}",
        params={"library_id": identity["library_id"]},
    )
    assert shown.status_code == 200 and "token" not in shown.json()
    caller_path = f"/admin/api/credentials/{identity['caller_id']}"
    params = {"library_id": identity["library_id"]}
    credential_path = f"{caller_path}/{identity['credential_id']}"
    assert (
        client.post(credential_path + "/reveal", params=params, json={}, headers=headers).json()[
            "token"
        ]
        == identity["token"]
    )
    assert (
        client.put(
            caller_path,
            params=params,
            json={
                "name": "Updated Agent",
                "description": "Updated purpose",
                "expected_updated_at": shown.json()["updated_at"],
            },
            headers=headers,
        ).json()["changed"]
        is True
    )
    scope = shown.json()["credentials"][0]
    changed_scope = client.put(
        credential_path + "/scope",
        params=params,
        json={
            "expected_scope_revision": scope["scope_revision"],
            "read_mode": "exclude",
            "write_mode": "include",
        },
        headers=headers,
    )
    assert changed_scope.status_code == 200, changed_scope.text
    rotated = client.post(
        credential_path + "/rotate",
        params=params,
        json={"credential_ttl_seconds": 3600},
        headers=headers,
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["token"] != identity["token"]
    assert (
        client.post(
            credential_path + "/reveal", params=params, json={}, headers=headers
        ).status_code
        == 410
    )
    assert (
        client.post(
            f"{caller_path}/{rotated.json()['credential_id']}/revoke",
            params=params,
            json={},
            headers=headers,
        ).json()["changed"]
        is True
    )
    assert (
        client.post(caller_path + "/disable", params=params, json={}, headers=headers).json()[
            "changed"
        ]
        is True
    )
    assert client.get("/admin/api/credentials").json()["items"][0]["disabled_at"] is not None
    ticket = client.post("/admin/api/onboarding-tickets", json={}, headers=headers)
    assert ticket.status_code == 201 and len(ticket.json()["code"]) == 16
