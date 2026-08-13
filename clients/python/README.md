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

The typed wire accepts a call-scoped bearer token. The included CLI adds a
non-secret profile, safe credential-input abstraction, deterministic output and
exit codes, and a permission-restricted mutation journal. The optional MCP
adapter exposes the same client and journal semantics over local stdio. Agent
Skill and live-network end-to-end support remain later layers.

## CLI

Install the package and run `patchouli --help`. Profiles are versioned TOML and
contain only an HTTPS origin and compatibility version:

```toml
version = 1

[profiles.default]
endpoint = "https://library.example.invalid"
api_version = "v1"
```

The default profile path follows `%APPDATA%` on Windows and
`$XDG_CONFIG_HOME` (or `~/.config`) on POSIX. `PATCHOULI_CONFIG_FILE`,
`PATCHOULI_PROFILE`, `PATCHOULI_ENDPOINT`, and `PATCHOULI_API_VERSION` provide
non-secret process-local overrides. A config containing unknown fields such as
a token is rejected. Config files are read once through verified, non-reparse
handles; the file and its existing parent chain must have trusted ownership and
permissions before an endpoint can be used with a bearer credential.

Caller credentials never have a command-line option. Resolution order is:

1. `--token-stdin`, when explicitly selected for a command that has no other
   stdin input;
2. the injected `PATCHOULI_TOKEN` environment variable; and
3. the optional operating-system keyring record for service
   `patchouli-client` and account equal to the profile name.

Install the optional keyring adapter with `patchouli-client[secret-store]`.
Without it, the CLI is an explicit environment/stdin-only client. It never
writes a plaintext token file.

The command surface is:

```text
patchouli doctor
patchouli capabilities
patchouli whoami
patchouli sections list [--limit N] [--cursor CURSOR]
patchouli books list --section SECTION [--limit N] [--cursor CURSOR]
patchouli pages list --section SECTION [--limit N] [--cursor CURSOR]
patchouli section search --section SECTION (--query-file FILE | --query-stdin) \
  [--limit N] [--cursor CURSOR]
patchouli page current --section SECTION --page PAGE
patchouli page revision --section SECTION --page PAGE --revision NUMBER
patchouli archive create --section SECTION --book BOOK \
  (--metadata-file FILE | --metadata-stdin) \
  (--content-file FILE | --content-stdin)
patchouli archive revise --section SECTION --page PAGE --if-match '"strong-etag"' \
  (--metadata-file FILE | --metadata-stdin) \
  (--content-file FILE | --content-stdin)
```

Search query, archive metadata, and Markdown content have only file or stdin
forms. At most one value, including the token, may own stdin. File inputs must
stay under the current directory or `--input-root`/`PATCHOULI_INPUT_ROOT`; the
reader rejects symlinks, reparse points, non-regular files, path escapes, NUL,
invalid UTF-8 where applicable, and oversized inputs. Local filenames never
enter request metadata, idempotency fingerprints, output, or diagnostics.
Each path component is traversed relative to an already verified directory
handle, so a concurrent pathname replacement cannot redirect an open read.
The production entry point reads stdin in binary mode so Markdown body bytes,
including line endings, are not normalized by the terminal text layer.

`--output json` writes a stable success envelope to stdout. Human output also
uses stdout only for returned data. All diagnostics use stderr; error output is
redacted and never renders content, metadata, a bearer token, or an idempotency
key.

Before the first create or revise request, the CLI writes a generated
idempotency key to a permission-restricted operation journal. The success or
failure output includes a non-secret operation UUID. Re-run the same command
with `--operation-id UUID` to reuse the key; any route, metadata, content, or
`If-Match` difference, including a changed profile origin, is rejected locally.
Every attempt first resolves `whoami`; the stable caller ID is journaled and
fingerprinted, so credential rotation for the same caller is allowed while a
different caller is rejected before mutation. Credential IDs and bearer values
are never journaled. Journal writes sync the file and containing directory
around creation and replacement. POSIX modes and protected Windows DACLs keep
state current-user-only, and journal access remains bound to verified handles.
A 412 or 428 does not apply a
Revision: exact replay keeps the original journal and arguments, while a new
ETag requires a new operation. Loss of the journal provides no cross-device
deduplication guarantee.

Exit statuses are deterministic:

| Status | Category |
| ---: | --- |
| 0 | success |
| 2 | command usage |
| 3 | profile/configuration |
| 4 | caller credential input/store |
| 5 | operation journal safety or mismatch |
| 10 | authentication |
| 11 | insufficient scope |
| 12 | missing or hidden resource |
| 13 | idempotency conflict |
| 14 | 412/428 Revision precondition |
| 15 | local or server validation, including 413/415/422 |
| 16 | rate limit or temporary service failure |
| 17 | bounded transport failure |
| 18 | outer edge gate or non-conforming upstream error |
| 19 | wire/application protocol failure |
| 70 | fail-closed internal error |
| 130 | interrupted operation |

## MCP

Install `patchouli-client[mcp]` and configure the same non-secret profile and
environment or operating-system keyring credential used by the CLI. Then point
an MCP host at the `patchouli-mcp` executable. It accepts no command-line
arguments and uses stdio exclusively; it never opens a TCP listener.

The adapter exposes these tools:

```text
capabilities
whoami
sections_list
books_list
section_search
page_current
page_revision
archive_create
archive_revise
```

Tools never accept credentials, endpoints, journal paths, idempotency keys, or
local file paths. Search query and Markdown content are bounded in-memory JSON
strings. Archive creation and revision are distinct tools; revision requires a
strong `if_match`. A returned non-secret `operation_id` may be supplied to the
same write tool for an exact replay. The caller-independent journal binding is
checked locally before `whoami`, and the stable caller is checked before the
mutation. Raw operation keys remain private journal state.

One `PatchouliClient` is shared for the stdio session and closed at session end.
The adapter delegates HTTP, authentication headers, retries, multipart encoding,
response validation, and write orchestration to the merged client/application
layers. Tool failures return stable redacted MCP errors without response bodies,
queries, content, source locators, endpoints, or low-level exception details.

The MCP extra uses the official MIT-licensed Python SDK stable v1 line, bounded
below v2. All stdout bytes belong to MCP framing; sanitized startup diagnostics
use stderr.

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
