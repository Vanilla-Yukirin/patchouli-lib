# Stable identifiers and references

## Page identifier

The current design direction uses a sortable occurrence-time component and a
short ASCII slug:

```text
<occurrence-time>-<short-english-slug>
```

An exact example is intentionally omitted until the timestamp encoding,
timezone, collision handling, and normalization rules are decided. These are
compatibility-sensitive details, not formatting trivia.

## Time semantics

Occurrence time describes when the documented event happened or was assigned,
not when the database row was created or last updated. A caller may provide it
during initial ingestion. The service also records independent creation and
update timestamps.

## Stability

A Page ID is intended to remain stable for the Page's lifetime. Changing it is
an exceptional administrative migration that must:

- detect collisions before writing;
- update derived reference indexes transactionally;
- preserve an alias or redirect when practical;
- emit an audit event;
- never rewrite historical Revision bodies in place.

Whether source Markdown references are rewritten or old aliases remain valid is
an open migration decision.

## Human title

The human-readable title is a separate field and can use any supported language.
Changing the title does not normally change the Page ID.

## Content reference syntax

Page bodies reference other Pages with wiki-style syntax:

```text
[[stable-page-id]]
```

A future extension may add optional display text or a specific Revision target.
The parser must define escaping and malformed-reference behavior before the
syntax becomes a compatibility contract.

## Reference index

Reference relationships are derived from Revision content. The service may
materialize a reverse index for queries such as “what links here?”, but the
index is rebuildable and never replaces source Markdown.

Other domain relationships that require their own lifecycle or authorization
may still deserve structured fields. The wiki reference syntax should not be
used to avoid modeling essential invariants.
