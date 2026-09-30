"""Disposable, synthetic 5,000-Page search acceptance benchmark.

Never points at an existing database. The temporary SQLite database and its
synthetic content are removed after the engine closes. This is an operator-run
measurement, not part of the ordinary test suite.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from content.helpers import (  # noqa: E402
    insert_page_graph,
    page_graph_values,
    seed_library_structure,
)

from patchouli_lib.api.authentication import AuthenticatedRequestContext  # noqa: E402
from patchouli_lib.auth.repository import AuthRepository  # noqa: E402
from patchouli_lib.auth.schemas import (  # noqa: E402
    AuthenticatedCaller,
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
    credential_metadata,
)
from patchouli_lib.auth.tokens import generate_token  # noqa: E402
from patchouli_lib.database import build_engine, immediate_transaction  # noqa: E402
from patchouli_lib.search.index_v2 import rebuild_search_index  # noqa: E402
from patchouli_lib.search.query_v2 import parse_query_v2_json  # noqa: E402
from patchouli_lib.search.service_v2 import search_pages_v2  # noqa: E402


def _seed(engine: Engine, pages: int, content: bytes) -> AuthenticatedRequestContext:
    library_id, section_id, book_id = seed_library_structure(engine)
    issued = generate_token()
    with immediate_transaction(engine) as connection:
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id="a" * 32,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic benchmark caller",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        credential = auth.add_credential(
            NewCredential(
                id="b" * 32,
                library_id=library_id,
                caller_id=caller.id,
                selector=issued.selector,
                token_version=issued.version,
                verifier=issued.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        auth.add_grant(
            NewSectionGrant(
                library_id=library_id,
                caller_id=caller.id,
                section_id=section_id,
                action=SectionAction.PAGE_READ,
                created_at=1_000_000,
            )
        )
        for index in range(1, pages + 1):
            page, revision, identifier, counter, source = page_graph_values(
                library_id=library_id,
                section_id=section_id,
                book_id=book_id,
                page_byte=index % 256,
                title=f"Synthetic document {index:05d}",
                content_md=content,
            )
            uid = index.to_bytes(16, "big")
            revision_id = f"rev_{index:032x}"
            insert_page_graph(
                connection,
                (
                    page.model_copy(update={"page_uid": uid, "current_revision_id": revision_id}),
                    revision.model_copy(update={"page_uid": uid, "revision_id": revision_id}),
                    identifier.model_copy(update={"page_uid": uid}),
                    counter,
                    source.model_copy(
                        update={
                            "page_uid": uid,
                            "revision_id": revision_id,
                            "source_id": f"{index:032x}",
                        }
                    ),
                ),
            )
    return AuthenticatedRequestContext(
        authenticated=AuthenticatedCaller(
            caller=caller,
            credential=credential_metadata(credential),
        ),
        grants=(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=5_000)
    parser.add_argument("--bytes-per-page", type=int, default=10_240)
    parser.add_argument("--repeats", type=int, default=5)
    arguments = parser.parse_args()
    if not 1 <= arguments.pages <= 10_000 or not 1_024 <= arguments.bytes_per_page <= 65_536:
        parser.error("Synthetic dataset is outside the reviewed benchmark range.")
    if not 1 <= arguments.repeats <= 100:
        parser.error("Invalid repetition count.")
    pattern = "技术报告 synthetic archive\n".encode()
    repetitions, remaining = divmod(arguments.bytes_per_page, len(pattern))
    payload = pattern * repetitions + b"x" * remaining
    with TemporaryDirectory(prefix="patchouli-search-v2-bench-") as directory:
        database = Path(directory) / "synthetic.sqlite"
        os.environ["PATCHOULI_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
        os.environ["PATCHOULI_ENVIRONMENT"] = "test"
        command.upgrade(Config(str(REPOSITORY_ROOT / "alembic.ini")), "head")
        engine = build_engine(os.environ["PATCHOULI_DATABASE_URL"])
        try:
            started = time.perf_counter()
            context = _seed(engine, arguments.pages, payload)
            seeded = time.perf_counter()
            rebuild_search_index(engine)
            rebuilt = time.perf_counter()
            query = parse_query_v2_json('{"keywords":["技术"],"limit":20}'.encode())
            timings = []
            for _ in range(arguments.repeats):
                began = time.perf_counter()
                result = search_pages_v2(engine, context, query, clock=lambda: 3_000_000)
                timings.append((time.perf_counter() - began) * 1_000)
                if len(result.items) != min(20, arguments.pages):
                    raise RuntimeError("Search returned an incomplete Top K.")
            print(f"pages={arguments.pages} bytes_per_page={arguments.bytes_per_page}")
            print(f"seed_seconds={seeded - started:.3f} rebuild_seconds={rebuilt - seeded:.3f}")
            print(f"database_mib={database.stat().st_size / 1_048_576:.1f}")
            print(f"query_ms={','.join(f'{value:.1f}' for value in timings)}")
            print(f"median_ms={statistics.median(timings):.1f} max_ms={max(timings):.1f}")
        finally:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
