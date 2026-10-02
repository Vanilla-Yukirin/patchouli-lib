"""Transaction-consistent current-Page search reader for the v1 HTTP endpoint.

Cursor support is opt-in until HTTP and browser adapters pass a shared codec.
Scores and query literals are never exposed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast

from sqlalchemy import Connection, Engine

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.api.authentication import AuthenticatedRequestContext
from patchouli_lib.auth.library_policy import LibraryAction
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, SectionAction
from patchouli_lib.auth.service import AuthenticationError, utc_microseconds
from patchouli_lib.retrieval.cursor import CursorBinding, CursorCodec, InvalidCursorError
from patchouli_lib.search.index_v2 import (
    INDEX_VERSION,
    SearchIndexUnavailableError,
    require_ready_index,
)
from patchouli_lib.search.literal_v2 import (
    candidate_match_expression,
    encoded_library_token,
    normalize_keywords,
)
from patchouli_lib.search.query_v2 import InvalidSearchQueryV2, SearchQueryV2

_FIELD_WEIGHT = {"title": 100, "file_name": 30, "file_text": 10}
_MAX_MATCH_SOURCES = 8
_SORT_VERSION = "fixed-field-v1"
_SNIPPET_CODEPOINTS = 240
_SNIPPET_LEADING_CODEPOINTS = 80


class SearchScopeError(RuntimeError):
    """An explicit Library or Tag identity is outside the visible scope."""

    def __init__(self) -> None:
        super().__init__("Search scope is not available.")


@dataclass(frozen=True, slots=True)
class SearchMatchSourceV2:
    kind: str
    file_name: str | None


@dataclass(frozen=True, slots=True)
class SearchSnippetV2:
    """Plain, bounded text from one authorized current UTF-8 file projection."""

    file_name: str
    text: str
    matched: bool


@dataclass(frozen=True, slots=True)
class SearchPageV2:
    library_id: str
    section_id: str
    book_id: str
    page_id: str
    revision_id: str
    revision_number: int
    title: str
    occurred_at: int
    match_sources: tuple[SearchMatchSourceV2, ...]
    snippet: SearchSnippetV2 | None = None


@dataclass(frozen=True, slots=True)
class SearchResultV2:
    items: tuple[SearchPageV2, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class _Scope:
    library_id: str
    legacy_sections: bool


@dataclass(frozen=True, slots=True)
class _ResolvedScope:
    scopes: tuple[_Scope, ...]
    caller_id: str
    principal_identity: bytes
    policy_version: int
    grants_identity: bytes


def _digest_json(domain: bytes, value: object) -> bytes:
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(domain + payload).digest()


def _current_scope(
    connection: Connection,
    context: AuthenticatedRequestContext,
    query: SearchQueryV2,
    now: int,
) -> _ResolvedScope:
    repository = AuthRepository(connection)
    supplied = context.authenticated
    identity = supplied.caller
    if identity.kind is not CallerKind.AGENT:
        raise AuthenticationError
    caller = repository.get_caller(identity.library_id, identity.id)
    credential = repository.get_credential(identity.library_id, identity.id, supplied.credential.id)
    if (
        caller is None
        or caller.kind is not CallerKind.AGENT
        or caller.disabled_at is not None
        or credential is None
        or credential.revoked_at is not None
        or credential.rotated_at is not None
        or credential.token_version != supplied.credential.token_version
        or now < credential.created_at
        or now >= credential.expires_at
    ):
        raise AuthenticationError

    if repository.has_library_grant_policy(identity.library_id, identity.id, credential.id):
        library_grants = repository.list_credential_library_grants(
            home_library_id=identity.library_id,
            caller_id=identity.id,
            credential_id=credential.id,
        )
        readable = {
            grant.library_id: _Scope(grant.library_id, False)
            for grant in library_grants
            if LibraryAction.READ in grant.actions and repository.library_exists(grant.library_id)
        }
        grants_identity = _digest_json(
            b"patchouli-search-grants-v1\0",
            [
                (grant.library_id, sorted(action.value for action in grant.actions))
                for grant in library_grants
            ],
        )
    else:
        section_grants = repository.list_grants(identity.library_id, identity.id)
        has_read = any(grant.action is SectionAction.PAGE_READ for grant in section_grants)
        readable = {identity.library_id: _Scope(identity.library_id, True)} if has_read else {}
        grants_identity = _digest_json(
            b"patchouli-search-grants-v1\0",
            [(grant.section_id, grant.action.value, grant.created_at) for grant in section_grants],
        )

    return _ResolvedScope(
        scopes=_select_scopes(connection, readable, query),
        caller_id=identity.id,
        principal_identity=_digest_json(
            b"patchouli-search-principal-v1\0",
            (identity.library_id, identity.id, credential.id, credential.token_version),
        ),
        policy_version=caller.policy_version,
        grants_identity=grants_identity,
    )


def _master_scope(
    connection: Connection,
    identity_id: str,
    session_generation: int,
    query: SearchQueryV2,
) -> _ResolvedScope:
    if not MasterTokenRepository(connection).is_session_generation_current(
        identity_id, session_generation
    ):
        raise AuthenticationError
    readable = {
        str(row[0]): _Scope(str(row[0]), False)
        for row in connection.exec_driver_sql("SELECT id FROM libraries ORDER BY id")
    }
    return _ResolvedScope(
        scopes=_select_scopes(connection, readable, query),
        caller_id="",
        principal_identity=_digest_json(
            b"patchouli-search-master-v1\0", (identity_id, session_generation)
        ),
        policy_version=session_generation,
        grants_identity=_digest_json(
            b"patchouli-search-master-scope-v1\0", tuple(sorted(readable))
        ),
    )


def _select_scopes(
    connection: Connection,
    readable: dict[str, _Scope],
    query: SearchQueryV2,
) -> tuple[_Scope, ...]:
    if query.libraries is not None:
        if any(library_id not in readable for library_id in query.libraries):
            raise SearchScopeError
        selected = tuple(readable[library_id] for library_id in query.libraries)
    else:
        selected = tuple(readable[library_id] for library_id in sorted(readable))

    selected_ids = {scope.library_id for scope in selected}
    for identity_tag in query.tags_any:
        if identity_tag.library_id not in selected_ids:
            raise SearchScopeError
        exists = connection.exec_driver_sql(
            "SELECT 1 FROM tags WHERE library_id = ? AND id = ?",
            (identity_tag.library_id, identity_tag.tag_id),
        ).first()
        if exists is None:
            raise SearchScopeError
    return selected


def _section_predicate(scope: _Scope, alias: str) -> str:
    if not scope.legacy_sections:
        return "1 = 1"
    return (
        "EXISTS (SELECT 1 FROM auth_section_grants AS sg "
        f"WHERE sg.library_id = {alias}.library_id AND sg.section_id = {alias}.section_id "
        "AND sg.caller_id = ? AND sg.action = 'page:read')"
    )


def _check_scope_integrity(
    connection: Connection, generation: int, scope: _Scope, caller_id: str
) -> None:
    # A bypassed writer may leave the old state in a formerly readable Section.
    # Check both current authority and previous projection, including deletes.
    current_section = _section_predicate(scope, "p")
    indexed_section = _section_predicate(scope, "s")
    dirty = connection.exec_driver_sql(
        "SELECT 1 FROM search_dirty_pages AS x "
        "LEFT JOIN pages AS p ON p.library_id = x.library_id AND p.page_uid = x.page_uid "
        "LEFT JOIN search_page_state AS s ON s.generation = ? "
        "AND s.library_id = x.library_id AND s.page_uid = x.page_uid "
        "WHERE x.library_id = ? AND (" + current_section + " OR " + indexed_section + ") LIMIT 1",
        (generation, scope.library_id, *((caller_id, caller_id) if scope.legacy_sections else ())),
    ).first()
    if dirty is not None:
        raise SearchIndexUnavailableError("Search index has dirty Pages in the selected scope.")

    # Ready metadata alone cannot prove completeness after corruption or an
    # uncoordinated write. This intentionally scans the selected live scope.
    section = _section_predicate(scope, "p")
    missing = connection.exec_driver_sql(
        "SELECT 1 FROM pages AS p "
        "LEFT JOIN search_page_state AS s ON s.generation = ? "
        "AND s.library_id = p.library_id AND s.page_uid = p.page_uid "
        "WHERE p.library_id = ? AND p.deleted_at IS NULL AND "
        + section
        + " AND (s.page_uid IS NULL OR s.section_id != p.section_id "
        "OR s.book_id != p.book_id OR s.page_id != p.page_id "
        "OR s.revision_id != p.current_revision_id "
        "OR s.revision_number != p.current_revision_number "
        "OR s.occurred_at != p.occurred_at "
        "OR s.document_count != (SELECT COUNT(*) FROM search_documents AS d "
        "WHERE d.generation = s.generation AND d.library_id = s.library_id "
        "AND d.page_uid = s.page_uid) "
        "OR EXISTS (SELECT 1 FROM search_documents AS d "
        "LEFT JOIN search_terms AS t ON t.rowid = d.id "
        "WHERE d.generation = s.generation AND d.library_id = s.library_id "
        "AND d.page_uid = s.page_uid AND t.rowid IS NULL)) LIMIT 1",
        (generation, scope.library_id, *((caller_id,) if scope.legacy_sections else ())),
    ).first()
    if missing is not None:
        raise SearchIndexUnavailableError("Search index is incomplete in the selected scope.")


def _rows_for_scope(
    connection: Connection,
    generation: int,
    scope: _Scope,
    caller_id: str,
    query: SearchQueryV2,
    needles: tuple[str, ...],
    candidate_expression: str | None,
) -> list[tuple[object, ...]]:
    parameters: list[object] = [generation, scope.library_id]
    conditions = ["p.library_id = ?", "p.deleted_at IS NULL"]
    if scope.legacy_sections:
        conditions.append(_section_predicate(scope, "p"))
        parameters.append(caller_id)
    if query.occurred_from_us is not None:
        conditions.append("p.occurred_at >= ?")
        parameters.append(query.occurred_from_us)
    if query.occurred_before_us is not None:
        conditions.append("p.occurred_at < ?")
        parameters.append(query.occurred_before_us)
    tag_ids = tuple(tag.tag_id for tag in query.tags_any if tag.library_id == scope.library_id)
    if query.tags_any:
        if not tag_ids:
            return []
        tags = ", ".join("?" for _ in tag_ids)
        conditions.append(
            "EXISTS (SELECT 1 FROM page_tags AS pt WHERE pt.library_id = p.library_id "
            f"AND pt.page_uid = p.page_uid AND pt.tag_id IN ({tags}))"
        )
        parameters.extend(tag_ids)
        tag_count = (
            "(SELECT COUNT(*) FROM page_tags AS pt WHERE pt.library_id = p.library_id "
            f"AND pt.page_uid = p.page_uid AND pt.tag_id IN ({tags}))"
        )
    else:
        tag_count = "0"
    candidate_cte = ""
    candidate_join = ""
    exact_document_join = ""
    candidate_parameters: tuple[object, ...] = ()
    exact_short_keyword = len(needles) == 1 and len(needles[0]) <= 3
    if candidate_expression is not None:
        scoped_expression = (
            f'"{encoded_library_token(scope.library_id)}" AND ({candidate_expression})'
        )
        if exact_short_keyword:
            # A complete 1/2/3-codepoint gram is the exact literal within one
            # indexed field. Retain its document ID so a broad hit never scans
            # or transfers every normalized file body just to confirm itself.
            # Keep MATCH outermost: a normal JOIN can scan Library documents
            # first and re-run the same FTS query once per document.
            candidate_cte = (
                "WITH candidate_documents AS MATERIALIZED ("
                "SELECT search_terms.rowid FROM search_terms "
                "CROSS JOIN search_documents AS candidate "
                "WHERE search_terms MATCH ? AND candidate.generation = ? "
                "AND candidate.library_id = ? AND candidate.id = search_terms.rowid) "
            )
            exact_document_join = "JOIN candidate_documents AS cd ON cd.rowid = d.id "
            candidate_parameters = (scoped_expression, generation, scope.library_id)
        else:
            # Evaluate FTS once per Library. A correlated MATCH under a Page
            # scan reparses the same expression thousands of times.
            candidate_cte = (
                "WITH candidate_pages AS MATERIALIZED ("
                "SELECT DISTINCT candidate.library_id, candidate.page_uid "
                # SQLite may otherwise choose the Library index first and
                # run MATCH once for every document. CROSS JOIN keeps the FTS
                # posting scan outermost; the rowid lookup then narrows scope.
                "FROM search_terms CROSS JOIN search_documents AS candidate "
                "WHERE search_terms MATCH ? AND candidate.generation = ? "
                "AND candidate.library_id = ? AND candidate.id = search_terms.rowid) "
            )
            candidate_join = (
                "JOIN candidate_pages AS cp ON cp.library_id = s.library_id "
                "AND cp.page_uid = s.page_uid "
            )
            candidate_parameters = (scoped_expression, generation, scope.library_id)
    # The count projection appears before WHERE placeholders in SQL.
    count_parameters: tuple[object, ...] = tag_ids if query.tags_any else ()
    # Let SQLite test the exact literal against indexed text in C. Returning
    # whole bodies to Python for every candidate makes broad matches transfer
    # tens of MiB despite the response containing only Page metadata.
    exact_columns = (
        ", 1"
        if exact_short_keyword
        else "".join(", instr(d.normalized_text, ?) > 0" for _ in needles)
    )
    sql = (
        candidate_cte + "SELECT p.library_id, p.section_id, p.book_id, p.page_id, "
        "p.current_revision_id, p.current_revision_number, p.title, p.occurred_at, "
        + tag_count
        + ", d.source_kind, d.file_name"
        + exact_columns
        + " "
        "FROM search_page_state AS s "
        + candidate_join
        + "JOIN pages AS p ON p.library_id = s.library_id AND p.page_uid = s.page_uid "
        "AND p.section_id = s.section_id AND p.book_id = s.book_id "
        "AND p.page_id = s.page_id AND p.current_revision_id = s.revision_id "
        "AND p.current_revision_number = s.revision_number "
        "AND p.occurred_at = s.occurred_at "
        "JOIN search_documents AS d ON d.generation = s.generation "
        "AND d.library_id = s.library_id AND d.page_uid = s.page_uid "
        "AND d.revision_id = s.revision_id AND d.revision_number = s.revision_number "
        + exact_document_join
        + "WHERE s.generation = ? AND "
        + " AND ".join(conditions)
        + " ORDER BY p.library_id, p.page_id, d.id"
    )
    return [
        tuple(row)
        for row in connection.exec_driver_sql(
            sql,
            (
                *candidate_parameters,
                *count_parameters,
                *(() if exact_short_keyword else needles),
                *parameters,
            ),
        ).all()
    ]


def _query_identity(query: SearchQueryV2, needles: tuple[str, ...]) -> bytes:
    return _digest_json(
        b"patchouli-search-query-v1\0",
        {
            "keywords": needles,
            "tags_any": sorted((tag.library_id, tag.tag_id) for tag in query.tags_any),
            "occurred_from_us": query.occurred_from_us,
            "occurred_before_us": query.occurred_before_us,
            "libraries": None if query.libraries is None else sorted(query.libraries),
        },
    )


def _ranked_fingerprint(
    generation: int, ranked: list[tuple[tuple[object, ...], SearchPageV2, str | None, str | None]]
) -> bytes:
    digest = hashlib.sha256(
        b"patchouli-search-visible-ranking-v1\0"
        + f"{INDEX_VERSION}:{generation}:{_SORT_VERSION}\0".encode("ascii")
    )
    for key, item, _file_name, _needle in ranked:
        payload = json.dumps(
            (
                key,
                item.library_id,
                item.section_id,
                item.book_id,
                item.page_id,
                item.revision_id,
                item.revision_number,
                item.title,
                item.occurred_at,
                [(source.kind, source.file_name) for source in item.match_sources],
            ),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
        digest.update(len(payload).to_bytes(4, "big"))
        digest.update(payload)
    return digest.digest()


def _cursor_binding(
    actor: _ResolvedScope,
    query: SearchQueryV2,
    needles: tuple[str, ...],
    generation: int,
    ranked: list[tuple[tuple[object, ...], SearchPageV2, str | None, str | None]],
) -> CursorBinding:
    return CursorBinding(
        caller_id=actor.principal_identity.hex(),
        policy_version=actor.policy_version,
        section_id=None,
        route_identity="search-v2",
        limit=query.limit,
        query_identity=_query_identity(query, needles),
        filters_identity=_digest_json(
            b"patchouli-search-scope-v1\0",
            (
                sorted((scope.library_id, scope.legacy_sections) for scope in actor.scopes),
                actor.grants_identity.hex(),
            ),
        ),
        sort_identity=_ranked_fingerprint(generation, ranked),
    )


_CURSOR_KEY_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,19}):[0-9a-f]{64}$", re.ASCII)


def _cursor_key(index: int, key: tuple[object, ...]) -> str:
    digest = _digest_json(b"patchouli-search-last-key-v1\0", key)
    return f"{index}:{digest.hex()}"


def _cursor_start(
    cursor: str | None,
    codec: CursorCodec | None,
    binding: CursorBinding | None,
    ranked: list[tuple[tuple[object, ...], SearchPageV2, str | None, str | None]],
) -> int:
    if cursor is None:
        return 0
    if codec is None or binding is None:
        raise InvalidSearchQueryV2
    key = codec.decode(cursor, binding=binding)
    if _CURSOR_KEY_PATTERN.fullmatch(key) is None:
        raise InvalidCursorError
    index = int(key.split(":", 1)[0])
    if index >= len(ranked) or key != _cursor_key(index, ranked[index][0]):
        raise InvalidCursorError
    return index + 1


def _snippet_for_page(
    connection: Connection,
    generation: int,
    page: SearchPageV2,
    matched_name: str | None,
    matched_needle: str | None,
) -> SearchSnippetV2 | None:
    # search_documents contains only complete, strictly decoded UTF-8 text
    # from the exact current Revision. SQL substr bounds output before it is
    # transferred to Python; opaque files have no file_text document.
    conditions = (
        "d.generation = ? AND d.library_id = ? AND p.page_id = ? "
        "AND p.section_id = ? AND p.book_id = ? "
        "AND p.current_revision_id = ? AND p.current_revision_number = ? "
        "AND p.deleted_at IS NULL AND d.revision_id = p.current_revision_id "
        "AND d.revision_number = p.current_revision_number AND d.source_kind = 'file_text'"
    )
    parameters: tuple[object, ...] = (
        generation,
        page.library_id,
        page.page_id,
        page.section_id,
        page.book_id,
        page.revision_id,
        page.revision_number,
    )
    if matched_name is not None and matched_needle is not None:
        excerpt = (
            "substr(d.normalized_text, "
            f"max(1, instr(d.normalized_text, ?) - {_SNIPPET_LEADING_CODEPOINTS}), "
            f"{_SNIPPET_CODEPOINTS})"
        )
        extra = " AND d.file_name = ? AND instr(d.normalized_text, ?) > 0"
        values = (matched_needle, *parameters, matched_name, matched_needle)
    else:
        excerpt = f"substr(d.normalized_text, 1, {_SNIPPET_CODEPOINTS})"
        extra = ""
        values = parameters
    row = connection.exec_driver_sql(
        "SELECT d.file_name, " + excerpt + " FROM search_documents AS d "
        "JOIN pages AS p ON p.library_id = d.library_id AND p.page_uid = d.page_uid "
        "WHERE " + conditions + extra + " ORDER BY d.file_name LIMIT 1",
        values,
    ).one_or_none()
    if row is None:
        if matched_name is not None:
            raise SearchIndexUnavailableError("A matched search text projection is missing.")
        return None
    return SearchSnippetV2(str(row[0]), str(row[1]), matched_name is not None)


def search_pages_v2(
    engine: Engine,
    context: AuthenticatedRequestContext,
    query: SearchQueryV2,
    *,
    clock: Callable[[], int] = utc_microseconds,
    cursor_codec: CursorCodec | None = None,
) -> SearchResultV2:
    """Search one concrete SQLite snapshot; never return a partial candidate Top K."""

    def resolve(connection: Connection) -> _ResolvedScope:
        return _current_scope(connection, context, query, clock())

    return _search_pages(engine, query, resolve, cursor_codec=cursor_codec)


def search_pages_for_master(
    engine: Engine,
    encoded_session: str,
    codec: AdminSessionCodec,
    query: SearchQueryV2,
    *,
    cursor_codec: CursorCodec | None = None,
) -> SearchResultV2:
    """Search as the verified single administrator, without an Agent token.

    Verify the signed cookie and recheck its generation inside the same read
    transaction used for scope, index readiness, and result selection.
    """

    session = codec.verify_master(encoded_session)
    if session is None:
        raise AuthenticationError

    def resolve(connection: Connection) -> _ResolvedScope:
        return _master_scope(connection, session.identity_id, session.session_generation, query)

    return _search_pages(engine, query, resolve, cursor_codec=cursor_codec)


def _search_pages(
    engine: Engine,
    query: SearchQueryV2,
    resolve: Callable[[Connection], _ResolvedScope],
    *,
    cursor_codec: CursorCodec | None,
) -> SearchResultV2:
    if not isinstance(query, SearchQueryV2):
        raise TypeError("Expected a parsed search-v2 query.")
    needles = normalize_keywords(query.keywords)
    if not (
        needles
        or query.tags_any
        or query.occurred_from_us is not None
        or query.occurred_before_us is not None
    ):
        raise InvalidSearchQueryV2
    expression = candidate_match_expression(needles) if needles else None
    with engine.connect() as connection:
        # SQLite's legacy driver does not start a read transaction on SELECT.
        connection.exec_driver_sql("BEGIN")
        try:
            actor = resolve(connection)
            generation = require_ready_index(connection).generation
            for scope in actor.scopes:
                _check_scope_integrity(connection, generation, scope, actor.caller_id)
            ranked: list[tuple[tuple[object, ...], SearchPageV2, str | None, str | None]] = []
            for scope in actor.scopes:
                rows = _rows_for_scope(
                    connection,
                    generation,
                    scope,
                    actor.caller_id,
                    query,
                    needles,
                    expression,
                )
                grouped: dict[str, list[tuple[object, ...]]] = {}
                for row in rows:
                    grouped.setdefault(str(row[3]), []).append(row)
                for page_rows in grouped.values():
                    first = page_rows[0]
                    best: dict[str, int] = {}
                    sources: set[SearchMatchSourceV2] = set()
                    matched_text: list[tuple[str, str]] = []
                    for row in page_rows:
                        kind, file_name = row[9:11]
                        for keyword, matched in zip(needles, row[11:], strict=True):
                            if matched:
                                best[keyword] = max(best.get(keyword, 0), _FIELD_WEIGHT[str(kind)])
                                sources.add(
                                    SearchMatchSourceV2(str(kind), cast("str | None", file_name))
                                )
                                if kind == "file_text" and file_name is not None:
                                    matched_text.append((str(file_name), keyword))
                    if needles and not best:
                        continue
                    ordered_sources = tuple(
                        sorted(
                            sources,
                            key=lambda source: (
                                -_FIELD_WEIGHT[source.kind],
                                source.file_name or "",
                            ),
                        )[:_MAX_MATCH_SOURCES]
                    )
                    item = SearchPageV2(
                        library_id=str(first[0]),
                        section_id=str(first[1]),
                        book_id=str(first[2]),
                        page_id=str(first[3]),
                        revision_id=str(first[4]),
                        revision_number=cast("int", first[5]),
                        title=str(first[6]),
                        occurred_at=cast("int", first[7]),
                        match_sources=ordered_sources,
                    )
                    if needles:
                        key: tuple[object, ...] = (
                            -len(best),
                            -sum(best.values()),
                            -cast("int", first[8]),
                            -item.occurred_at,
                            item.library_id,
                            item.page_id,
                        )
                    else:
                        key = (-item.occurred_at, item.library_id, item.page_id)
                    matched_name, matched_needle = (
                        min(matched_text) if matched_text else (None, None)
                    )
                    ranked.append((key, item, matched_name, matched_needle))
            ranked.sort(key=lambda pair: pair[0])
            binding = (
                _cursor_binding(actor, query, needles, generation, ranked)
                if cursor_codec is not None
                else None
            )
            start = _cursor_start(query.cursor, cursor_codec, binding, ranked)
            end = min(start + query.limit, len(ranked))
            items = tuple(
                replace(
                    item,
                    snippet=_snippet_for_page(
                        connection, generation, item, matched_name, matched_needle
                    ),
                )
                for _, item, matched_name, matched_needle in ranked[start:end]
            )
            next_cursor = (
                cursor_codec.encode(
                    binding=binding, last_key=_cursor_key(end - 1, ranked[end - 1][0])
                )
                if cursor_codec is not None and binding is not None and end < len(ranked)
                else None
            )
            return SearchResultV2(items, next_cursor)
        finally:
            connection.rollback()


__all__ = [
    "SearchMatchSourceV2",
    "SearchPageV2",
    "SearchResultV2",
    "SearchSnippetV2",
    "SearchScopeError",
    "search_pages_for_master",
    "search_pages_v2",
]
