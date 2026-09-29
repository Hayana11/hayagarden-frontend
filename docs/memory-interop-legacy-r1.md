# MEMORY-INTEROP R1 — Legacy `posts` adapter

## Status and scope

R1 is an offline, read-only shadow adapter. It proves that the current
legacy `posts` memory store can be translated losslessly into MEMORY-INTEROP
v0.1 contracts without changing legacy behavior.

It is not itself a production write or search replacement.
It does not dual-write. It does not deploy. It does not open or create a
Memory Kernel database.

R2A may invoke this translator from a default-off Live Shadow worker after an
authoritative legacy result is frozen. That worker is observation-only; see
`docs/memory-interop-live-shadow-r2a.md`. The R1 module remains a read-only
translator. No Kernel committer lives here.

## Translation shape

```text
Legacy posts
    ↓
Legacy Interop Adapter  (legacy.posts.v1)
    ├→ Evidence-only SubmissionEnvelope   (shadow ingest)
    └→ ContextBundle                      (shadow retrieve)
```

The adapter translates. It does not judge truth.

## Adapter identity

- `adapter_id`: `legacy.posts.v1`
- `protocol_version`: `0.1`
- operations: `ingest`, `retrieve`, `context_contribute`
- `readable`: true
- `writable`: false
- capability references: `memory.search`, `memory.write`

`memory.write` support means the adapter can translate an **already-created**
legacy row into an Interop submission for shadow observation. It does not
insert the row. Persistence remains `tools.memory_tool.save_memory` through
`tools.memory_write_adapter.write_memory`.

## Evidence-only ingest

Each legacy row becomes one `Evidence` payload inside a `SubmissionEnvelope`.

- content is the exact stored `posts.content` string
- `source_ref` is `legacy://posts/<id>`
- `source_type` is `legacy.posts`
- provenance keeps present legacy fields: table, row id, type, author,
  layer, tags, pinned, resolved, importance, and the raw `created_at` value
- candidate State / Delta lists are empty
- envelope provenance marks the payload as `legacy_shadow_translation`

No `CandidateState`. No `CandidateDelta`. R1 does not infer State or Delta
from arbitrary legacy text. Semantic interpretation into accepted Kernel
State happens later, if at all, through Processor/Review policy. This
adapter stops at Evidence.

## Derived by default

Legacy `posts` rows do not reliably distinguish:

- a raw user utterance
- a model summary or dream
- imported memory
- other derived text

R1 therefore **does not** silently promote legacy content to raw factual
source. Every translated Evidence uses:

```text
origin_kind = "derived"
```

That means "the legacy system stored this text". It does not mean the text
is objectively true, and it does not mean the user directly said it.

## Timestamp safety

Legacy `created_at` values are often timezone-naive SQLite datetimes.
This adapter does not guess a timezone and does not apply a silent `+08`
conversion.

- the raw `created_at` string is preserved in provenance when present
- `Evidence.observed_at` is an explicit caller-supplied timezone-bearing
  timestamp
- `Evidence.occurred_at` is populated only when the source timestamp itself
  is already timezone-bearing and valid under Kernel rules
- otherwise `occurred_at` remains `None`

## Search remains legacy-authoritative

Shadow retrieval reuses `tools.product_handlers.search_memory_posts` on a
read-only SQLite connection. It does not reimplement or improve ranking.

Preserved exactly:

- result ordering (`id DESC`)
- limit behavior
- DIARY inclusion
- resolved-row inclusion
- empty-query behavior (latest rows)
- Unicode substring matching

Each legacy row becomes one `ContextItem`:

- `content` is the exact legacy content
- `source_adapter_id` is `legacy.posts.v1`
- `source_refs` includes `legacy://posts/<id>`
- `kernel_refs` is empty
- `confidence` and `epistemic_status` are omitted unless a later source
  actually has them; R1 does not invent either
- item order is the original legacy order
- `request_id` / `turn_id` propagate from `InteropRequestContext`

The `ContextBundle` contributor list contains `legacy.posts.v1`. There is
no reranking and no extra filtering.

## Read-only SQLite

Database helpers open the explicit fixture path with SQLite URI `mode=ro`
and `PRAGMA query_only = ON`. Retrieval must not change `recall_count` or
`last_recalled_at`, and must not insert, update, or delete rows.

Tests use temporary fixture databases only. They do not open
`/opt/frontend/memories.db`.

## Offline shadow harness

`tools.memory_interop_legacy.shadow_compare_legacy_search` and the optional
CLI `tools/memory_interop_legacy_shadow.py` compare authoritative legacy
search rows to the adapter `ContextBundle` (same ids, order, content, and
count).

They are invoked only by explicit tests or a manual command. They are not
started by production, systemd, cron, HTTP, or MCP.

## What R1 is not

- not a production Interop plugin registry
- not a live Shadow hook on `memory.write` / `memory.search`
- not a Kernel committer
- not an Ombre change
- not a migration of legacy `posts` into Kernel tables
