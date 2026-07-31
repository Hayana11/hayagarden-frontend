# Context Window v0.2 — Preview / dry-run (R1)

Read-only candidate preview. Flag-off. No Forge publish, DB bind, staged
resident, first-turn commit, Capacity Swap, Canary, or real switch.

## Scope

| Module | Change |
|--------|--------|
| `chat/context_window_preview.py` | Read-only Preview core |
| `context_window_routes.py` | `POST /api/context-window/preview` |
| `tests/test_context_window_preview.py` | Focused Preview cases |
| This doc | Contract notes |

Frozen (not modified): formal switch state machine in `chat/context_window.py`,
`gateway.py`, `daily_runtime.py`, `cc_resident.py`, `daily_context.py`,
`session_registry.py`, `claude_event_mapping.py`, Transcript Reader / Model /
Transform / Validator, Forge publish, frontend, deploy, flags, Actions.

## Endpoint

`POST /api/context-window/preview`

Same Bearer auth + `DAILY_SOFT_WINDOW_ENABLED` disabled gate as other
context-window routes. No Preview-specific flag.

Request fields:

- `chat_id` (optional, default `DEFAULT_CHAT_ID`)
- `source_context_id` (positive int)
- `source_context_epoch` (positive int)
- `count` (`parse_strict_json_carryover_count`)
- `preview_id` (required UUID; not persisted)
- `thinking_policy` (optional; `keep` \| `drop`; default `drop`)

## Statuses

| Status | Meaning |
|--------|---------|
| `READY` | ≥1 migratable round; Transform + Validator pass; prefix + fresh DB recheck ok |
| `NATIVE_COLD` | `count=0`; empty candidate; no ordinary empty-events Validator |
| `BLOCKED` | Dry-run completed; candidate unsafe (`ok=true`, HTTP 200) |

## Read-only DB

`_open_preview_db_readonly`:

- SQLite URI `mode=ro`, `uri=True`
- `row_factory=sqlite3.Row`
- `PRAGMA query_only=ON`
- `BEGIN` only (never `BEGIN IMMEDIATE`)
- Never `ensure_schema`, INSERT / UPDATE / DELETE / REPLACE / DDL

`_close_preview_readonly` best-effort ROLLBACK + close (idempotent).

## Flow (count > 0)

1. Snapshot source identity on a read-only connection (canonical open window,
   no active lease / switch intent); select formal app rounds.
2. Registry gate for `(context_id, resident_generation)`: READY, non-empty
   session/path.
3. **Safe** parse of `scan_offset` (`int(...)` never runs unchecked). Invalid /
   non-positive → content BLOCKED `PREVIEW_REGISTRY_SCAN_OFFSET_INVALID` with
   `source.scan_offset=None` (HTTP 200). Candidate UUID seed uses `0`.
4. `prefix_before = _snapshot_transcript_prefix(path, scan_offset)` — exact
   `[0, scan_offset)` SHA-256; no symlink follow; no read past offset.
5. `read_transcript_range(path, 0, scan_offset)`.
6. Canonical Mapping + selection consistency + Transform + Validator.
7. `prefix_after` must match `prefix_before` (`end_offset` + `sha256`); else
   `PREVIEW_SOURCE_CHANGED`. Bytes after `scan_offset` are ignored (EOF append ok).
8. Close first connection. Open a **fresh** read-only connection and re-check
   identity / Registry / selected message IDs; any drift → `PREVIEW_SOURCE_CHANGED`.
9. Both prefix + fresh DB recheck must pass before `READY`.

## count = 0

Still validates source identity / busy / switch intent on the first connection,
then **closes** it and opens a second fresh read-only connection for
`_recheck_after_read`. Skips Registry, JSONL, Mapping, Transform, and ordinary
Validator. Returns `NATIVE_COLD` with `proof_kind='native_cold_contract'` and
empty SHA-256. Identity drift → `PREVIEW_SOURCE_CHANGED`.

## Candidate session id

Stable UUID v5 over `preview_id` + source identity + `scan_offset` +
`selected_round_count` + thinking policy. Always
`candidate_session_reserved=false`. Never creates JSONL or Registry rows.

## Response hygiene

Never expose `transcript_path`, absolute cwd, Claude home, full transformed
events, or raw exception text. `dropped` returns counts only.

BLOCKED responses:

- no public `error_detail`
- `validation.errors` contains only the safe Preview `error_code`
- internal detail may be logged server-side only

Unexpected route failures return HTTP 500 with fixed
`{ok:false, error:'preview failed', code:'preview_internal_error'}` — never
`str(exc)`.
