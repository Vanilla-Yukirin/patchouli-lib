# FTS5 current-Revision retrieval for alpha

Status: **Proposed**

## Decision request and gate

Adopt an application-tokenized SQLite FTS5 index as the alpha candidate-recall
baseline for Section-scoped search over current Revisions. Caller text is always
literal data, never FTS5 syntax. The index is derived state and may be rebuilt
from authoritative Page and Revision rows.

**No FTS migration may land while this proposal remains Proposed.** Acceptance
requires a public maintainer-reviewed change of this status to **Accepted**.
Merging an evaluation script, opening a pull request, or passing CI does not by
itself accept the production choice.

This proposal chooses an alpha index representation and a bounded operational
query path. It does not make ranking scores, linguistic relevance, or snippet
format a stable public compatibility promise.

## Accepted invariants

This proposal preserves decisions that are already public:

- a query selects and authorizes one Section before recall;
- search considers only each Page's current Revision;
- every result identifies the exact Page and Revision that was searched;
- the current pointer, derived search row, mutation audit event, and successful
  idempotency record change in one transaction;
- caller input is never interpolated into SQL or an FTS expression;
- query text stays in a POST body and out of ordinary logs;
- result ordering is deterministic for an unchanged dataset; and
- the public contract remains independent of deployment target and model
  provider.

The accepted Agent MVP remains the source for authorization, hidden-resource,
pagination, and citation behavior. This proposal does not widen those scopes.

## Evidence

### Method

The redistributable evaluator compares three SQLite FTS5 candidates on six
original synthetic Chinese/English documents and 15 relevance judgments:

- plain `unicode61` over original text;
- SQLite's built-in `trigram` tokenizer; and
- application-generated tokens stored in a `unicode61` table.

The fixture covers one-, two-, and three-or-more-character Han queries, mixed
English/Han input, an English phrase, punctuation, and FTS-looking text such as
`AND`, `prefix*`, column selectors, quotes, parentheses, and `NEAR`. It also
checks replacement of old current content, repeated-query ordering, and a
stable Page-ID tie break. Unicode 15.1 regressions separately exercise
multi-character runs from Extensions I, G, and H and verify generation of
one-, two-, and three-character grams.

Every synthetic document carries a Section identity. The isolation probe first
ranks two same-term documents in the authorized Section, then adds 12 stronger
same-term documents in a different Section. It verifies that the authorized
result count and rank order are unchanged, `LIMIT 1` still returns the same
authorized Page, and no other-Section Page appears. A separate query against the
other synthetic Section proves those decoys were present and searchable, rather
than accidentally absent from the index.

For a small resource comparison, the evaluator deterministically replicates the
fixture to 384 documents. It records compacted database size above an identical
non-FTS baseline and wall-clock build, current-document update, and query
latency. Timings are diagnostic observations, not release thresholds or
cross-machine guarantees.

Reproduce the complete report with:

```sh
uv run python scripts/evaluate_fts.py --format markdown
```

### Quality result

On Python 3.13.3, SQLite 3.47.1, and Unicode data 15.1.0, the fixture produced:

| Candidate | Mean recall | Mean precision | Han 1 | Han 2 | Han 3+ | Mixed | Literal syntax |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `unicode61` | 0.667 | 0.633 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 |
| `trigram` | 0.800 | 0.800 | 0.000 | 0.000 | 1.000 | 0.000 | 1.000 |
| `application_cjk_ngrams` | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

The per-dimension cells are recall. All three candidates replaced an old
current-document row and produced the same deterministic order on repeated
queries. The explicit equal-score probe ordered `ranking-a` before
`ranking-z`. Every candidate also passed the Section-isolation probe: the two
authorized results retained their original count and order after the 12 stronger
other-Section rows were added, and the authorized `LIMIT 1` remained unchanged.

The application candidate's literal-syntax precision depends on encoding
non-whitespace punctuation as data tokens. For example, `prefix*` requires both
the encoded word and encoded `*`; it does not execute a prefix query or match
the unpunctuated `prefixable` decoy.

### Resource result

One representative 384-document run with Section-scoped FTS tokens and the
precomputed rank-token projection produced the following indicative values.
Use the command above for the exact current run rather than treating this table
as a capacity baseline.

| Candidate | Index overhead | Bytes/document | Build median | Update median | Query median | Query p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `unicode61` | 5,718,016 B | 14,890.667 B | 555.5 ms | 12.3 ms | 2.1 ms | 4.2 ms |
| `trigram` | 5,951,488 B | 15,498.667 B | 576.3 ms | 13.9 ms | 2.6 ms | 4.7 ms |
| `application_cjk_ngrams` | 8,622,080 B | 22,453.333 B | 1,032.9 ms | 15.5 ms | 1.3 ms | 4.0 ms |

All candidates include the same precomputed ranking projection, so these values
measure the complete proposed query path rather than isolated FTS bytes. The
application candidate used about 1.5 times the bytes per document of
`unicode61` in this deliberately small corpus. The sample is too small and
repetitive for capacity planning, but it exposes the direction of the storage
and build-cost trade-off. Larger representative public corpora and release
thresholds remain follow-up work.

The resource guardrail probe additionally indexed and queried an exact 2 MiB
low-entropy synthetic body. A separate exact 2 MiB high-entropy Han body
crossed the 65,536-derived-token ceiling and failed before any document or
index row became durable. The probe served a 256-candidate authorized broad
match while 1,024 same-term candidates existed in another Section, and failed
closed when that other Section was queried because its own candidate set
exceeded 256. The recall SQL has no ordering step before `LIMIT 257`; its query
plan uses no temporary sort, and SQLite VM evidence places the limit counter's
`DecrJumpZero` before `VNext`, so it stops rather than sorting the complete
selected-Section match set.

Queries above 2,048 code points or 4,096 UTF-8 bytes failed before
normalization and token generation. A separate 65-unique-token query failed
before FTS execution. Query-time ranking read only the bounded candidate IDs
and precomputed token rows, not original bodies. These are executable alpha
work bounds, not claims that a 384-document timing sample predicts production
capacity.

`LIMIT 257` alone does not bound the internal work needed to find FTS matches:
two individually common terms can have a very late or empty intersection. The
evaluator therefore also fixes a hard ceiling of 1,024 searchable Pages per
Section. Its adversarial Section divides two terms across disjoint halves of
all 1,024 Pages and executes the empty conjunction. A 1,025th write is rejected
before derived-state mutation; when an over-limit authoritative row is injected
to simulate incompatible pre-existing state, search fails before `MATCH` and
returns no partial hit.

## Proposed alpha choice

### Token and index representation

Use `application_cjk_ngrams`, versioned as an internal tokenizer schema:

1. normalize indexed and query text with NFKC and Unicode case folding;
2. encode every contiguous Han run as overlapping one-, two-, and
   three-character grams;
3. encode every contiguous non-Han alphanumeric run as one word token;
4. encode each non-whitespace punctuation code point as literal data; and
5. hex-encode token payloads behind type/width prefixes before inserting them
   into an FTS5 `unicode61` table; and
6. prefix every FTS token with the SHA-256 digest of its Section ID.

The Han range table is explicitly versioned as Unicode 15.1. It covers CJK
Unified Ideographs, Extension A, Extensions B through I, Compatibility
Ideographs, and the Compatibility Supplement. Range changes are tokenizer
schema changes and require evaluation plus index rebuild; characters outside
the versioned ranges are not silently promoted to Han merely because a runtime
classifies them as alphanumeric.

Encoding prevents caller content from becoming FTS grammar and avoids relying
on a system-installed segmentation dictionary. The Section prefix gives each
Section a disjoint posting vocabulary, so a broad term in another Section does
not enlarge the selected Section's posting list. It is a namespace prefix, not
a secret or authorization mechanism. The tokenized columns represent title,
summary, tags, and current body. Section ID, Page ID, and Revision ID are
filtering or citation fields, not searchable terms.

Section ID is an unindexed FTS column used as a bound equality constraint in the
same candidate query: `MATCH :compiled_query AND section_id = :section_id`.
Authorization resolves the Section before this query executes. The Section
predicate is therefore inside candidate recall and before the application rank
function, ordering, and `LIMIT`; the implementation must not retrieve globally
and filter afterward. Counts, scores, cursors, and snippets are computed only
from the authorized candidate set. The equality constraint remains mandatory
even though the posting tokens are Section-scoped.

The FTS index contains exactly one row per searchable Page and names the exact
current Revision. A companion derived table stores unique unscoped rank tokens
by Section, Page, field, and token. It is populated when content is accepted;
search never retokenizes a stored body. A Revision append replaces both derived
projections in the same write transaction that advances the current pointer.
Historical Revisions remain fetchable by citation but are not search candidates.

The alpha write path accepts at most 2 MiB of current body content and 65,536
derived token rows across title, summary, tags, and body. It checks both limits
before deleting or inserting a derived row. Crossing either limit rejects the
index update and leaves the authoritative current Revision and prior compatible
search projection unchanged. This token-row limit also bounds the amplified
Section-prefixed FTS representation: the index may be several times larger than
the source bytes, but it cannot grow with an unbounded number of unique Han
one-, two-, and three-grams from one document. The route-level status and
Problem Details code for an indexability rejection remain a public contract
decision and must be fixed before production implementation.

The alpha also permits at most 1,024 searchable Pages in one Section. Creation
or movement of a Page into a full Section fails before changing its derived
rows. Before every search, an indexed `(section_id, document_id)` count reads at
most 1,025 rows; a count above 1,024 rejects the query before FTS recall. Given
the one-current-row-per-Page invariant and Section-prefixed terms, each of at
most 64 query-token posting lists then contains at most 1,024 entries. This
bounds even disjoint or late intersections inside FTS; `LIMIT 257` separately
bounds produced candidates and the rank projection reads at most 256 candidate
IDs. An index whose FTS row invariant is not proven at open/rebuild time remains
`search_unavailable` rather than queryable.

### Literal alpha query grammar

The alpha input is one non-empty literal text value of at most 2,048 Unicode
code points and 4,096 UTF-8 bytes, producing at most 64 unique tokens. The raw
limits are checked before NFKC normalization, case folding, or token generation,
so a repeated-token request cannot consume unbounded preprocessing work. There
is no caller-visible `MATCH`, boolean, prefix, column, phrase, `NEAR`,
regular-expression, fuzzy, or wildcard grammar. The service produces unique
encoded tokens and joins them internally with `AND` using bound parameters.

This is conjunctive token containment, not a promise of phrase order or
adjacency. Han grams from a longer query can occur in different positions, and
English words can match across indexed fields. Those limitations must be stated
in capabilities or search documentation before the route is enabled.

Candidate recall reads the first at most 257 Page IDs from the already
authorized, Section-scoped posting list without ordering them. Zero through 256
candidates continue to deterministic ranking; 257 means the 256-candidate alpha
ceiling was exceeded and the query fails closed without partial results. The
overflow subset need not be deterministic because it is never returned or
ranked. Omitting an order before this limit is part of the work bound: ordering
an unindexed Page ID would require scanning and sorting every match before the
limit could apply. The public Problem Details status/code for overflow remains
to be fixed before the search route is advertised; migration code must not
invent one. The existing result-page maximum remains 100.

The 1,024-Page Section ceiling is an alpha compatibility limit, not merely an
observability threshold. The service must expose that limit in capabilities
before enabling search. Raising it requires new public resource evidence and a
proposal update; silently increasing it would invalidate the posting-work
bound.

### Deterministic alpha ranking

Do not use FTS5 `bm25` for the alpha rank. Its inverse-document-frequency
statistics span the whole virtual table, so documents from another Section can
change an authorized result's score even when the Section predicate excludes
them from output.

Instead, join the bounded candidate IDs and query tokens to the precomputed
rank-token projection. For every field, compute the fraction of unique encoded
query tokens present in that field, then apply fixed logical weights of title
10, tags 6, summary 4, and body 1. Sort the resulting score descending, followed
by opaque Page ID ascending as the total-order tie break. Ranking reads no
original body and has no corpus-frequency input, so another Section cannot
alter candidate count, score, order, work bound, or `LIMIT`. Numeric scores are
internal and are not a stable API value.

Any later default that changes token generation, field weights, or tie-break
semantics requires an evaluation update and an explicit index/query version.
Opaque pagination cursors remain bound to that version under the accepted Agent
MVP contract.

### Snippet limitation

Native FTS5 snippets are not usable for this candidate because they expose the
encoded token stream rather than original Markdown. The alpha search layer must
generate a separately bounded, plain-text excerpt from the authorized current
Revision after ranking. It must not render Markdown or place encoded tokens in
the response.

Exact excerpt anchoring, highlighting, Unicode offset units, and behavior when
conjunctive tokens occur in different fields remain deferred. Until those are
accepted, snippets are explicitly non-stable presentation data and cannot be
used as citations; Page and Revision identity remain authoritative.

## Compatibility, rebuild, and rollback

- Tokenizer schema version and Unicode database version are index metadata.
  Opening an incompatible index fails closed as `search_unavailable`; it does
  not silently query with new normalization rules.
- The index is a derived projection, not part of backup authority. Backup and
  restore preserve Pages, Revisions, current pointers, and index-version
  metadata; restore may rebuild the FTS and rank-token rows before search is
  marked ready.
- A rebuild reads only current Revisions, writes a fresh versioned index, checks
  FTS row, Section-prefix, rank-token, and citation invariants, and switches
  versions atomically. It applies the same byte and derived-token ceilings;
  encountering an unindexable current Revision aborts the candidate build and
  reports the Page for operator remediation without exposing its content.
  Interrupted or rejected builds leave the prior compatible index active or
  search unavailable.
- A rebuild also counts current Pages per Section and rejects any Section above
  1,024 before activation. It never builds a partial searchable subset.
- Rolling back the search feature drops or disables only the derived index and
  capability. It never deletes or rewrites a Page, Revision, Source, audit row,
  or idempotency record.
- A migration that creates this projection must provide deterministic rebuild
  and downgrade behavior. Its schema and transaction ordering require separate
  review after this proposal is accepted.

## Dependency and license impact

The choice adds no package, model, hosted provider, segmentation dictionary, or
runtime service. It uses the already selected SQLite FTS5 capability and Python
standard-library Unicode normalization. Range endpoints are versioned from the
Unicode Consortium's [Unicode 15.1 Blocks data][unicode-blocks], which is
distributed under the [Unicode License v3][unicode-license]; the project does
not vendor or parse that data file at runtime. The evaluation corpus is original
synthetic text marked `CC0-1.0`; no private archive or external training corpus
was used.

Because token bytes depend on Unicode data, a supported runtime change can
require a rebuild even when application code is unchanged. That operational
cost is preferable to introducing a dictionary dependency and its language,
versioning, packaging, and license surface for the alpha.

The explicit Unicode 15.1 range table, not the runtime Unicode category alone,
defines Han tokenization. A runtime whose normalization data differs from the
recorded index metadata still requires a fail-closed rebuild before search.

## Still deferred

Acceptance of this proposal would not settle:

- a long-term normative ranking function, score contract, or quality threshold;
- phrase, adjacency, stemming, synonym, fuzzy, prefix, or advanced query syntax;
- exact snippet anchoring, highlighting, offset units, or Markdown treatment;
- cross-Section search or any authorization expansion;
- embeddings, hosted-model reranking, or provider selection;
- corpus scale, production capacity targets, or latency service objectives;
- any increase to the alpha 1,024-searchable-Page per-Section ceiling;
- public error status/codes and remediation policy for query, candidate, body,
  or derived-token resource-limit rejection;
- index tuning and maintenance policy beyond rebuildability and current-only
  correctness.

Those decisions need new public evidence. They must not be inferred from this
synthetic experiment or silently frozen by the first migration.

[unicode-blocks]: https://www.unicode.org/Public/15.1.0/ucd/Blocks.txt
[unicode-license]: https://www.unicode.org/license.txt
