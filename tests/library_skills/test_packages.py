"""One package journey and pure path checks; no browser matrix."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from content.conftest import content_engine as content_engine
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, insert

from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.library_skill_routes import create_library_skill_router
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.auth.service import CredentialIssuer, utc_microseconds
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewLibrary
from patchouli_lib.library_skills.backup import (
    CREATE_SKILL_ROUTE,
    REVISE_SKILL_ROUTE,
    validate_skill_replay,
)
from patchouli_lib.library_skills.install import install_skill_package
from patchouli_lib.library_skills.service import (
    SkillConflictError,
    SkillValidationError,
    build_skill_snapshot,
)


def test_package_journey_permissions_history_and_replay(content_engine: Engine) -> None:
    now = utc_microseconds()
    library = "1" * 32
    with immediate_transaction(content_engine) as connection:
        LibraryRepository(connection).add_library(
            NewLibrary(id=library, name="Synthetic", created_at=now, updated_at=now)
        )
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id="2" * 32,
                library_id=library,
                kind=CallerKind.AGENT,
                name="Synthetic",
                created_at=now,
                updated_at=now,
            )
        )
        issued = CredentialIssuer(auth, clock=lambda: now, id_factory=lambda: "3" * 32).issue(
            caller, expires_at=None
        )
        identity = {
            "credential_id": issued.credential.id,
            "caller_id": caller.id,
            "home_library_id": library,
            "created_at": now,
        }
        connection.execute(insert(CredentialLibraryPolicy), {**identity, "mode": "library_grants"})
        for action in ("read", "write"):
            connection.execute(
                insert(CredentialLibraryGrant),
                {**identity, "target_library_id": library, "action": action},
            )
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    install_api_exception_handlers(app)
    app.include_router(create_library_skill_router(content_engine))
    headers = {"Authorization": "Bearer " + issued.value, "Idempotency-Key": "synthetic-skill"}
    payload: list[tuple[str, tuple[str | None, bytes | str, str]]] = [
        (
            "metadata",
            (
                None,
                json.dumps({"name": "sample-skill", "description": "Sample purpose"}),
                "application/json",
            ),
        ),
        ("file", ("SKILL.md", b"# Sample\n", "text/markdown")),
        ("file", ("scripts/run.py", b"print('not executed')\n", "text/plain")),
    ]
    base = f"/api/v1/libraries/{library}/skills"
    with TestClient(app) as client:
        assert client.get(base).status_code == 401
        created = client.post(base, headers=headers, files=payload)
        assert created.status_code == 201, created.text
        body = created.json()
        replay = client.post(base, headers=headers, files=payload)
        assert replay.json() == body and replay.headers["Idempotency-Replayed"] == "true"
        skill = base + "/" + body["id"]
        manifest = client.get(created.headers["Location"], headers=headers)
        assert [entry["path"] for entry in manifest.json()["files"]] == [
            "SKILL.md",
            "scripts/run.py",
        ]
        for entry in manifest.json()["files"]:
            response = client.get(entry["href"], headers=headers)
            assert response.status_code == 200 and len(response.content) == entry["bytes"]
        assert (
            client.get(
                skill, headers={**headers, "If-None-Match": created.headers["etag"]}
            ).status_code
            == 304
        )
        unchanged = client.post(
            skill + "/revisions",
            headers={**headers, "Idempotency-Key": "same", "If-Match": body["etag"]},
            files=payload,
        )
        assert unchanged.status_code == 200 and unchanged.json()["changed"] is False
        assert len(client.get(skill + "/revisions", headers=headers).json()["items"]) == 1
        payload[1] = ("file", ("SKILL.md", b"# New version\n", "text/markdown"))
        revised = client.post(
            skill + "/revisions",
            headers={**headers, "Idempotency-Key": "changed", "If-Match": body["etag"]},
            files=payload,
        )
        assert revised.status_code == 200 and revised.json()["revision_number"] == 2
        assert (
            client.post(
                skill + "/revisions",
                headers={**headers, "Idempotency-Key": "stale", "If-Match": body["etag"]},
                files=payload,
            ).status_code
            == 412
        )
        assert (
            client.get(manifest.json()["files"][0]["href"], headers=headers).content
            == b"# Sample\n"
        )
        with immediate_transaction(content_engine) as connection:
            connection.execute(
                delete(CredentialLibraryGrant).where(CredentialLibraryGrant.action == "read")
            )
        assert client.get(skill, headers=headers).status_code == 200
        assert client.get(skill + "/manifest", headers=headers).status_code == 403
        assert (
            client.get(f"/api/v1/libraries/{'f' * 32}/skills", headers=headers).status_code == 403
        )
    assert content_engine.url.database is not None
    with closing(sqlite3.connect(content_engine.url.database)) as connection:
        for response, route in (
            (created, CREATE_SKILL_ROUTE),
            (unchanged, REVISE_SKILL_ROUTE),
            (revised, REVISE_SKILL_ROUTE),
        ):
            validate_skill_replay(
                connection,
                library_id=library,
                caller_id=caller.id,
                method="POST",
                route=route,
                status=response.status_code,
                parsed=response.json(),
                location=response.headers["Location"],
                etag=response.headers["etag"],
                original_request_id=response.headers["X-Request-ID"],
            )
        with pytest.raises(ValueError, match="Changed Skill replay metadata"):
            validate_skill_replay(
                connection,
                library_id=library,
                caller_id=caller.id,
                method="POST",
                route=REVISE_SKILL_ROUTE,
                status=revised.status_code,
                parsed={**revised.json(), "name": "altered-replay-name"},
                location=revised.headers["Location"],
                etag=revised.headers["etag"],
                original_request_id=revised.headers["X-Request-ID"],
            )
        assert connection.execute("SELECT count(*) FROM library_skill_revisions").fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE library_skill_files SET content_bytes=x'00'")


def test_skill_paths_do_not_relax_page_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from patchouli_lib.content.file_manifest import normalize_file_name

    for path in ("../escape", "/absolute", "a\\b", "a//b", "a/./b", "a/../b", "CON/file"):
        with pytest.raises(SkillValidationError):
            build_skill_snapshot((("SKILL.md", b"# A"), (path, b"data")))
    for invalid_files in (
        (("SKILL.md", b"# A"), ("A", b"x"), ("a/b", b"y")),
        (("SKILL.md", b"# A"), ("a/X", b"x"), ("A/x", b"y")),
        (("references/readme.md", b"# A"),),
    ):
        with pytest.raises(SkillValidationError):
            build_skill_snapshot(invalid_files)
    with pytest.raises(ValueError):
        normalize_file_name("scripts/run.py")
    assert (
        build_skill_snapshot((("SKILL.md", b"# A"), ("scripts/run.py", b"x"))).total_size_bytes == 4
    )
    files = {"SKILL.md": b"# A", "scripts/run.py": b"x"}
    snapshot = build_skill_snapshot(files.items())
    import hashlib

    manifest = {
        "name": "sample-skill",
        "library_id": "1" * 32,
        "skill_id": "2" * 32,
        "revision_id": "rev_" + "3" * 32,
        "snapshot_sha256": snapshot.snapshot_sha256.hex(),
        "files": [
            {"path": path, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for path, body in files.items()
        ],
    }
    destination = tmp_path / "skills/sample-skill"
    receipt = tmp_path / "private/receipt.json"
    assert (
        install_skill_package(
            destination,
            manifest,
            files,
            service_origin="https://example.invalid",
            receipt_path=receipt,
        )
        == "installed"
    )
    assert (
        install_skill_package(
            destination,
            manifest,
            files,
            service_origin="https://example.invalid",
            receipt_path=receipt,
        )
        == "unchanged"
    )
    private = destination / "local-config.json"
    private.write_text("synthetic configuration", encoding="utf-8")
    (destination / "SKILL.md").write_bytes(b"# Local changes")
    with pytest.raises(SkillConflictError):
        install_skill_package(
            destination,
            manifest,
            files,
            service_origin="https://example.invalid",
            receipt_path=receipt,
        )
    assert private.read_text(encoding="utf-8") == "synthetic configuration"
    assert (destination / "SKILL.md").read_bytes() == b"# Local changes"
    # Windows directory junctions must fail before any package write or removal.
    with monkeypatch.context() as guard:
        guard.setattr(Path, "is_junction", lambda path: path == destination.parent)
        with pytest.raises(SkillConflictError, match="unsafe"):
            install_skill_package(
                destination,
                manifest,
                files,
                service_origin="https://example.invalid",
                receipt_path=receipt,
            )
