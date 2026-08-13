"""Read-only structural and domain validation for experimental backup artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import quote

from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.backup.manifest import SUPPORTED_SCHEMA_REVISION
from patchouli_lib.content.schemas import ArchiveResponseBody
from patchouli_lib.content.service import (
    CREATE_ROUTE_TEMPLATE,
    REVISE_ROUTE_TEMPLATE,
    page_current_etag,
)
from patchouli_lib.identifiers import (
    canonical_utc_wire,
    page_id_registry_digest,
    page_id_timestamp_prefix,
    validate_page_id,
)

# Hashes cover every non-internal SQLite schema object at the accepted migration
# head. SQLite-managed objects whose names start with ``sqlite_`` are the only
# exemption. An Alembic row or object name alone is therefore insufficient.
_EXPECTED_SQL_HASHES: Final = {
    ("index", "ix_auth_audit_events_library_request_id"): (
        "4d13d9f0a447ee626d5796d1834202e6a65130e065dc3d9256e553dff4fe017a"
    ),
    ("index", "uq_page_identifier_registry_library_page_canonical"): (
        "263efd59e682f1a803fc4ec9db8e8897f1e42ab40138ae49a3bb046e754b0084"
    ),
    ("table", "alembic_version"): (
        "1e1ffb41bfee027fe6614ee3d494792f82bdc3e970e61cd844a37325751d9551"
    ),
    ("table", "auth_audit_events"): (
        "6f07b429c88bee095d67ea8949988064036096391732d6d03840a1460a868544"
    ),
    ("table", "auth_callers"): ("40897a2343fb473a9b0e34ae4727eb1d8f6cb8acbef1242832ba507329e5c759"),
    ("table", "auth_credentials"): (
        "7522b491a643eb1cb64b2782f7c55d23ec2037297535f64655425ceede0b0909"
    ),
    ("table", "auth_section_grants"): (
        "462c147084ce1964ca7494a71f88594dc951b067aaa9dbf94734d7d3f1f86479"
    ),
    ("table", "books"): ("0812139b964347ba630860242b7710903c9dcbf3f6c11d6933812d8c4ee5b702"),
    ("table", "idempotency_records"): (
        "a2600611d89cc74f5f4f05c9191cff1484b273203ae3ac901bec46ce368e3ebd"
    ),
    ("table", "libraries"): ("6bb11b3b4688c9845586c8ecd05981e36f2eef7ed0a5fd80376990106b2ef02f"),
    ("table", "operator_bootstrap_markers"): (
        "9e4ac714a64c125775e179eb4a6f52a0a40644a5f1f19751a98b50e67df20a5a"
    ),
    ("table", "page_id_collision_counters"): (
        "14b2fa31f0ae6d6c5242bfbee3e1f62d04d08bbdfe46c6efa310cbca918ec05d"
    ),
    ("table", "page_identifier_registry"): (
        "f00f5eda1f98d7ca627a823a8981833db71795aebef72dd169c3117080e189ef"
    ),
    ("table", "page_revision_append_guards"): (
        "f034fdb20287ef3c548294a874a22fa029da3609348dd7f9191fa382388207e6"
    ),
    ("table", "page_sources"): ("e2736429307cd28768483fb718826e686fff197abd4abc1b23aaf6f793f9e334"),
    ("table", "pages"): ("6ac4615d1122d1f5b5e7976cf865ed574086db4a0390d27643dfaaa80429d92f"),
    ("table", "revisions"): ("fc3bd5d3f0ff42d07a92ceb26477a48ce2991a7df8abedb3e821f9e6dcf529ef"),
    ("table", "schema_metadata"): (
        "257911ed87982371b26ee44aecd9e8ee23741d25d6ab87fb1fa9b28c8e3c5b32"
    ),
    ("table", "sections"): ("0da1d13398d80ef5f57d238a462a498e81e647c575f2b4793d0e15599bed211c"),
    ("trigger", "trg_idempotency_records_immutable_update"): (
        "8ec45701f9b821e9d3336181cd59390ae8dca60352ead3d01c698a04cbe08967"
    ),
    ("trigger", "trg_idempotency_records_no_delete"): (
        "8f6803c4ac14c72813cd9c9623f3249f0042244797d899de9260349472ffa2f3"
    ),
    ("trigger", "trg_page_id_collision_counters_monotonic"): (
        "c750235b44dce0abde298ab7394d14c0e3de76ffcdb9774f836ef3e3bbc4893e"
    ),
    ("trigger", "trg_page_id_collision_counters_no_delete"): (
        "98377b594870730896d51b061a54cbe4259485ab568af86dc6418104d10541a2"
    ),
    ("trigger", "trg_page_identifier_registry_kind_on_insert"): (
        "2caddcb76a603514306b709926627cbaf38259b361ebcb711abae0fbac9f18d0"
    ),
    ("trigger", "trg_page_identifier_registry_no_delete"): (
        "d9f3e08038dbbaa0648e12df9431d45615743568d083db379f446df4620ae830"
    ),
    ("trigger", "trg_page_identifier_registry_stable"): (
        "4984e9f58cad2c80341fe07830a157b5aa16a7d9dbbff6cede168f4469f4a6ab"
    ),
    ("trigger", "trg_page_revision_append_guards_no_update"): (
        "c8dce6ff0ec7b92364a3c991aa5f5f447e8bbbaccd13103f5aa0f647eb6366cb"
    ),
    ("trigger", "trg_page_revision_append_guards_safe_delete"): (
        "f1881e422dbbb01e335b83ecf6414d56febf2fba8bd73ad5236d861d1175d6fa"
    ),
    ("trigger", "trg_pages_canonical_identifier_on_insert"): (
        "08057a2c681f9e9b3b94baf464dd81543a4fe063f89c92002365427f19aaafce"
    ),
    ("trigger", "trg_pages_clear_append_guard"): (
        "f24cfec41cbafe58644394438e85b93cd324c1b837ee7a067d17e3f7589fcc87"
    ),
    ("trigger", "trg_pages_current_revision_advance"): (
        "9753a7e581beffe804ef5a41ec4120866780e1ed3e55658a7c0074beec41c03e"
    ),
    ("trigger", "trg_pages_initial_revision_number"): (
        "bc7fe0357fee6ac8d3cdd751d9efb565948f235a277cc893f8cdf3c962fb3d25"
    ),
    ("trigger", "trg_pages_stable_identity"): (
        "7ae19e042a77bd988a7362ea88ef69e67ed2cdad60cf03b808f3ad175525087c"
    ),
    ("trigger", "trg_revisions_create_append_guard"): (
        "54b3bb78b67aaab9acefb545d8b6ce2e28238f83fe5de8612823d9b35520f30c"
    ),
    ("trigger", "trg_revisions_immutable_delete"): (
        "6278a2927aecfd931c141ee87e0a584bb6eba4b5be6c07fc403f7ddc7f7abd92"
    ),
    ("trigger", "trg_revisions_immutable_update"): (
        "b02bd56b1df874099c18d7f7f68b4a986e7ffbf7f9ced85a885418689a58b25f"
    ),
    ("trigger", "trg_revisions_sequential_insert"): (
        "bfb59191beb9acd52e5fedd6c834cd7bb51fbd49c742048c51404359730e4a1e"
    ),
}


@dataclass(frozen=True, slots=True)
class DatabaseValidationReport:
    """Non-sensitive facts proven for a self-contained database artifact."""

    schema_revision: str
    sqlite_version: str
    artifact_journal_mode: str


def _read_only_uri(path: Path) -> str:
    encoded = quote(path.as_posix(), safe="/:")
    return f"file:{encoded}?mode=ro&immutable=1"


def _one_integer(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    if row is None or type(row[0]) is not int:
        raise BackupDatabaseError
    return row[0]


def _canonical_schema_sql(object_type: str, sql: str) -> str:
    normalized = " ".join(sql.split())
    if object_type != "table":
        return normalized
    opening = normalized.find("(")
    if opening < 0 or not normalized.endswith(")"):
        raise BackupDatabaseError
    prefix = normalized[:opening].rstrip()
    body = normalized[opening + 1 : -1]
    clauses: list[str] = []
    start = 0
    depth = 0
    quote_character: str | None = None
    index = 0
    while index < len(body):
        character = body[index]
        if quote_character is not None:
            if character == quote_character:
                if index + 1 < len(body) and body[index + 1] == quote_character:
                    index += 1
                else:
                    quote_character = None
        elif character in {"'", '"'}:
            quote_character = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise BackupDatabaseError
        elif character == "," and depth == 0:
            clauses.append(body[start:index].strip())
            start = index + 1
        index += 1
    if quote_character is not None or depth != 0:
        raise BackupDatabaseError
    clauses.append(body[start:].strip())
    if not clauses or any(not clause for clause in clauses):
        raise BackupDatabaseError
    columns = [clause for clause in clauses if not clause.startswith("CONSTRAINT ")]
    constraints = sorted(clause for clause in clauses if clause.startswith("CONSTRAINT "))
    if len(columns) + len(constraints) != len(clauses):
        raise BackupDatabaseError
    return f"{prefix} ({', '.join((*columns, *constraints))})"


def _require_schema(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"
    )
    observed: dict[tuple[str, str], str] = {}
    for object_type, name, sql in rows:
        if not all(isinstance(value, str) for value in (object_type, name, sql)):
            raise BackupDatabaseError
        normalized = _canonical_schema_sql(object_type, sql)
        observed[(object_type, name)] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if observed != _EXPECTED_SQL_HASHES:
        raise BackupDatabaseError

    revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if revisions != [(SUPPORTED_SCHEMA_REVISION,)]:
        raise BackupDatabaseError
    return SUPPORTED_SCHEMA_REVISION


def _require_sqlite_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise BackupDatabaseError
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupDatabaseError


def _require_page_graph(connection: sqlite3.Connection) -> None:
    if _one_integer(connection, "SELECT count(*) FROM page_revision_append_guards") != 0:
        raise BackupDatabaseError

    invalid_current = _one_integer(
        connection,
        "SELECT count(*) FROM pages AS p WHERE NOT EXISTS ("
        "SELECT 1 FROM revisions AS current "
        "WHERE current.library_id = p.library_id AND current.page_uid = p.page_uid "
        "AND current.revision_id = p.current_revision_id "
        "AND current.revision_number = p.current_revision_number) "
        "OR (SELECT min(r.revision_number) FROM revisions AS r "
        "WHERE r.library_id = p.library_id AND r.page_uid = p.page_uid) != 1 "
        "OR (SELECT max(r.revision_number) FROM revisions AS r "
        "WHERE r.library_id = p.library_id AND r.page_uid = p.page_uid) "
        "!= p.current_revision_number "
        "OR (SELECT count(*) FROM revisions AS r "
        "WHERE r.library_id = p.library_id AND r.page_uid = p.page_uid) "
        "!= p.current_revision_number",
    )
    if invalid_current:
        raise BackupDatabaseError

    for content, size, digest in connection.execute(
        "SELECT content_md, content_size_bytes, content_sha256 FROM revisions"
    ):
        if type(content) is not bytes or type(size) is not int or type(digest) is not bytes:
            raise BackupDatabaseError
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise BackupDatabaseError from None
        if b"\x00" in content or len(content) != size or hashlib.sha256(content).digest() != digest:
            raise BackupDatabaseError

    invalid_sources = _one_integer(
        connection,
        "SELECT count(*) FROM page_sources AS s WHERE NOT EXISTS ("
        "SELECT 1 FROM revisions AS r WHERE r.library_id = s.library_id "
        "AND r.page_uid = s.page_uid AND r.revision_id = s.revision_id "
        "AND r.revision_number = s.revision_number)",
    )
    if invalid_sources:
        raise BackupDatabaseError
    missing_sources = _one_integer(
        connection,
        "SELECT count(*) FROM revisions AS r WHERE NOT EXISTS ("
        "SELECT 1 FROM page_sources AS s WHERE s.library_id = r.library_id "
        "AND s.page_uid = r.page_uid AND s.revision_id = r.revision_id "
        "AND s.revision_number = r.revision_number)",
    )
    if missing_sources:
        raise BackupDatabaseError

    identifiers_by_page: dict[tuple[str, bytes], list[tuple[str, str]]] = {}
    for library_id, digest, text, kind, page_uid in connection.execute(
        "SELECT library_id, identifier_digest, identifier_text, identifier_kind, page_uid "
        "FROM page_identifier_registry"
    ):
        if not isinstance(library_id, str) or type(page_uid) is not bytes:
            raise BackupDatabaseError
        if type(digest) is not bytes or not isinstance(text, str) or not isinstance(kind, str):
            raise BackupDatabaseError
        try:
            validate_page_id(text)
            expected_digest = page_id_registry_digest(text)
        except ValueError:
            raise BackupDatabaseError from None
        if digest != expected_digest:
            raise BackupDatabaseError
        identifiers_by_page.setdefault((library_id, page_uid), []).append((text, kind))

    page_counter_keys: set[tuple[str, str, int, str]] = set()
    for (
        library_id,
        page_uid,
        page_id,
        scheme,
        timestamp,
        slug,
        ordinal,
        occurred_at,
    ) in connection.execute(
        "SELECT library_id, page_uid, page_id, id_scheme, id_timestamp_micros, "
        "base_slug, collision_ordinal, occurred_at FROM pages"
    ):
        if (
            not isinstance(library_id, str)
            or type(page_uid) is not bytes
            or not isinstance(page_id, str)
            or not isinstance(scheme, str)
            or type(timestamp) is not int
            or not isinstance(slug, str)
            or type(ordinal) is not int
            or type(occurred_at) is not int
        ):
            raise BackupDatabaseError
        suffix = "" if ordinal == 1 else f"-{ordinal}"
        try:
            expected_page_id = f"{page_id_timestamp_prefix(timestamp)}-{slug}{suffix}"
            validate_page_id(expected_page_id)
        except ValueError:
            raise BackupDatabaseError from None
        if (
            scheme != "page-v1"
            or page_id != expected_page_id
            or timestamp != (occurred_at // 1000) * 1000
        ):
            raise BackupDatabaseError
        identifiers = identifiers_by_page.get((library_id, page_uid), [])
        if identifiers.count((page_id, "canonical")) != 1:
            raise BackupDatabaseError
        page_counter_keys.add((library_id, scheme, timestamp, slug))

    observed_counter_keys: set[tuple[str, str, int, str]] = set()
    for library_id, scheme, timestamp, slug, next_ordinal in connection.execute(
        "SELECT library_id, id_scheme, id_timestamp_micros, base_slug, next_ordinal "
        "FROM page_id_collision_counters"
    ):
        if (
            not isinstance(library_id, str)
            or not isinstance(scheme, str)
            or type(timestamp) is not int
            or not isinstance(slug, str)
            or type(next_ordinal) is not int
        ):
            raise BackupDatabaseError
        key = (library_id, scheme, timestamp, slug)
        observed_counter_keys.add(key)
        row = connection.execute(
            "SELECT max(collision_ordinal) FROM pages WHERE library_id = ? "
            "AND id_scheme = ? AND id_timestamp_micros = ? AND base_slug = ?",
            key,
        ).fetchone()
        if row is None or type(row[0]) is not int or next_ordinal <= row[0]:
            raise BackupDatabaseError
    if page_counter_keys != observed_counter_keys:
        raise BackupDatabaseError


def _require_auth_graph(connection: sqlite3.Connection) -> None:
    invalid_bootstrap = _one_integer(
        connection,
        "SELECT count(*) FROM operator_bootstrap_markers AS m "
        "LEFT JOIN auth_callers AS c ON c.id = m.operator_caller_id "
        "AND c.library_id = m.library_id "
        "LEFT JOIN auth_credentials AS k ON k.id = m.initial_credential_id "
        "AND k.caller_id = m.operator_caller_id AND k.library_id = m.library_id "
        "WHERE c.id IS NULL OR c.kind != 'operator' OR k.id IS NULL",
    )
    if invalid_bootstrap:
        raise BackupDatabaseError

    invalid_grants = _one_integer(
        connection,
        "SELECT count(*) FROM auth_section_grants AS g "
        "JOIN auth_callers AS c ON c.id = g.caller_id AND c.library_id = g.library_id "
        "WHERE c.kind != 'agent'",
    )
    if invalid_grants:
        raise BackupDatabaseError

    rotations: dict[str, tuple[str, str, str | None, int | None, int | None, int]] = {}
    for (
        identifier,
        library_id,
        caller_id,
        target,
        rotated_at,
        revoked_at,
        created_at,
    ) in connection.execute(
        "SELECT id, library_id, caller_id, rotated_to_credential_id, rotated_at, "
        "revoked_at, created_at FROM auth_credentials"
    ):
        if not all(isinstance(value, str) for value in (identifier, library_id, caller_id)):
            raise BackupDatabaseError
        if target is not None and not isinstance(target, str):
            raise BackupDatabaseError
        if rotated_at is not None and type(rotated_at) is not int:
            raise BackupDatabaseError
        if revoked_at is not None and type(revoked_at) is not int:
            raise BackupDatabaseError
        if type(created_at) is not int:
            raise BackupDatabaseError
        rotations[identifier] = (
            library_id,
            caller_id,
            target,
            rotated_at,
            revoked_at,
            created_at,
        )

    targets: set[str] = set()
    for identifier, (
        library_id,
        caller_id,
        target,
        rotated_at,
        revoked_at,
        _created_at,
    ) in rotations.items():
        if target is None:
            continue
        if target in targets or rotated_at is None or revoked_at != rotated_at:
            raise BackupDatabaseError
        targets.add(target)
        target_row = rotations.get(target)
        if target_row is None or target_row[0:2] != (library_id, caller_id):
            raise BackupDatabaseError
        if target_row[5] != rotated_at:
            raise BackupDatabaseError
        seen = {identifier}
        cursor: str | None = target
        while cursor is not None:
            if cursor in seen:
                raise BackupDatabaseError
            seen.add(cursor)
            next_row = rotations.get(cursor)
            if next_row is None:
                raise BackupDatabaseError
            cursor = next_row[2]


def _require_idempotency_graph(connection: sqlite3.Connection) -> None:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    for (
        library_id,
        method,
        route,
        status,
        media_type,
        body_bytes,
        location,
        etag,
    ) in connection.execute(
        "SELECT library_id, method, route_template, response_status, "
        "response_media_type, response_body, response_location, response_etag "
        "FROM idempotency_records"
    ):
        if (
            not isinstance(library_id, str)
            or method != "POST"
            or not isinstance(route, str)
            or status != 201
            or media_type != "application/json"
            or type(body_bytes) is not bytes
            or (location is not None and not isinstance(location, str))
            or not isinstance(etag, str)
        ):
            raise BackupDatabaseError
        try:
            decoded = body_bytes.decode("utf-8", errors="strict")
            parsed = json.loads(
                decoded,
                object_pairs_hook=reject_duplicate_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(parsed, dict):
                raise ValueError
            body = ArchiveResponseBody.model_validate(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise BackupDatabaseError from None

        row = connection.execute(
            "SELECT p.page_uid, p.book_id, p.title, p.page_type, p.occurred_at, "
            "r.created_at, r.content_md, r.content_sha256 FROM pages AS p "
            "JOIN revisions AS r ON r.library_id = p.library_id AND r.page_uid = p.page_uid "
            "AND r.revision_id = ? AND r.revision_number = ? "
            "WHERE p.library_id = ? AND p.page_id = ? AND p.section_id = ?",
            (
                body.revision.revision_id,
                body.revision.revision_number,
                library_id,
                body.page.page_id,
                body.page.section_id,
            ),
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not bytes
            or not isinstance(row[1], str)
            or not isinstance(row[2], str)
            or not isinstance(row[3], str)
            or type(row[4]) is not int
            or type(row[5]) is not int
            or type(row[6]) is not bytes
            or type(row[7]) is not bytes
        ):
            raise BackupDatabaseError
        if (
            body.page.book_id != row[1]
            or body.page.title != row[2]
            or body.page.type != row[3]
            or body.page.occurred_at != canonical_utc_wire(row[4])
            or body.revision.created_at != canonical_utc_wire(row[5])
        ):
            raise BackupDatabaseError
        try:
            stored_content = row[6].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise BackupDatabaseError from None
        if stored_content != body.revision.content or row[7].hex() != body.revision.content_sha256:
            raise BackupDatabaseError
        if (
            page_current_etag(row[0], body.revision.revision_id, body.revision.revision_number)
            != etag
        ):
            raise BackupDatabaseError
        page_location = f"/api/v1/sections/{body.page.section_id}/pages/{body.page.page_id}"
        revision_location = f"{page_location}/revisions/{body.revision.revision_number}"
        if body.citation.href != revision_location:
            raise BackupDatabaseError
        expected_location = page_location if route == CREATE_ROUTE_TEMPLATE else revision_location
        if route not in {CREATE_ROUTE_TEMPLATE, REVISE_ROUTE_TEMPLATE}:
            raise BackupDatabaseError
        if location != expected_location:
            raise BackupDatabaseError


def _validate_connection(connection: sqlite3.Connection) -> DatabaseValidationReport:
    connection.execute("PRAGMA query_only = ON")
    _require_sqlite_integrity(connection)
    schema_revision = _require_schema(connection)
    sqlite_version_row = connection.execute("SELECT sqlite_version()").fetchone()
    journal_row = connection.execute("PRAGMA journal_mode").fetchone()
    if (
        sqlite_version_row is None
        or not isinstance(sqlite_version_row[0], str)
        or journal_row is None
        or not isinstance(journal_row[0], str)
    ):
        raise BackupDatabaseError
    _require_page_graph(connection)
    _require_auth_graph(connection)
    _require_idempotency_graph(connection)
    return DatabaseValidationReport(
        schema_revision=schema_revision,
        sqlite_version=sqlite_version_row[0],
        artifact_journal_mode=journal_row[0].lower(),
    )


def validate_database(path: Path) -> DatabaseValidationReport:
    """Validate one closed, self-contained SQLite file without modifying it."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise BackupDatabaseError
    try:
        if path.is_symlink() or not path.is_file():
            raise BackupDatabaseError
        with closing(sqlite3.connect(_read_only_uri(path), uri=True, timeout=5.0)) as connection:
            report = _validate_connection(connection)
    except BackupDatabaseError:
        raise
    except (OSError, RecursionError, sqlite3.Error, ValueError):
        raise BackupDatabaseError from None
    if report.artifact_journal_mode != "delete":
        raise BackupDatabaseError
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{path}{suffix}").exists():
            raise BackupDatabaseError
    return report


__all__ = ["DatabaseValidationReport", "validate_database"]
