# Roadmap

PatchouliLib is design-first. Milestones describe capability order, not delivery
dates or promises.

## How to read this roadmap

This status describes the public repository at commit
`1581709d857b6e0c1a64197e649e382779145b2a`. It deliberately separates three
states:

- **implemented**: code and automated validation exist in the repository;
- **experimental or Proposed**: evidence or bounded tooling exists, but it is
  not yet a supported compatibility or recovery promise;
- **not implemented**: the capability is absent or deliberately returns an
  unavailable response.

Implementation does not accept a public proposal. Accepted decisions remain
listed under `docs/decisions/`; the FTS and backup/restore documents under
`docs/proposals/` remain Proposed.

## Milestone 0: design contract

**Status: in progress.**

- Resolve the blocking questions in [docs/08-open-questions.md](docs/08-open-questions.md).
- Define storage invariants and threat model.
- Publish an initial API contract and compatibility policy.
- Implement the accepted runtime, database, validation, and delivery baseline.

Implemented evidence includes Python 3.13, FastAPI, SQLite, Alembic migrations,
locked local validation, OCI publishing, an operator-initiated private update
helper, RFC 9457-style errors, request IDs, signed retrieval cursors, and
idempotent create/revise operations. The current HTTP behavior is tested, but a
supported-version compatibility policy and several data-safety decisions remain
Milestone 0 work.

## Milestone 1: durable core

**Status: partially implemented.**

- Implement Section, Book, Page, Revision, Tag, and Source persistence.
- Support create, read, revise, move, soft-delete, export, and restore flows.
- Add transactional audit records and administrative data erasure.
- Prove backup and restore behavior with automated tests.

Implemented now:

- Library, Section, Book, Page, immutable Revision, stable Page identifier, and
  Source persistence;
- protected Page creation and revision append routes with optimistic
  concurrency, exact citations, idempotent replay, and audit records;
- protected non-search reads for Sections, Books, Page listings, current Page
  content, and Revision history.

Still incomplete:

- Tag persistence;
- Page move, soft-delete, content restore, and export flows;
- administrative erasure across live data, history, indexes, exports, and
  backups.

Experimental backup creation, verification, and restore into a new empty
destination have unit and Linux container-smoke evidence. The proposal remains
Proposed, so real-data restore, in-place replacement, point-in-time recovery,
and migration activation are not supported operations.

## Milestone 2: agent access

**Status: the bounded access path is implemented.**

- Publish a CLI and an MCP-compatible adapter.
- Add scoped credentials, rotation, and revocation.
- Provide idempotent import primitives and structured error responses.

The repository now includes a typed Python client, Agent CLI, stdio MCP adapter,
bundled Agent Skill, local operator CLI, password-protected administration
console, scoped Section grants, credential expiry/recovery/revocation, and
structured client-side protocol validation. Packaged synthetic end-to-end tests
exercise the supported create, replay, revise, list, current, and history path.

This does not yet establish a stable external release or complete every future
credential-policy question.

## Milestone 3: retrieval

**Status: exact non-search retrieval is implemented; full-text search is not.**

- Add Section-scoped full-text search, tags, and summaries.
- Establish a relevance evaluation corpus and measurable baselines.
- Add provider-neutral agent-assisted retrieval behind explicit configuration.

Five protected non-search read routes and integrity-protected cursor pagination
are implemented. A redistributable FTS5 evaluator and a detailed alpha proposal
exist, but the proposal remains Proposed. The protected search route therefore
fails explicitly as `search_unavailable`; there is no production FTS migration,
tag retrieval, summary generation, embedding retrieval, or model-provider call.

## Milestone 4: derived knowledge

**Status: not implemented.**

- Add traceable facts and source links as rebuildable derived data.
- Evaluate optional vector retrieval against the baseline.
- Add reviewable organization suggestions with cooldown and audit semantics.

## Not scheduled

- Hosted multi-tenant service.
- Automatic content moves without approval.
- Binary asset storage.
- A general content browsing and editing web interface. The bounded
  administration console is implemented, but it is not a reader or editor for
  stored knowledge.
