# PatchouliLib Python client

This directory is an independently buildable Python distribution containing
the typed PatchouliLib Agent HTTP wire. It deliberately does not install the
PatchouliLib server runtime.

The alpha surface covers:

- `/api/v1` capabilities and caller self-description;
- Section, Book, Page, Revision, cursor, and exact-citation models;
- Section-scoped POST search;
- explicit archive create and Revision append operations;
- RFC 9457 Problem Details and protected-response headers; and
- bounded, idempotency-aware transport retries.

The caller supplies a bearer token to each operation. The client does not read
or write profiles, command-line arguments, logs, or an operating-system secret
store. CLI, MCP, Agent Skill, and live-network end-to-end support are later
layers over this package.

## Development

From this directory:

```sh
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
uv run python scripts/verify_artifacts.py
```

All examples and tests use synthetic data and deterministic mock transports.
The artifact check verifies that both distributions carry the MIT license file
and declare it in their core metadata.
