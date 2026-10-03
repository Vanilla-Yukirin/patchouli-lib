"""Local maintenance entrypoint for the disposable current-Page search index."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from patchouli_lib.config import Settings
from patchouli_lib.database import build_engine, check_database
from patchouli_lib.search.index_v2 import rebuild_search_index


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patchouli-search-index",
        description="Rebuild search from authoritative Pages and Revisions under a write lock.",
    )
    parser.add_argument("action", choices=("rebuild",))
    arguments = parser.parse_args(argv)
    if arguments.action != "rebuild":
        parser.error("Unknown search-index action.")
    engine = build_engine(Settings().database_url)
    try:
        check_database(engine)
        generation = rebuild_search_index(engine)
    finally:
        engine.dispose()
    print(f"Search index generation {generation} is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
