# MEMORY-INTEROP R2A — Live Shadow wiring

## Status

R2A adds production **call-site wiring** for a Live Shadow observer behind a
default-off gate. It does not replace legacy memory, does not dual-write, does
not commit Kernel records, and must not be enabled or deployed by this change.

Receipts are observations only. They are never authority.

## Coverage matrix

| Surface | Formal capability | R2A Live Shadow |
|---|---|---|
| Claude Code capability proxy | `memory.write` | wired, default-off |
| Internal MCP | `memory.write` | wired, default-off |
| API Relay gateway | formal `memory_write` | wired, default-off |
| Claude Code capability proxy | `memory.search` | wired, default-off |
| Internal MCP | `memory.search` | wired, default-off |
| Home MCP `GET /api/posts?search=` | informal search | **not covered** |
| Legacy gateway `run_tool search_memories` | `memory_tool.search_memories` | **not covered** |
| Legacy `save_memory` tool | persistence owner | **not covered** |
| `[[SAVE:...]]` marker persistence | write | **not covered** |
| Automatic recall / context injection | read | **not covered** |
| Ombre | separate store | **not covered** |
| Wake legacy recall | read | **not covered** |

R2A does **not** claim 100% `memory.search` coverage. Home MCP and the legacy
gateway search tool remain on their current production paths with no Shadow
dispatch.

## Gate

`MEMORY_INTEROP_SHADOW_ENABLED`

- unset / `0` / `false` / any value other than exact `1` → completely disabled
- exact `1` → enabled

Default is **OFF**.

When OFF:

- no shadow process is spawned
- no shadow DB is opened
- no shadow file is created
- no Interop translation is attempted
- no additional production DB read is performed
- authoritative return shape and text stay unchanged

`MEMORY_INTEROP_SHADOW_DB_PATH` must point at a **separate** SQLite receipt
file when the gate is on. It must never be:

- `memories.db`
- a Memory Kernel DB
- an Ombre DB
- a continuity DB
- the same file as the authoritative posts DB

If the gate is on but the path is missing or forbidden, Shadow fails open and
the authoritative request is untouched.

## Code path

```text
authoritative legacy operation
        ↓
authoritative result frozen
        ├────────→ caller  (original status / posts / tool text)
        └────────→ best-effort Shadow observation
                      ↓
                   detached worker
                      ↓
                   R1 translation + receipt DB
```

Authoritative work always happens first. Shadow is attempted only after the
result is frozen. The caller always receives that original result.

## Fail-open

Any Shadow failure is local to observation:

- import failure
- spawn failure
- translation failure
- receipt DB missing, locked, or invalid
- malformed legacy row
- worker crash
- timeout
- permission error

Shadow must not:

- turn a successful `memory.search` into failure
- turn a successful `memory.write` into failure
- modify returned posts
- modify returned write status/id
- retry the authoritative write
- duplicate the authoritative write

## Async dispatch

The production adapter process does not open or write the receipt database
before returning.

`tools/memory_interop_shadow.py` is a gate + event builder + `subprocess.Popen`
dispatcher (`shell=False`, stdin pipe, stdout/stderr discarded, `close_fds`,
`start_new_session`, no wait). It does not import Kernel or the R1 adapter and
does not use SQLite.

`tools/memory_interop_shadow_worker.py` runs out of band. It may:

- open legacy posts **read-only**
- call `legacy_post_to_submission_envelope` / `legacy_rows_to_context_bundle`
- write receipts to `MEMORY_INTEROP_SHADOW_DB_PATH`

It must not write posts, Kernel, or Ombre, call a provider, or change the
caller-visible result.

## Write observation

Dispatched only after formal `memory.write` returns `status == CREATED` and a
stable row id. The worker re-reads that exact row, verifies the content
SHA-256, translates through `legacy.posts.v1`, and stores a receipt.

No review. No `AcceptedReview`. No Kernel committer. No Kernel DB.

## Search observation

After the authoritative `memory.search` result is frozen, the observer captures
ordered legacy ids and content hashes (not a rerank). The worker fetches those
exact ids read-only and builds a `ContextBundle` in that same order.

If a row disappears or changes before the worker reads it, the receipt records
`missing_row` / `hash_mismatch` / `source_changed`. That is a Shadow
observation failure, not a production failure.

## Receipts

Receipts live only in the dedicated shadow DB. They store hashes and source
refs, not raw query text, raw memory content, transcripts, prompts, tokens, or
lease secrets.

Statuses include at least: `ok`, `source_changed`, `missing_row`,
`hash_mismatch`, `translation_failed`, `receipt_failed`. Duplicate exact events
replay idempotently. The same event key with a different payload is a Shadow
`conflict`; it never retries `memory.write`.

## Authority

UH-A0 leases remain the capability authority. Shadow `source_surface` /
`shadow_turn_id` values are observation metadata only:

- capability proxy → `claude_code`
- internal MCP → `internal_mcp`
- API Relay formal write → `api_relay`

They cannot grant a capability.

## What R2A is not

- not an enabled production feature
- not a Kernel writer
- not an Ombre change
- not a memory migration
- not a replacement for search or write
- not Home MCP search wiring
- not legacy gateway `search_memories` wiring
