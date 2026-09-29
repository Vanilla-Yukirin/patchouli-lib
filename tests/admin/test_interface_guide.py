from __future__ import annotations

import json
import re
from collections.abc import Iterator
from html import escape, unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.api.agent_skill_routes import SkillBundle
from patchouli_lib.api.auth_contracts import CapabilitiesResponse
from patchouli_lib.api.errors import ProblemDetails
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import Caller
from patchouli_lib.config import Settings

_ORIGIN = "https://admin.example.invalid"
_PASSWORD = "synthetic guide password"


@pytest.fixture
def admin_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'guide.db').as_posix()}",
            "admin_password_hash": hash_password(
                _PASSWORD,
                salt_factory=lambda size: b"s" * size,
                iterations=300_000,
            ),
            "admin_session_signing_secret": "s" * 32,
        }
    )
    app = create_app(settings)
    Caller.metadata.create_all(app.state.engine)
    with TestClient(app, base_url=_ORIGIN, follow_redirects=False) as client:
        yield client


def _login(client: TestClient) -> None:
    response = client.post(
        "/admin/login",
        data={"password": _PASSWORD},
        headers={"Origin": _ORIGIN},
    )
    assert response.status_code == 303


def _preview(html: str, identifier: str) -> dict[str, Any]:
    match = re.search(rf'<pre data-preview="{re.escape(identifier)}">(.*?)</pre>', html, re.DOTALL)
    assert match is not None
    value = json.loads(unescape(match.group(1)))
    assert isinstance(value, dict)
    return value


def test_interface_pages_are_session_protected_and_remain_read_only(
    admin_client: TestClient,
) -> None:
    for path in ("/admin/guide", "/admin/agent", "/admin/mcp"):
        unauthenticated = admin_client.get(path)
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == "/admin/login"

    _login(admin_client)
    for path in ("/admin/guide", "/admin/agent", "/admin/mcp"):
        response = admin_client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        assert response.text.count("<form ") == 1  # Existing logout form only.
        assert 'action="/api/' not in response.text
        assert "fetch(" not in response.text
        assert "plb1." not in response.text


def test_api_directory_and_synthetic_examples_match_reviewed_wire_shapes(
    admin_client: TestClient,
) -> None:
    _login(admin_client)
    response = admin_client.get("/admin/guide?lang=zh-CN")
    assert response.headers["content-language"] == "zh-CN"
    assert "合成响应，非实时数据" in response.text
    assert "管理登录 Cookie 不能代替它" in response.text
    assert "需要检索配置" in response.text
    assert "/api/v1/sections/{section_id}/books/{book_id}/pages" in response.text
    assert "搜索：不可用（503）" in response.text

    capabilities = _preview(response.text, "capabilities-preview")
    parsed_capabilities = CapabilitiesResponse.model_validate(capabilities)
    assert parsed_capabilities.api_versions == ("v1",)
    assert parsed_capabilities.features == ("archive", "tags")

    search_error = _preview(response.text, "search-preview")
    parsed_problem = ProblemDetails.model_validate(search_error)
    assert parsed_problem.status == 503
    assert parsed_problem.code == "search_unavailable"


def test_skill_guide_shows_only_packaged_manifest_and_escaped_fixed_files(
    admin_client: TestClient,
) -> None:
    _login(admin_client)
    response = admin_client.get("/admin/agent?file=../private")
    assert response.status_code == 200
    assert "本地打包数据" not in response.text  # Default language is English.
    assert "Packaged data — not a live HTTP response" in response.text
    assert "../private" not in response.text
    assert 'href="/connect"' in response.text

    bundle = SkillBundle()
    manifest = _preview(response.text, "skill-manifest-preview")
    assert manifest == {"version": bundle.version, "files": bundle.file_entries}
    for path, contents in bundle.contents.items():
        assert f"<summary><code>{escape(path)}</code></summary>" in response.text
        assert escape(contents.decode("utf-8")) in response.text


def test_mcp_inventory_and_synthetic_structured_content(
    admin_client: TestClient,
) -> None:
    _login(admin_client)
    response = admin_client.get("/admin/mcp?lang=zh-CN")
    assert "MCP 工具名称" in response.text
    assert "合成响应，非实时数据" in response.text
    for name in (
        "capabilities",
        "whoami",
        "sections_list",
        "books_list",
        "section_search",
        "page_current",
        "page_revision",
        "archive_create",
        "archive_revise",
    ):
        assert f"<code>{name}</code>" in response.text

    success = _preview(response.text, "mcp-success-preview")
    assert success["ok"] is True
    assert set(success) == {"ok", "data", "metadata"}
    assert success["data"]["grants"][0]["actions"] == ["page:read"]
    assert success["metadata"]["etag"] is None

    unavailable = _preview(response.text, "mcp-error-preview")
    assert unavailable == {
        "ok": False,
        "error": {
            "category": "service",
            "code": "search_unavailable",
            "message": "service is temporarily unavailable",
            "request_id": "req_00000000000000000000000000000000",
        },
    }
