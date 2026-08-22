# PatchouliLib public design

This directory is the public design source of truth for PatchouliLib. Documents
describe accepted principles, provisional contracts, and explicitly open
questions. They must not contain private deployment details or operator data.

## Map

| Document | Purpose | Status |
| --- | --- | --- |
| [01-product-positioning.md](01-product-positioning.md) | Product scope and principles | Accepted direction |
| [02-library-domain-model.md](02-library-domain-model.md) | Core entities and invariants | Accepted direction |
| [03-page-revision-and-history.md](03-page-revision-and-history.md) | Revision, restore, and deletion semantics | Accepted direction |
| [04-distillation-and-summary.md](04-distillation-and-summary.md) | Summaries and derived facts | Partly open |
| [05-retrieval-and-cloud-agent.md](05-retrieval-and-cloud-agent.md) | Retrieval interfaces and agent roles | Partly open |
| [06-identifiers-and-references.md](06-identifiers-and-references.md) | Stable IDs and content references | Partly open |
| [07-authentication-and-audit.md](07-authentication-and-audit.md) | Credentials, authorization, and audit | Partly open |
| [08-open-questions.md](08-open-questions.md) | Decision ledger | Active |
| [09-automatic-organization.md](09-automatic-organization.md) | Reviewable split and merge suggestions | Experimental direction |

## Engineering documents

- [Development, validation, and delivery](development-and-delivery.md)
- [Agent contribution workflow](agent-contribution-workflow.md)
- [ADR 0001: implementation and delivery baseline](decisions/0001-implementation-baseline.md)
- [ADR 0002: operator-initiated private updates](decisions/0002-manual-private-updates.md)

## Status vocabulary

- **Accepted direction**: stable enough to guide prototypes, but still subject
  to change before the first supported release.
- **Partly open**: core intent is accepted; contract details still need a public
  proposal.
- **Experimental direction**: useful hypothesis that must be evaluated before
  it becomes a compatibility promise.
- **Active**: unresolved decisions; implementations must not silently choose a
  permanent answer.

## Changing the design

Use the design-proposal issue template for changes that affect entities,
storage invariants, authorization, or public interfaces. A proposal should
state the problem, constraints, alternatives, migration impact, and security or
privacy consequences.
