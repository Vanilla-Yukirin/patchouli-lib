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

from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.file_set_write_routes import create_file_set_write_router
from patchouli_lib.api.library_skill_routes import create_library_skill_router
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.auth.service import CredentialIssuer, utc_microseconds
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewBook, NewLibrary, NewSection
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
    section, book = "4" * 32, "5" * 32
    with immediate_transaction(content_engine) as connection:
        repository = LibraryRepository(connection)
        repository.add_library(
            NewLibrary(id=library, name="Synthetic", created_at=now, updated_at=now)
        )
        repository.add_section(
            NewSection(
                id=section, library_id=library, name="Synthetic", created_at=now, updated_at=now
            )
        )
        repository.add_book(
            NewBook(
                id=book,
                library_id=library,
                section_id=section,
                name="Synthetic",
                created_at=now,
                updated_at=now,
            )
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
    app.include_router(create_file_set_write_router(content_engine))
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

    def package_state() -> dict[str, tuple[tuple[object, ...], ...]]:
        with content_engine.connect() as connection:
            return {
                table: tuple(
                    tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")
                )
                for table in (
                    "library_skills",
                    "library_skill_revisions",
                    "library_skill_files",
                    "auth_audit_events",
                    "idempotency_records",
                )
            }

    with TestClient(app) as client:
        assert client.get(base).status_code == 401
        created = client.post(base, headers=headers, files=payload)
        assert created.status_code == 201, created.text
        body = created.json()
        replay = client.post(base, headers=headers, files=payload)
        assert replay.json() == body and replay.headers["Idempotency-Replayed"] == "true"
        before = package_state()
        reused = client.post(
            base, headers={**headers, "Idempotency-Key": "same-name-create"}, files=payload
        )
        assert reused.status_code == 200 and reused.json() == {**body, "changed": False}
        after = package_state()
        assert {key: value for key, value in after.items() if key != "idempotency_records"} == {
            key: value for key, value in before.items() if key != "idempotency_records"
        }
        assert len(after["idempotency_records"]) == len(before["idempotency_records"]) + 1
        conflicting = [*payload]
        conflicting[1] = ("file", ("SKILL.md", b"# Different content\n", "text/markdown"))
        assert (
            client.post(
                base,
                headers={**headers, "Idempotency-Key": "same-name-conflict"},
                files=conflicting,
            ).status_code
            == 409
        )
        assert package_state() == after
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
        payload[0] = (
            "metadata",
            (
                None,
                json.dumps({"name": "sample-skill-updated", "description": "Sample purpose"}),
                "application/json",
            ),
        )
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
                headers={**headers, "Idempotency-Key": "changed", "If-Match": body["etag"]},
                files=payload,
            ).json()
            == revised.json()
        )
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
        page_payload: list[tuple[str, tuple[str | None, bytes | str, str]]] = [
            (
                "metadata",
                (
                    None,
                    json.dumps(
                        {
                            "title": "Legacy stable title",
                            "source": {"kind": "synthetic"},
                            "primary_file": "notes.md",
                        }
                    ),
                    "application/json",
                ),
            ),
            ("file", ("notes.md", b"# Original title\n", "text/markdown")),
        ]
        first_page = client.post(
            f"/api/v1/libraries/{library}/sections/{section}/books/{book}/pages",
            headers={**headers, "Idempotency-Key": "page-create"},
            files=page_payload,
        )
        assert first_page.status_code == 201, first_page.text
        page_versions = [first_page.json()]
        etag = first_page.headers["ETag"]
        for number, title in ((2, "Second title"), (3, "Third title")):
            page_payload[0] = (
                "metadata",
                (
                    None,
                    json.dumps({"source": {"kind": "synthetic"}, "primary_file": "notes.md"}),
                    "application/json",
                ),
            )
            page_payload[1] = ("file", ("notes.md", f"# {title}\n", "text/markdown"))
            revision = client.post(
                f"/api/v1/libraries/{library}/sections/{section}/pages/"
                f"{page_versions[0]['page_id']}/file-revisions",
                headers={
                    **headers,
                    "Idempotency-Key": f"page-revise-{number}",
                    "If-Match": etag,
                },
                files=page_payload,
            )
            assert revision.status_code == 200, revision.text
            page_versions.append(revision.json())
            etag = revision.headers["ETag"]

        model = AdminReadModel(content_engine)
        activity = model.content_activity_page(actor=(library, caller.id)).items
        assert len(activity) == 5
        assert len(model.recent_content_activity()) == 5
        assert all(item.actor_id == caller.id and item.library_id == library for item in activity)
        skill_activity = [item for item in activity if item.skill_id is not None]
        assert {item.action for item in skill_activity} == {
            "library.skill.create",
            "library.skill.revise",
        }
        assert {
            (item.revision_id, item.revision_number, item.skill_name) for item in skill_activity
        } == {
            (body["revision_id"], 1, "sample-skill"),
            (revised.json()["revision_id"], 2, "sample-skill-updated"),
        }
        assert all(
            item.skill_id == body["id"]
            and item.page_id is None
            and item.page_title is None
            and item.section_id is None
            and item.book_id is None
            for item in skill_activity
        )
        page_activity = [item for item in activity if item.page_id is not None]
        assert {
            (item.revision_id, item.revision_number, item.page_title) for item in page_activity
        } == {
            (version["revision_id"], number, title)
            for version, number, title in zip(
                page_versions,
                (1, 2, 3),
                ("Original title", "Second title", "Third title"),
                strict=True,
            )
        }
        assert all(item.section_id == section and item.book_id == book for item in page_activity)
        with immediate_transaction(content_engine) as connection:
            connection.execute(
                delete(CredentialLibraryGrant).where(CredentialLibraryGrant.action == "read")
            )
        assert client.get(skill, headers=headers).status_code == 200
        assert client.get(skill + "/manifest", headers=headers).status_code == 403
        assert (
            client.get(f"/api/v1/libraries/{'f' * 32}/skills", headers=headers).status_code == 403
        )
        with immediate_transaction(content_engine) as connection:
            connection.execute(
                insert(CredentialLibraryGrant),
                {**identity, "target_library_id": library, "action": "read"},
            )
            connection.execute(
                delete(CredentialLibraryGrant).where(CredentialLibraryGrant.action == "write")
            )
        readonly_manifest = client.get(skill + "/manifest", headers=headers)
        assert readonly_manifest.status_code == 200
        assert readonly_manifest.json()["revision_id"] == revised.json()["revision_id"]
        entry = next(
            item for item in readonly_manifest.json()["files"] if item["path"] == "SKILL.md"
        )
        downloaded = client.get(entry["href"], headers=headers)
        assert downloaded.status_code == 200 and downloaded.content == b"# New version\n"
        before = package_state()
        assert (
            client.post(
                base, headers={**headers, "Idempotency-Key": "readonly-create"}, files=payload
            ).status_code
            == 403
        )
        assert (
            client.post(
                skill + "/revisions",
                headers={
                    **headers,
                    "Idempotency-Key": "readonly-revise",
                    "If-Match": revised.json()["etag"],
                },
                files=payload,
            ).status_code
            == 403
        )
        assert package_state() == before
    assert content_engine.url.database is not None
    with closing(sqlite3.connect(content_engine.url.database)) as raw_connection:
        for response, route in (
            (created, CREATE_SKILL_ROUTE),
            (reused, CREATE_SKILL_ROUTE),
            (unchanged, REVISE_SKILL_ROUTE),
            (revised, REVISE_SKILL_ROUTE),
        ):
            validate_skill_replay(
                raw_connection,
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
                raw_connection,
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
        assert raw_connection.execute(
            "SELECT count(*) FROM library_skill_revisions"
        ).fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError):
            raw_connection.execute("UPDATE library_skill_files SET content_bytes=x'00'")


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
    ancestor_receipt = tmp_path / "uncreated-receipt"
    with pytest.raises(SkillConflictError, match="unsafe"):
        install_skill_package(
            ancestor_receipt / "sample-skill",
            manifest,
            files,
            service_origin="https://example.invalid",
            receipt_path=ancestor_receipt,
        )
    assert not ancestor_receipt.exists()
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

    def update_package(updated: dict[str, bytes], revision: str) -> str:
        package = build_skill_snapshot(updated.items())
        return install_skill_package(
            destination,
            {
                **manifest,
                "revision_id": "rev_" + revision * 32,
                "snapshot_sha256": package.snapshot_sha256.hex(),
                "files": [
                    {"path": path, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                    for path, body in updated.items()
                ],
            },
            updated,
            service_origin="https://example.invalid",
            receipt_path=receipt,
        )

    def local_state() -> tuple[dict[str, bytes | None], bytes]:
        return (
            {
                path.relative_to(destination).as_posix(): None
                if path.is_dir()
                else path.read_bytes()
                for path in destination.rglob("*")
            },
            receipt.read_bytes(),
        )

    (destination / "SKILL.md").write_bytes(files["SKILL.md"])
    nested = {
        "SKILL.md": b"# Nested resources\n",
        "references/a.md": b"first",
        "references/nested/b.md": b"second",
    }
    flat = {"SKILL.md": b"# Flat resource\n", "references": b"flat resource"}
    assert update_package(nested, "4") == "installed"
    assert not (destination / "scripts").exists()
    before = local_state()
    with pytest.raises(SkillConflictError, match="untracked"):
        update_package({**flat, "local-config.json": b"replacement"}, "5")
    assert local_state() == before

    extra = destination / "references/private.json"
    extra.write_bytes(b"synthetic private configuration")
    before = local_state()
    with pytest.raises(SkillConflictError, match="untracked"):
        update_package(flat, "5")
    assert local_state() == before
    extra.unlink()

    extra_alias = destination / "references/alias.md"
    extra_alias.hardlink_to(destination / "references/a.md")
    before = local_state()
    with pytest.raises(SkillConflictError, match="untracked"):
        update_package(flat, "5")
    assert local_state() == before
    extra_alias.unlink()

    extra_directory = destination / "references/private-empty"
    extra_directory.mkdir()
    before = local_state()
    with pytest.raises(SkillConflictError, match="untracked local directory"):
        update_package(flat, "5")
    assert local_state() == before
    with monkeypatch.context() as guard:
        guard.setattr(Path, "is_junction", lambda path: path == extra_directory)
        with pytest.raises(SkillConflictError, match="junction"):
            update_package(flat, "5")
    assert local_state() == before
    extra_directory.rmdir()

    tracked = destination / "references/a.md"
    tracked.write_bytes(b"local edits")
    before = local_state()
    with pytest.raises(SkillConflictError, match="explicit merge"):
        update_package(flat, "5")
    assert local_state() == before
    tracked.write_bytes(nested["references/a.md"])

    assert update_package(flat, "5") == "installed"
    assert (destination / "references").read_bytes() == flat["references"]
    assert update_package(nested, "6") == "installed"
    assert (destination / "references/a.md").read_bytes() == nested["references/a.md"]
    assert (destination / "references/nested/b.md").read_bytes() == nested["references/nested/b.md"]
    renamed = {**nested, "references/A.md": nested["references/a.md"]}
    del renamed["references/a.md"]
    assert update_package(renamed, "7") == "installed"
    assert (destination / "references/A.md").read_bytes() == renamed["references/A.md"]
    assert private.read_text(encoding="utf-8") == "synthetic configuration"
