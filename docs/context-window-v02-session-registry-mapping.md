# Context Window v0.2 — Session Registry + Message-Event Mapping

Flag-off data layer only. Not wired into Runtime, Preview, Forge, gateway, or deploy.

## Scope

| Module | Path | Responsibility |
|--------|------|----------------|
| Schema | `chat/daily_context.py` (`_ensure_session_registry_mapping_schema`) | Idempotent DDL for two tables + indexes |
| Registry | `chat/session_registry.py` | Explicit session/path registration, conflict checks, scan_offset CAS |
| Mapping | `chat/claude_event_mapping.py` | Offline byte-range mapping pass + canonical user query |
| Reader | `chat/claude_transcript_reader.py` | Minimal `read_transcript_range` (classification unchanged) |

## Tables

### `context_claude_sessions`

- PK `(context_id, resident_generation)`
- UNIQUE `claude_session_id`
- `transcript_path` frozen from explicit `session_id` + `cwd` derivation (`tools.cc_jsonl_usage.session_jsonl_path`)
- `scan_offset` / `scan_status` / `scan_error_code` / `last_mapped_message_id` for restart-safe replay
- No mtime / latest-file / directory scan

### `chat_message_claude_events`

- PK `event_uuid` (global unique)
- Partial UNIQUE `(message_id) WHERE role='user'` — canonical user 1:1 only
- **No** `UNIQUE(message_id, role)` — assistant/tool one-to-many allowed
- Content SoT remains `chat_messages.content` (no duplicated canonical body)

## Public API

- `register_context_claude_session(...)` — idempotent same-key same-value; conflict otherwise
- `get_context_claude_session(...)`
- `cas_advance_scan_offset(...)` — CAS on `scan_offset`
- `run_mapping_pass(MappingPassRequest)` — single offline pass; no threads
- `get_user_canonical_by_event_uuid(event_uuids)` → `{event_uuid: content}`

## Mapping pass contract (summary)

1. Verify Registry identity (`context_epoch`, `resident_generation`, `chat_id`, session/path).
2. Require `scan_offset == expected_start_offset`.
3. Read-only `[expected_start_offset, observed_end_offset)` via Reader.
4. Exactly one main-chain `CANDIDATE_USER` in range; else fail closed.
5. Map user → `user_message_id`; main-chain assistant/tool → `assistant_message_id`.
6. One DB transaction: mappings + CAS advance; conflict → rollback, offset unchanged, `scan_status=BLOCKED`.
7. File size `<` registered/start offset → truncated, fail closed (no rewind to head).

## Explicit non-goals

- no Runtime / `prepare_daily_turn` / assistant persist wiring
- no Preview / Forge / Capacity Swap
- no flag changes / deploy / model calls / live Claude processes
- no third task table, queue, worker, or cron
