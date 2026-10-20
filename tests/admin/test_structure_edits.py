"""Metadata CAS and audit rollback remain covered after replacing HTML forms."""

from typing import Any

import pytest
from helpers import AdminWeb, create_structure
from helpers import admin_web as admin_web
from sqlalchemy import select

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.auth.models import MasterAuditEvent


def test_hierarchy_metadata_cas_noop_and_redacted_audit(admin_web: AdminWeb) -> None:
    web = admin_web
    library, section, book = create_structure(web)
    for kind, item_id, field in (
        ("library", library, "description"),
        ("section", section, "description"),
        ("book", book, "summary"),
    ):
        path = f"/admin/api/{kind[:-1] + 'ies' if kind == 'library' else kind + 's'}/{item_id}"
        before = web.client.get(path).json()[kind]
        form = {
            "name": before["name"],
            field: before[field],
            "expected_updated_at": before["updated_at"],
        }
        noop = web.client.put(path, json=form, headers=web.headers)
        assert noop.status_code == 200
        name = f"Changed {kind} <synthetic>"
        value = "𠮷" * 4000
        changed = web.client.put(
            path,
            json={**form, "name": name, field: value, "readme": "# Instructions\n"},
            headers=web.headers,
        )
        assert changed.status_code == 200, changed.text
        after = web.client.get(path).json()[kind]
        assert after["id"] == item_id and after["name"] == name and after[field] == value
        assert after["readme"] == "# Instructions\n" and after["updated_at"] > before["updated_at"]
        assert web.client.put(path, json=form, headers=web.headers).status_code == 409
        with web.engine.connect() as connection:
            events = (
                connection.execute(
                    select(MasterAuditEvent.__table__).where(
                        MasterAuditEvent.action == f"{kind}.update"
                    )
                )
                .mappings()
                .all()
            )
        assert len(events) == 1
        assert events[0]["target_id"] == item_id and events[0]["target_type"] == kind
        assert events[0]["identity_id"] == web.session.identity_id
        assert name not in repr(events) and value not in repr(events)


def test_structure_edit_audit_failure_rolls_back(
    admin_web: AdminWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = admin_web
    library, _, _ = create_structure(web)
    path = f"/admin/api/libraries/{library}"
    before = web.client.get(path).json()["library"]

    def reject(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic private audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject)
    response = web.client.put(
        path,
        json={
            "name": "Rollback",
            "description": "Changed",
            "expected_updated_at": before["updated_at"],
        },
        headers=web.headers,
    )
    assert response.status_code == 500 and "synthetic private audit failure" not in response.text
    assert web.client.get(path).json()["library"] == before
