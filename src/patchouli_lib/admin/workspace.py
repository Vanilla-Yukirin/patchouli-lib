"""Same-origin hosting of separately built administration assets, without templates."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse, Response


def web_root() -> Path:
    installed = Path(__file__).resolve().parents[1] / "web_resources"
    if (installed / "index.html").is_file():
        return installed
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def create_workspace_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/admin/assets/{resource_path:path}")
    def asset(resource_path: str) -> FileResponse:
        root = web_root().resolve()
        target = (root / resource_path).resolve()
        if (
            not target.is_relative_to(root)
            or target == root / "index.html"
            or not target.is_file()
            or target.suffix not in {".js", ".css", ".woff2", ".svg", ".png", ".ico"}
        ):
            raise HTTPException(404)
        return FileResponse(
            target,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    @router.get("/admin")
    @router.get("/admin/{client_path:path}")
    def entry(request: Request, client_path: str = "") -> Response:
        # Unknown API/asset requests must never turn into a successful HTML page.
        if client_path == "api" or client_path.startswith(("api/", "assets/")):
            raise HTTPException(404)
        filename = web_root() / "index.html"
        if not filename.is_file():
            raise HTTPException(
                503, "Build the administration frontend before starting this service."
            )
        nonce = secrets.token_urlsafe(24)
        content = filename.read_bytes().replace(b"__CSP_NONCE__", nonce.encode("ascii"))
        return Response(
            content,
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "X-Frame-Options": "DENY",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; connect-src 'self'; "
                    f"style-src 'self' 'nonce-{nonce}'; img-src 'self'; font-src 'self'; "
                    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
                    "object-src 'none'"
                ),
            },
        )

    return router
