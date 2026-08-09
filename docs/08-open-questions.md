# Open questions

These decisions are unresolved. An implementation may prototype an answer, but
it must not present that answer as a stable project contract without a public
proposal.

## Blocking the first implementation contract

- [x] Choose the implementation language and supported runtime versions:
  Python 3.13, recorded in [ADR 0001](decisions/0001-implementation-baseline.md).
- [x] Select an initial database: SQLite with FTS5, recorded in
  [ADR 0001](decisions/0001-implementation-baseline.md).
- [ ] Publish transaction, migration, backup, and restore invariants beyond the
  bootstrap migration checks.
- [ ] Define the HTTP or RPC API, error model, pagination, and idempotency keys.
- [ ] Specify the stable-ID timestamp encoding, timezone, normalization, and
  collision behavior.
- [ ] Define credential scope representation, default policy, and allow/deny
  precedence.
- [ ] Define administrative erasure across current data, history, indexes,
  exports, and backups.

## Revision and concurrency

- [ ] Is optimistic concurrency required by default for Page revisions?
- [ ] How are rejected writes represented without losing the submitted body?
- [ ] Should restore operations use a dedicated endpoint or ordinary revision
  creation with structured metadata?

## Summaries and derived facts

- [ ] Which events create, invalidate, regenerate, or remove derived facts?
- [ ] How are generator versions and stale derived data exposed to callers?
- [ ] Which derived artifacts survive a Page move, soft deletion, or erasure?
- [ ] What size and quality constraints apply to summaries?

## Retrieval

- [ ] Which baseline ranking function and evaluation metrics become normative?
- [ ] Is cross-Section search needed, and which credential may request it?
- [ ] When do embeddings provide enough measured value to justify their cost
  and privacy surface?
- [ ] How should prompt-injection content be isolated from retrieval-agent
  instructions?

## Shelves and organization

- [ ] Are Shelves user-authored saved searches, automatically maintained
  projections, or both?
- [ ] Which signals and thresholds make a Book a split or merge candidate?
- [ ] How do Derived Facts follow Page moves and Book reorganizations?
- [ ] What user interface should present, reject, or postpone suggestions?

## Content and interoperability

- [ ] Is the first release text-only, or does it support attachment metadata?
- [ ] Which Markdown profile and reference grammar are supported?
- [ ] What export format preserves IDs, Revisions, Tags, Sources, and audit data?
- [ ] Which import metadata is required to make retries idempotent?

## Hosted-agent integration

- [ ] What provider interface supports local and remote models without exposing
  content contrary to operator policy?
- [ ] Which actions may a hosted agent perform directly, and which always need
  approval?
- [ ] How are model cost, latency, prompts, and citations observed without
  logging private content unnecessarily?

## Accepted directions

- [x] Page bodies have immutable Revisions; restore creates a new Revision.
- [x] A Page belongs to one Book at a time and can move without copying history.
- [x] Tags and Shelves are retrieval projections, not content owners.
- [x] Model providers and deployment locations are operator configuration.
- [x] Automatic organization produces reviewable suggestions by default.
- [x] Public examples and workflows contain no maintainer-specific infrastructure.
