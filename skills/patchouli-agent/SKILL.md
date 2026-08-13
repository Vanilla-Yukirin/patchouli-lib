---
name: patchouli-agent
description: Use the shipped PatchouliLib CLI or stdio MCP adapter to diagnose caller access, discover granted Sections and Books, archive or revise Markdown explicitly, run Section-scoped searches, and fetch exact Revision citations. Use when an Agent must work with PatchouliLib without implementing HTTP, authentication, retries, or idempotency.
---

# Patchouli Agent

Use only the installed `patchouli` CLI or connected `patchouli-mcp` tools. Treat
both as presentation layers over the same typed client and operation journal.

## Keep the boundary

- Never implement or invoke raw HTTP, authorization headers, multipart bodies,
  retry loops, idempotency keys, or credential lifecycle operations.
- Never ask for or place a bearer credential in argv, MCP arguments, prompts,
  profiles, tracked configuration, output, or logs. Use an existing operating-
  system secret-store record or controlled `PATCHOULI_TOKEN` process injection.
- Use an existing non-secret profile. Do not invent deployment settings.
- Treat Section, Book, Page, Revision, cursor, and operation IDs as opaque.
  Copy them from validated output; do not parse them for time, order, identity,
  or authorization.
- Keep search queries, metadata, Source locators, and Markdown out of ordinary
  diagnostics. For CLI calls, supply sensitive values through the supported
  file or stdin options. For MCP, pass only the documented in-memory fields.

## Choose one interface

- Prefer connected MCP tools when the host already exposes them.
- Otherwise use `patchouli --output json ...` and parse only its stable stdout
  envelope. Treat stderr as diagnostics.
- Do not shell out to the CLI from an MCP session or create another client.

## Diagnose before content access

For CLI, run:

```text
patchouli --output json doctor
patchouli --output json capabilities
patchouli --output json whoami
```

For MCP, call `capabilities` and `whoami`; use CLI `doctor` only when the CLI is
also installed. Stop on failed compatibility, authentication, or missing
Section grants. Confirm the selected Section has `section:query` for search,
`page:read` for current or exact Revision fetches, and `archive:write` for
create or revise as the task requires. Do not broaden scope or substitute an
administrative identity.

## Discover opaque scope

List granted Sections, then list Books in the selected Section:

```text
patchouli --output json sections list
patchouli --output json books list --section <section-id>
```

The MCP equivalents are `sections_list` and `books_list`. Archive creation
requires an existing Book; never create one implicitly.

## Search and fetch an exact citation

Search only one explicit Section:

```text
patchouli --output json section search --section SECTION_ID --query-file QUERY_FILE
```

The MCP equivalent is `section_search` with `section_id`, `query`, and optional
`limit` or opaque `cursor`. Do not claim cross-Section search, raw full-text
syntax, or provider-specific semantic search.

Use the selected result's `section_id`, `page_id`, and `revision_number` to
fetch the immutable Revision:

```text
patchouli --output json page revision --section SECTION_ID --page PAGE_ID --revision REVISION_NUMBER
```

The MCP equivalent is `page_revision`. Return the validated exact citation with
all five fields: `section_id`, `page_id`, `revision_id`, `revision_number`, and
relative `href`. Do not replace it with a current-Page reference.

## Create an archive explicitly

Use `archive create` or MCP `archive_create`; never infer an upsert. CLI metadata
must be a UTF-8 JSON object shaped like this synthetic example:

```json
{
  "title": "Synthetic archive",
  "occurred_at": "2026-08-11T09:15:00Z",
  "source": {"kind": "conversation"}
}
```

Invoke the CLI with separate metadata and complete Markdown inputs:

```text
patchouli --output json archive create --section SECTION_ID --book BOOK_ID --metadata-file METADATA_FILE --content-file MARKDOWN_FILE
```

For MCP `archive_create`, pass `section_id`, `book_id`, `title`, `occurred_at`,
`source_kind`, optional `source_locator`, and complete `content`. Never pass a
credential, endpoint, local filename, journal location, or idempotency key.

The client durably prepares its permission-restricted operation journal before
the mutation. Preserve the returned non-secret `operation_id`. After an
uncertain failure, replay only the identical CLI command plus
`--operation-id OPERATION_ID`, or the identical MCP tool input plus
`operation_id`. Omitting the operation ID starts a new operation. Any route,
metadata, exact content bytes, caller, or profile-origin difference must start a
new operation. Do not claim cross-device recovery or deduplicate another key by
title, Source locator, timestamp, or content. If the result is lost and no
operation ID reached the caller, stop: the shipped interfaces cannot discover
journal entries, and starting another write may create a duplicate.

## Revise an archive explicitly

First fetch the current Page and its strong ETag:

```text
patchouli --output json page current --section SECTION_ID --page PAGE_ID
```

Then append a complete Revision, never a patch:

```text
patchouli --output json archive revise --section SECTION_ID --page PAGE_ID --if-match STRONG_ETAG --metadata-file METADATA_FILE --content-file MARKDOWN_FILE
```

The MCP equivalents are `page_current` and `archive_revise`; pass `if_match`,
`source_kind`, optional `source_locator`, and complete `content`. Preserve the
ETag exactly, including its strong quoting. Confirm the fetched Page is an
archive before requesting an archive Revision.

On a known 412 or 428 response, the Revision was not applied. Do not replay that
failed operation or silently retry with changed inputs. Fetch current state,
review it, and deliberately start a new operation with the new strong ETag.
Reserve exact replay for an uncertain outcome when the original operation ID
and every original argument remain available. Report the resulting exact
citation.

## Report safely

Return operation outcome, safe request ID when present, non-secret operation
ID for recoverable writes, and exact citation. Never echo query text, metadata,
content, Source locator, credential material, idempotency key, or deployment
details unless the user explicitly requested the non-secret content itself.
