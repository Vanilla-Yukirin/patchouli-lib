from __future__ import annotations

import sys


def entrypoint() -> None:
    """Load the optional SDK without exposing import or environment details."""
    try:
        from patchouli_mcp.server import entrypoint as server_entrypoint
    except ModuleNotFoundError:
        sys.stderr.write("patchouli-mcp: install the patchouli-client[mcp] optional extra\n")
        raise SystemExit(70) from None
    server_entrypoint()
