from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_nginx_example_preserves_admin_security_and_archive_size_boundaries() -> None:
    example = (REPOSITORY_ROOT / "deploy" / "nginx" / "patchouli-admin.conf.example").read_text(
        encoding="utf-8"
    )

    assert "admin.example.invalid" in example
    assert "limit_req_zone $binary_remote_addr" in example
    assert "location = /admin/api/session" in example
    assert "location = /admin/api/setup" in example
    assert example.count("limit_req zone=patchouli_admin_login") == 2
    assert example.count("client_max_body_size 16k") == 4
    assert "location ^~ /admin/api/ {" in example
    assert example.count("client_max_body_size 66m") == 2
    assert "location ^~ /admin/ {\n        client_max_body_size 16k;" in example
    assert "proxy_set_header Host $http_host" in example
    assert "proxy_set_header X-Forwarded-Proto https" in example
    assert "proxy_pass http://patchouli_api" in example
    assert "DEPLOY_" not in example
