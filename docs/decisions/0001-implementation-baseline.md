# ADR 0001: implementation and delivery baseline

- Status: Accepted
- Date: 2026-08-10

The private-update transport and rollback clauses in this baseline are
superseded by [ADR 0002](0002-manual-private-updates.md). The remaining
implementation baseline is unchanged.

## Context

PatchouliLib needs a small, reproducible implementation substrate before domain
APIs are added. The project is text-first, self-hostable, and currently expects
one service instance with modest storage requirements. Retrieval experiments
need SQLite full-text search, while future agent integrations benefit from a
well-supported Python ecosystem.

The baseline must support Windows development, Linux CI, OCI containers, schema
migrations, and deployments that do not expose an operator's private target in
the public repository.

## Decision

- Python 3.13 is the initial runtime.
- FastAPI provides the HTTP application boundary.
- SQLAlchemy 2 and Alembic provide persistence and schema migrations.
- SQLite with FTS5 is the initial database and retrieval baseline.
- uv manages locked Python dependencies and environments.
- Ruff, strict MyPy, pytest, and coverage are required validation layers.
- OCI images are the supported deployment artifact.
- GitHub Actions validates pull requests and publishes immutable image digests.
- Private deployments receive only an exact image digest through
  operator-owned infrastructure.

This decision establishes implementation plumbing, not the unresolved domain
API, authorization policy, identifier grammar, backup policy, or model provider.

## Consequences

- Contributors can run one cross-platform validation entry point.
- Development and deployment use the same locked dependencies and container.
- SQLite migrations and FTS5 availability can be tested before domain work.
- Deployments can remain private while the implementation stays public.
- Supporting another database or Python runtime requires a new compatibility
  proposal and migration plan.
- The initial published image targets `linux/amd64`; additional architectures
  require CI and runtime validation before support is claimed.

## Security and operations

- Containers run as a non-root user and bind through operator configuration.
- The sample Compose configuration exposes the API on loopback by default.
- Public workflows contain no real host, account, path, port, domain, or key.
- A deployment helper must validate the requested repository and digest and
  wait for health. Its invocation and failure policy are defined by ADR 0002.
- Database changes must remain backward-compatible with the previous image until
  a tested database rollback policy is accepted.
