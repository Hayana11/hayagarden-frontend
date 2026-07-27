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
- `source_kind` must be `chat` (or legacy empty)
- reject non-empty `tool_calls`
- reject SAVE markers
- allow image-only rows as `[image]`

Wake executor and workspace job completion writes set `source_kind` explicitly.

## Epoch fence

`commit_if_epoch_current(token, writer(conn))` runs inside one `BEGIN IMMEDIATE`
transaction: verify epoch/generation → `writer(conn)` → re-verify → commit/rollback.

## APIs (flag-gated; db_path injected at blueprint construction)

- `GET /api/daily-context/current`
- `GET /api/daily-context/carryover-candidates`
- `POST /api/daily-context/select-carryover` `{ "count": 0|3|5|10 }`

Auto zero-carryover after the day's first formal user message is already
committed: `finalize_zero_for_first_user_message(context_id, user_message_id)`.

## Explicit non-goals (this PR)

- Auto LLM handoff generation / Wake rollover wiring / morning greeting / frontend
- Enabling `DAILY_SOFT_WINDOW_ENABLED`
- Changing Clean Window / Daily Candidate Shadow behavior
