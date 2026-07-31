# Context Window v0.2 — Preview / dry-run (R1)

Read-only candidate preview. Flag-off. No Forge publish, DB bind, staged
resident, first-turn commit, Capacity Swap, Canary, or real switch.

## Scope

| Module | Change |
|--------|--------|
| `chat/context_window_preview.py` | Read-only Preview core |
| `context_window_routes.py` | `POST /api/context-window/preview` |
| `tests/test_context_window_preview.py` | Four focused cases |
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
| `READY` | ≥1 migratable round; Transform + Validator pass |
| `NATIVE_COLD` | `count=0`; empty candidate; no ordinary empty-events Validator |
| `BLOCKED` | Dry-run completed; candidate unsafe (`ok=true`, HTTP 200) |

## Read-only DB

`_open_preview_db_readonly`:

- SQLite URI `mode=ro`, `uri=True`
- `row_factory=sqlite3.Row`
- `PRAGMA query_only=ON`
- `BEGIN` only (never `BEGIN IMMEDIATE`)
- Never `ensure_schema`, INSERT / UPDATE / DELETE / REPLACE / DDL

## Flow (count > 0)

1. Snapshot source identity on a read-only connection (canonical open window,
   no active lease / switch intent).
2. Select formal app rounds via existing `_collect_context_formal_messages` /
   `_select_rounds_locked`.
3. Registry gate for `(context_id, resident_generation)`: READY, non-empty
   session/path, `scan_offset > 0`, file size ≥ offset.
4. `read_transcript_range(path, 0, scan_offset)` — never past offset / EOF scan.
5. Build `user_canonical_by_event_uuid` from existing Mapping + `chat_messages`
   (same rules as `get_user_canonical_by_event_uuid`; read-only conn).
6. Reject cross-session / cross-generation selection:
   `PREVIEW_SELECTED_ROUNDS_SPAN_SESSIONS` (no auto-shrink).
7. Require app user UUID order == Transcript eligible-tail UUID order.
8. In-memory `transform_transcript` + `validate_transcript_events`.
9. Re-open a fresh read-only connection and re-check identity / Registry /
   selected message IDs; any drift → `PREVIEW_SOURCE_CHANGED`.

## count = 0

Still validates source identity / busy / switch intent. Skips Registry, JSONL,
Mapping, Transform, and ordinary Validator. Returns `NATIVE_COLD` with
`proof_kind='native_cold_contract'` and empty SHA-256.

## Candidate session id

Stable UUID v5 over `preview_id` + source identity + `scan_offset` +
`selected_round_count` + thinking policy. Always
`candidate_session_reserved=false`. Never creates JSONL or Registry rows.

## Response hygiene

Never expose `transcript_path`, absolute cwd, Claude home, or full transformed
events. `dropped` returns counts only.
