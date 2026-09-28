from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from time import time_ns

from fastapi.testclient import TestClient

from patchouli_lib import __version__
from patchouli_lib.app import create_app
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.config import Settings
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.models import Base


def _client_and_token(tmp_path: Path) -> tuple[TestClient, str]:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'skill.db').as_posix()}",
            "retrieval_cursor_signing_secret": "synthetic-skill-cursor-secret-32-bytes",
        }
    )
    app = create_app(settings)
    engine = app.state.engine
    Base.metadata.create_all(engine)
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
    return TestClient(app), token


def test_public_connection_instruction_contains_no_credential(tmp_path: Path) -> None:
    client, token = _client_and_token(tmp_path)
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
    client, token = _client_and_token(tmp_path)
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
    client, token = _client_and_token(tmp_path)
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
