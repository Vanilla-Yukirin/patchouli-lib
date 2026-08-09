# Distillation, derived facts, and summaries

## Motivation

Repeatedly loading every full document is expensive and often unnecessary.
PatchouliLib separates canonical source text from smaller retrieval aids.

## Page summary

A summary is a short Page attribute used during recall.

- A caller may provide a summary with a new Revision.
- When absent, a configured background worker may generate one asynchronously.
- Generation state must be explicit: pending, ready, or failed.
- Failed generation can be retried without blocking access to source content.
- A summary is derived data and can be rebuilt.

Implementations must retain the generator identity, configuration version, and
source Revision so stale summaries can be detected.

## Derived facts

A Derived Fact is a concise statement extracted from one or more source
Revisions.

- It has its own identifier and lifecycle.
- Every fact links to the exact source Revisions that support it.
- It is a cache and navigation aid, not canonical evidence.
- Source changes mark dependent facts stale before they are regenerated.
- Deleting or hiding a source must update retrieval visibility for its facts.

The triggers for create, update, invalidation, and deletion remain open.

## Retrieval layers

Candidate recall inputs include:

- Section and Book context;
- titles and summaries;
- Tags;
- full-text search;
- Derived Facts;
- optional embeddings evaluated against a measured baseline.

No one layer should be described as authoritative. Responses that synthesize
knowledge must cite Pages or exact Revisions.

## References

Page bodies can contain `[[stable-page-id]]` references. A reference index is
derived by scanning content after an accepted write. See
[06-identifiers-and-references.md](06-identifiers-and-references.md).

## Safety and privacy

Generated summaries and facts inherit the visibility of their source content.
A background model must not receive content unless the deployment's configured
data policy permits that provider and request. Provider calls must be explicit,
auditable, and replaceable.
