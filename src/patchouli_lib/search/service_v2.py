"""Transaction-consistent current-Page search reader for the v1 HTTP endpoint.

The current response has no cursor and does not expose scores or query literals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from sqlalchemy import Connection, Engine

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.api.authentication import AuthenticatedRequestContext
from patchouli_lib.auth.library_policy import LibraryAction
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, SectionAction
from patchouli_lib.auth.service import AuthenticationError, utc_microseconds
from patchouli_lib.search.index_v2 import SearchIndexUnavailableError, require_ready_index
from patchouli_lib.search.literal_v2 import candidate_match_expression, normalize_keywords
from patchouli_lib.search.query_v2 import InvalidSearchQueryV2, SearchQueryV2

_FIELD_WEIGHT = {"title": 100, "file_name": 30, "file_text": 10}
_MAX_MATCH_SOURCES = 8


class SearchScopeError(RuntimeError):
    """An explicit Library or Tag identity is outside the visible scope."""

    def __init__(self) -> None:
        super().__init__("Search scope is not available.")


@dataclass(frozen=True, slots=True)
class SearchMatchSourceV2:
    kind: str
    file_name: str | None


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


@dataclass(frozen=True, slots=True)
class SearchResultV2:
    items: tuple[SearchPageV2, ...]


@dataclass(frozen=True, slots=True)
class _Scope:
    library_id: str
    legacy_sections: bool


def _current_scope(
    connection: Connection,
    context: AuthenticatedRequestContext,
    query: SearchQueryV2,
    now: int,
) -> tuple[_Scope, ...]:
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
        readable = {
            grant.library_id: _Scope(grant.library_id, False)
            for grant in repository.list_credential_library_grants(
                home_library_id=identity.library_id,
                caller_id=identity.id,
                credential_id=credential.id,
            )
            if LibraryAction.READ in grant.actions and repository.library_exists(grant.library_id)
        }
    else:
        has_read = any(
            grant.action is SectionAction.PAGE_READ
            for grant in repository.list_grants(identity.library_id, identity.id)
        )
        readable = {identity.library_id: _Scope(identity.library_id, True)} if has_read else {}

    return _select_scopes(connection, readable, query)


def _master_scope(
    connection: Connection,
    identity_id: str,
    session_generation: int,
    query: SearchQueryV2,
) -> tuple[_Scope, ...]:
    if not MasterTokenRepository(connection).is_session_generation_current(
        identity_id, session_generation
    ):
        raise AuthenticationError
    readable = {
        str(row[0]): _Scope(str(row[0]), False)
        for row in connection.exec_driver_sql("SELECT id FROM libraries ORDER BY id")
    }
    return _select_scopes(connection, readable, query)


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
        if exact_short_keyword:
            # A complete 1/2/3-codepoint gram is the exact literal within one
            # indexed field. Retain its document ID so a broad hit never scans
            # or transfers every normalized file body just to confirm itself.
            candidate_cte = (
                "WITH candidate_documents AS MATERIALIZED ("
                "SELECT search_terms.rowid FROM search_terms "
                "JOIN search_documents AS candidate ON candidate.id = search_terms.rowid "
                "WHERE search_terms MATCH ? AND candidate.generation = ? "
                "AND candidate.library_id = ?) "
            )
            exact_document_join = "JOIN candidate_documents AS cd ON cd.rowid = d.id "
            candidate_parameters = (candidate_expression, generation, scope.library_id)
        else:
            # Evaluate FTS once per Library. A correlated MATCH under a Page
            # scan reparses the same expression thousands of times.
            candidate_cte = (
                "WITH candidate_pages AS MATERIALIZED ("
                "SELECT DISTINCT candidate.library_id, candidate.page_uid "
                "FROM search_terms JOIN search_documents AS candidate "
                "ON candidate.id = search_terms.rowid "
                "WHERE search_terms MATCH ? AND candidate.generation = ? "
                "AND candidate.library_id = ?) "
            )
            candidate_join = (
                "JOIN candidate_pages AS cp ON cp.library_id = s.library_id "
                "AND cp.page_uid = s.page_uid "
            )
            candidate_parameters = (candidate_expression, generation, scope.library_id)
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


def search_pages_v2(
    engine: Engine,
    context: AuthenticatedRequestContext,
    query: SearchQueryV2,
    *,
    clock: Callable[[], int] = utc_microseconds,
) -> SearchResultV2:
    """Search one concrete SQLite snapshot; never return a partial candidate Top K."""

    def resolve(connection: Connection) -> tuple[tuple[_Scope, ...], str]:
        return (
            _current_scope(connection, context, query, clock()),
            context.authenticated.caller.id,
        )

    return _search_pages(engine, query, resolve)


def search_pages_for_master(
    engine: Engine,
    encoded_session: str,
    codec: AdminSessionCodec,
    query: SearchQueryV2,
) -> SearchResultV2:
    """Search as the verified single administrator, without an Agent token.

    Verify the signed cookie and recheck its generation inside the same read
    transaction used for scope, index readiness, and result selection.
    """

    session = codec.verify_master(encoded_session)
    if session is None:
        raise AuthenticationError

    def resolve(connection: Connection) -> tuple[tuple[_Scope, ...], str]:
        return (
            _master_scope(connection, session.identity_id, session.session_generation, query),
            "",
        )

    return _search_pages(engine, query, resolve)


def _search_pages(
    engine: Engine,
    query: SearchQueryV2,
    resolve: Callable[[Connection], tuple[tuple[_Scope, ...], str]],
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
            scopes, caller_id = resolve(connection)
            generation = require_ready_index(connection).generation
            for scope in scopes:
                _check_scope_integrity(connection, generation, scope, caller_id)
            ranked: list[tuple[tuple[object, ...], SearchPageV2]] = []
            for scope in scopes:
                rows = _rows_for_scope(
                    connection,
                    generation,
                    scope,
                    caller_id,
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
                    for row in page_rows:
                        kind, file_name = row[9:11]
                        for keyword, matched in zip(needles, row[11:], strict=True):
                            if matched:
                                best[keyword] = max(best.get(keyword, 0), _FIELD_WEIGHT[str(kind)])
                                sources.add(
                                    SearchMatchSourceV2(str(kind), cast("str | None", file_name))
                                )
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
                    ranked.append((key, item))
            ranked.sort(key=lambda pair: pair[0])
            return SearchResultV2(tuple(item for _, item in ranked[: query.limit]))
        finally:
            connection.rollback()


__all__ = [
    "SearchMatchSourceV2",
    "SearchPageV2",
    "SearchResultV2",
    "SearchScopeError",
    "search_pages_for_master",
    "search_pages_v2",
]
