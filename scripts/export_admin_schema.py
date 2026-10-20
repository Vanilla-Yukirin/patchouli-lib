"""Export the isolated browser contract without connecting to a real instance."""

import argparse
import json
from pathlib import Path

from fastapi.openapi.utils import get_openapi
from pydantic import SecretStr

from patchouli_lib import __version__
from patchouli_lib.admin.json_api import create_admin_json_router
from patchouli_lib.config import Settings
from patchouli_lib.database import build_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    # No environment file, deployment database or credential is consulted.
    settings = Settings.model_construct(
        environment="test",
        database_url="sqlite://",
        admin_session_signing_secret=SecretStr("synthetic-schema-export-session-key-0001"),
    )
    engine = build_engine("sqlite://")
    try:
        router = create_admin_json_router(engine, settings)
        schema = get_openapi(
            title="PatchouliLib management", version=__version__, routes=router.routes
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
