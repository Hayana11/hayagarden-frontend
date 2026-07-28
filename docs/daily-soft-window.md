# P-CONTEXT-DAILY-SOFT-WINDOW-BE-R0

## Status

Backend-only **Draft**. Default **off** (`DAILY_SOFT_WINDOW_ENABLED=0`).
Does **not** generate day handoffs via LLM, does not change formal chat defaults,
does not enable itself on deploy.

**Note:** process boot still runs idempotent `ensure_schema()` so the three new
tables (and optional `chat_messages.source_kind`) may be created even while the
flag stays `0`. That is DDL only — no runtime Soft Window behavior.

## Product semantics

- Same UI chat window; no new visible session.
- Backend `context_epoch` rolls at Asia/Shanghai **04:00** (half-open `[04:00, next 04:00)`).
- R0 message queries treat `chat_messages` as a **single global formal chat**
  (`chat_id` on `daily_contexts` is reserved for future mapping).

## Schema

- `daily_contexts` — `UNIQUE(chat_id, local_day)`
- `daily_carryover_messages` — exact selected `message_id`s
- `day_handoffs` — formal handoff storage
- `chat_messages.source_kind` — authoritative origin (`chat` / `wake` / `workspace_job` / …)

## State machine

```text
ABSENT → COMPACTING → PROVISIONAL → FINALIZED
ABSENT → PROVISIONAL                    # R0 skip-compaction open path
PROVISIONAL → COMPACTING                # late / retry compaction
FAILED_RETRYABLE → COMPACTING|PROVISIONAL
```

`get_or_create_daily_context()` inserts **ABSENT**, then with default
`skip_compaction=True` immediately promotes **ABSENT → PROVISIONAL** in the
same transaction. Pass `skip_compaction=False` to leave ABSENT for
`acquire_compaction_lease()`.

## Formal message predicate

`is_formal_chat_message()` (shared by carryover + current-day history):

- author user/assistant only
- `source_kind` must be `chat` (or legacy empty); non-chat kinds excluded
- formal chat rows with non-empty `tool_calls` are kept (including `ws_job` tool use)
- legacy pre-migration workspace completions: `ws_job` + top-level `job` + `args.action=status`
- reject SAVE markers
- allow image-only rows as `[image]`

Wake executor and workspace job completion writes set `source_kind` explicitly.

## Epoch fence

`commit_if_epoch_current(token, operations=[(sql, params), ...])` runs inside one
`BEGIN IMMEDIATE` transaction: verify active epoch/generation (`is_backfill=0`) →
execute validated SQL operations → re-verify → commit/rollback.

Forbidden SQL includes transaction control (`BEGIN`/`END`/`COMMIT`/…), multi-statement
batches, and `PRAGMA`. Backfill tokens (`is_backfill=1`) never pass fence checks.

## Resident history cursor

`build_daily_window_context()` is read-only for the cursor. It returns
`cursor_before`, `replayed_through_message_id`, and `cursor_advance_required` in
the manifest. After the assistant message is persisted and provider success is
confirmed, call `advance_resident_history_cursor(context_id, resident_generation,
processed_through_message_id, expected_cursor=None)` — cursor advances
monotonically only.

## APIs (flag-gated; db_path injected at blueprint construction)

- `GET /api/daily-context/current`
- `GET /api/daily-context/carryover-candidates` — `carryover_unit=round`, authoritative `rounds[]`
- `POST /api/daily-context/select-carryover` `{ "count": 0|3|5|10 }` — count is **round** count

`daily_contexts.carryover_count` stores the number of selected **rounds** (not messages).
`carryover_requested_count` stores the user-selected tier (`0|3|5|10`) for idempotent
retries when fewer rounds are available than requested.

Provider manifest reports both `carryover_round_count` and `carryover_message_count`.

HTTP routes resolve the current day via `resolve_current_daily_context_for_api()`,
which applies the cross-day provider lease fence before creating a new context.
`DeferredError` returns HTTP 423 with `code: rollover_deferred`.

### FE wiring note (post-#146)

Backend round shape (`message_ids` + `messages[]`) differs from the merged FE preview
shape (`user` + `assistants`). Formal chat wiring requires a canonical adapter and
separate handling of `requested_round_count` vs `selected_round_count`.

Auto zero-carryover after the day's first formal user message is already
committed: `finalize_zero_for_first_user_message(context_id, user_message_id)`.

## Explicit non-goals (this PR)

- Auto LLM handoff generation / Wake rollover wiring / morning greeting / frontend
- Enabling `DAILY_SOFT_WINDOW_ENABLED`
- Changing Clean Window / Daily Candidate Shadow behavior

## Frontend companion

See `docs/daily-soft-window-fe.md` for **P-CONTEXT-DAILY-SOFT-WINDOW-FE-R0**.

Current FE Draft is **preview-only** (`/dash/daily-soft-window` mock + round
semantics). Formal `/dash/chat` Soft Window wiring stays off until backend
**R1.1 round contract**.
