"""Cookie-only JSON administration, independent of the legacy HTML adapters."""

from patchouli_lib.admin.json_api.router import create_admin_json_router

__all__ = ["create_admin_json_router"]
