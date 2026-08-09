# Retrieval interfaces and hosted-agent responsibilities

## Retrieval boundary

Queries should normally select a Section first. This keeps policy evaluation,
result interpretation, and resource use predictable. Cross-Section search may
be added later as an explicit privileged operation.

## Direct query interface

The access layer should support structured operations such as:

- list Sections and Books;
- list Pages in a Book, optionally with summaries;
- search Page metadata and current Revision text within a Section;
- filter by Tag, type, status, or time;
- fetch a Page or an explicitly requested Revision by ID;
- return stable citations with every result.

Regular expressions, full-text syntax, and semantic queries need distinct input
fields and resource limits. User input must not be interpolated into a database
or shell expression.

## Retrieval pipeline

A baseline pipeline can combine:

1. Section and authorization filtering;
2. title, Tag, and full-text candidate recall;
3. summary or Derived Fact recall;
4. deterministic ranking and result limits;
5. optional model-assisted synthesis with source citations.

Embedding retrieval is an optional extension. It must demonstrate measurable
value over the baseline and must not require an external provider by design.

## Hosted agent

A deployment may configure a hosted retrieval agent. Its responsibilities are:

- plan and execute bounded searches;
- request additional source content only when needed;
- produce short answers with Page or Revision citations;
- generate summaries or organization suggestions;
- expose which provider and retrieval steps were used.

The public contract is provider-neutral. No model name, account, endpoint, or
deployment region is a project-wide requirement.

## Calling agents

Local tools, MCP clients, and agent skills act as authenticated callers. They
can create Pages, append Revisions, search, download, and respond to suggestions
within their assigned scopes.

Large text should be uploaded from a file or stream rather than embedded in
multiple layers of command-line JSON. Credentials belong in an operating-system
secret store or injected environment variable, never in request examples or
tracked configuration.

## Import

The core API should expose idempotent, observable single-item writes. Bulk
imports can initially be client-side orchestration over those primitives. A
dedicated bulk protocol should be added only when measurements show it is
needed.

## Evaluation

Retrieval quality needs a versioned evaluation corpus containing synthetic or
redistributable documents, questions, relevant-source judgments, latency, and
resource measurements. Ranking changes should be evaluated against this corpus
before they become defaults.
