# Automatic organization: reviewable Book suggestions

## Principle

PatchouliLib may calculate and present Book split or merge suggestions. It does
not execute those suggestions automatically by default. A user or authorized
caller reviews the proposed change.

## Split suggestions

A split workflow can use two stages:

1. **Deterministic screening** measures size, topic diversity, retrieval
   confusion, or other published signals.
2. **Semantic review** evaluates candidates and writes a reasoned proposal with
   Page assignments and confidence.

The output is a proposal, not a content mutation. A proposal must identify the
input Revision set and become stale when relevant content changes.

## Merge suggestions

Merge screening compares similarity between Books with cohesion inside each
Book. Exact distance functions and thresholds remain experimental. A merge
proposal must explain naming, Section membership, Page movement, Tag impact,
and how links remain valid.

## Applying a decision

An accepted proposal uses ordinary audited operations:

- create the destination Book if necessary;
- move Pages by changing ownership metadata;
- preserve Page IDs and Revision histories;
- invalidate or update affected derived indexes;
- record the proposal, approver, operations, and result.

Rejecting or postponing a proposal is also an audit event.

## Scheduling and cooldown

A possible scheduler evaluates only Books changed since their last scan and
enforces a minimum interval between scans. Rejected suggestions enter a longer
cooldown to avoid repeated noise. Exact intervals must be configurable and
should not become API contracts.

## Presentation

Book-level queries may include a compact, non-blocking suggestion notice. A
future interface could also expose a review queue or timeline. Retrieval results
must remain usable when suggestion generation is unavailable.

## Safety constraints

- Suggestions cannot bypass authorization.
- Source text and Revision history are never rewritten by organization jobs.
- Models receive only content allowed by the deployment's provider policy.
- Every suggestion cites the Pages and signals that produced it.
- There must be a deterministic way to dismiss, expire, and rebuild suggestions.

## Open evaluation work

- split and merge quality metrics;
- false-positive cost and acceptable review load;
- Derived Fact behavior during reorganization;
- provider-neutral semantic review;
- interaction between manual decisions and later scans.
