from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from time import time_ns
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import insert

from patchouli_lib import __version__
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewSectionGrant, SectionAction
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.search.index_v2 import rebuild_search_index


def _client_and_token(
    tmp_path: Path, *, library_grants: bool = False
) -> tuple[TestClient, str, str, str, str]:
    database_url = f"sqlite:///{(tmp_path / 'skill.db').as_posix()}"
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": database_url,
            "retrieval_cursor_signing_secret": "synthetic-skill-cursor-secret-32-bytes",
        }
    )
    with patch.dict(
        os.environ,
        {"PATCHOULI_DATABASE_URL": database_url, "PATCHOULI_ENVIRONMENT": "test"},
    ):
        command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    app = create_app(settings)
    engine = app.state.engine
    now = time_ns() // 1_000
    with immediate_transaction(engine) as connection:
        identifiers = iter(("1" * 32, "2" * 32, "3" * 32))
        library = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: now,
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Skill Library",
                section_name="Synthetic Skill Section",
                book_name="Synthetic Skill Book",
            )
        )
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id="4" * 32,
                library_id=library.library.id,
                kind=CallerKind.AGENT,
                name="Synthetic Skill Agent",
                created_at=now,
                updated_at=now,
            )
        )
        token = (
            CredentialIssuer(auth, id_factory=lambda: "5" * 32, clock=lambda: now)
            .issue(caller, expires_at=now + 3_600_000_000)
            .value
        )
        for action in (SectionAction.ARCHIVE_WRITE, SectionAction.PAGE_READ):
            auth.add_grant(
                NewSectionGrant(
                    library_id=library.library.id,
                    caller_id=caller.id,
                    section_id=library.section.id,
                    action=action,
                    created_at=now,
                )
            )
        if library_grants:
            identity = {
                "credential_id": "5" * 32,
                "caller_id": caller.id,
                "home_library_id": library.library.id,
                "created_at": now,
            }
            connection.execute(
                insert(CredentialLibraryPolicy), {**identity, "mode": "library_grants"}
            )
            for permission in ("read", "write"):
                connection.execute(
                    insert(CredentialLibraryGrant),
                    {**identity, "target_library_id": library.library.id, "action": permission},
                )
    if library_grants:
        rebuild_search_index(engine)
    return TestClient(app), token, library.library.id, library.section.id, library.book.id


def test_public_connection_instruction_contains_no_credential(tmp_path: Path) -> None:
    client, token, _, _, _ = _client_and_token(tmp_path)
    response = client.get("/connect")
    assert response.status_code == 200
    assert "{{BASE_URL}}" in response.text
    assert "复制接入指令" in response.text
    assert token not in response.text
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    script = client.get("/connect.js")
    assert script.status_code == 200
    assert "window.location.origin" in script.text
    assert token not in script.text


def test_skill_manifest_and_each_file_require_bearer_and_disable_caching(
    tmp_path: Path,
) -> None:
    client, token, _, _, _ = _client_and_token(tmp_path)
    manifest_url = "/api/v1/agent/skill/manifest"
    for path in (manifest_url, "/api/v1/agent/skill/files/SKILL.md"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401

    headers = {"Authorization": f"Bearer {token}"}
    response = client.get(manifest_url, headers=headers)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    manifest = response.json()
    assert manifest["version"].startswith(f"{__version__}+")
    assert [entry["path"] for entry in manifest["files"]] == [
        "SKILL.md",
        "references/http.md",
        "references/local-token.md",
    ]
    source = Path(__file__).resolve().parents[2] / "skills" / "patchouli-agent"
    for entry in manifest["files"]:
        downloaded = client.get(entry["href"], headers=headers)
        assert downloaded.status_code == 200
        assert downloaded.headers["Cache-Control"] == "private, no-store"
        assert downloaded.headers["Content-Disposition"].startswith("attachment;")
        assert downloaded.content == (source / entry["path"]).read_bytes()
        assert entry["bytes"] == len(downloaded.content)
        assert entry["sha256"] == sha256(downloaded.content).hexdigest()


def test_skill_file_allowlist_rejects_unlisted_paths(tmp_path: Path) -> None:
    client, token, _, _, _ = _client_and_token(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    for path in (
        "/api/v1/agent/skill/files/missing.md",
        "/api/v1/agent/skill/files/references/other.md",
        "/api/v1/agent/skill/files/SKILL.md/extra",
        "/api/v1/agent/skill/files/%2e%2e/secret.md",
    ):
        assert client.get(path, follow_redirects=False).status_code == 401
        response = client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code in {400, 404}
        assert token not in response.text


def test_ai_native_http_path_reaches_multi_file_create_and_exact_revision_readback(
    tmp_path: Path,
) -> None:
    client, token, library_id, section_id, book_id = _client_and_token(
        tmp_path, library_grants=True
    )
    headers = {"Authorization": f"Bearer {token}"}
    identity = client.get("/api/v1/auth/whoami", headers=headers)
    capabilities = client.get("/api/v1/capabilities", headers=headers)
    assert identity.status_code == capabilities.status_code == 200
    assert identity.json()["name"] == "Synthetic Skill Agent"
    assert "file-sets" in capabilities.json()["features"]

    manifest = client.get("/api/v1/agent/skill/manifest", headers=headers)
    assert manifest.status_code == 200
    for entry in manifest.json()["files"]:
        downloaded = client.get(entry["href"], headers=headers)
        assert downloaded.status_code == 200
        assert len(downloaded.content) == entry["bytes"]
        assert sha256(downloaded.content).hexdigest() == entry["sha256"]

    create_path = f"/api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages"
    files: list[tuple[str, tuple[str | None, bytes, str]]] = [
        (
            "metadata",
            (
                None,
                json.dumps(
                    {"title": "Synthetic Guide Page", "source": {"kind": "manual"}}
                ).encode(),
                "application/json",
            ),
        ),
        ("file", ("content.md", b"# Synthetic guide\n", "text/markdown")),
        ("file", ("figure.png", b"\x89PNG\x00synthetic", "application/octet-stream")),
    ]
    created = client.post(
        create_path,
        headers={**headers, "Idempotency-Key": "synthetic-guide-create"},
        files=files,
    )
    assert created.status_code == 201, created.text
    assert token not in created.text
    body = created.json()
    assert [item["filename"] for item in body["files"]] == ["content.md", "figure.png"]
    exact_path = created.headers["Location"]
    exact = client.get(exact_path, headers=headers)
    assert exact.status_code == 200
    assert exact.json()["snapshot_sha256"] == body["snapshot_sha256"]
    for name, content in (
        ("content.md", b"# Synthetic guide\n"),
        ("figure.png", b"\x89PNG\x00synthetic"),
    ):
        downloaded = client.get(f"{exact_path}/{name}", headers=headers)
        assert downloaded.status_code == 200
        assert downloaded.content == content
        assert downloaded.headers["X-Content-Type-Options"] == "nosniff"

    replay = client.post(
        create_path,
        headers={**headers, "Idempotency-Key": "synthetic-guide-create"},
        files=files,
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["page_id"] == body["page_id"]

    # The index was ready before upload: Tag changes must be searchable without
    # rebuilding, re-uploading files, or creating another content Revision.
    directory = f"/api/v1/libraries/{library_id}/tags"
    page_tags = f"/api/v1/libraries/{library_id}/sections/{section_id}/pages/{body['page_id']}/tags"
    tag_ids = []
    for name in ("开发", "Archive", "未关联"):
        tag = client.post(directory, headers=headers, json={"name": name})
        assert tag.status_code == 201, tag.text
        tag_ids.append(tag.json()["tag_id"])
    reused = client.post(directory, headers=headers, json={"name": "ARCHIVE"})
    assert reused.status_code == 200
    assert reused.json()["tag_id"] == tag_ids[1]
    for tag_id in tag_ids[:2]:
        attached = client.put(f"{page_tags}/{tag_id}", headers=headers)
        assert attached.status_code == 200 and attached.json() == {"changed": True}
        repeated = client.put(f"{page_tags}/{tag_id}", headers=headers)
        assert repeated.status_code == 200 and repeated.json() == {"changed": False}
    listed = client.get(page_tags, headers=headers)
    assert listed.status_code == 200
    assert {item["tag_id"] for item in listed.json()["items"]} == set(tag_ids[:2])
    assert len(listed.json()["items"]) == 2

    def tagged_search(ids: list[str]) -> list[str]:
        found = client.post(
            "/api/v1/search",
            headers=headers,
            json={"tags_any": [{"library_id": library_id, "tag_id": tag_id} for tag_id in ids]},
        )
        assert found.status_code == 200, found.text
        return [item["page_id"] for item in found.json()["items"]]

    assert tagged_search(tag_ids[1:]) == [body["page_id"]]  # OR, not all Tags required.
    detached = client.delete(f"{page_tags}/{tag_ids[1]}", headers=headers)
    assert detached.status_code == 200 and detached.json() == {"changed": True}
    assert tagged_search(tag_ids[1:]) == []
    assert tagged_search(tag_ids[:1]) == [body["page_id"]]
    current = client.get(exact_path.split("/revisions/", 1)[0], headers=headers)
    assert current.status_code == 200
    assert current.json()["revision_id"] == body["revision_id"]
    assert current.json()["snapshot_sha256"] == body["snapshot_sha256"]
