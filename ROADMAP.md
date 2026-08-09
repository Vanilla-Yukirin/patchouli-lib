# Roadmap

PatchouliLib is design-first. Milestones describe capability order, not delivery
dates or promises.

## Milestone 0: design contract

- Resolve the blocking questions in [docs/08-open-questions.md](docs/08-open-questions.md).
- Define storage invariants and threat model.
- Publish an initial API contract and compatibility policy.
- Implement the accepted runtime, database, validation, and delivery baseline.

Bootstrap status: Python 3.13, FastAPI, SQLite/FTS5, Alembic, locked local
validation, OCI publishing, and private-deployment plumbing are in place. The
domain API and data-safety contracts remain Milestone 0 work.

## Milestone 1: durable core

- Implement Section, Book, Page, Revision, Tag, and Source persistence.
- Support create, read, revise, move, soft-delete, export, and restore flows.
- Add transactional audit records and administrative data erasure.
- Prove backup and restore behavior with automated tests.

## Milestone 2: agent access

- Publish a CLI and an MCP-compatible adapter.
- Add scoped credentials, rotation, and revocation.
- Provide idempotent import primitives and structured error responses.

## Milestone 3: retrieval

- Add Section-scoped full-text search, tags, and summaries.
- Establish a relevance evaluation corpus and measurable baselines.
- Add provider-neutral agent-assisted retrieval behind explicit configuration.

## Milestone 4: derived knowledge

- Add traceable facts and source links as rebuildable derived data.
- Evaluate optional vector retrieval against the baseline.
- Add reviewable organization suggestions with cooldown and audit semantics.

## Not scheduled

- Hosted multi-tenant service.
- Automatic content moves without approval.
- Binary asset storage.
- A specific web interface.
