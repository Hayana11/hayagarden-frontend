# Context Window v0.2 — First-turn commit freeze

Independent first-turn unit after Target prepare (`intent.status=ready`,
`manual_staged` target + Registry). Closes source and promotes the **same**
target only on the first non-empty assistant `text` delta.

Flag-off. No Gateway route, no frontend, no production calls.

## Scope

| Module | Change |
|--------|--------|
| `chat/context_window_first_turn.py` | First-turn orchestrator |
| `chat/daily_context.py` | `context_switch_intents` first-turn nullable fields + unique indexes |
| `tests/test_context_window_first_turn.py` | Three focused paths |
| This doc | Frozen contract |

Frozen: `gateway.py`, `context_window_bff.py`, frontend, flags, Capacity Swap,
Canary, last-good/fallback, production Claude live tests.

## Single handoff point

Not on `SessionStart`, stdin accept, `think`/`meta`/`status`, empty text, or
full `result`. Order:

```text
candidate receives first real user turn
→ capture first non-empty text delta (hold)
→ sole BEGIN IMMEDIATE DB transaction
→ formal resident install
→ intent COMMITTED
→ close old resident
→ release held first text delta
```

## Message ownership

One `chat_messages` row; `daily_message_contexts` maps it to **target**
identity from the first claim. Never source→target UPDATE or duplicate user
rows. `message_id` PK enforces this.

## Intent status reuse

```text
READY → COMMITTING → HANDOFF_PENDING → COMMITTED
```

`COMMITTED` means house handed over, not full round complete. Assistant persist,
cursor CAS, and mapping remain later steps (`complete_first_turn_round`).

## DB transaction (on first non-empty text)

Re-read and CAS:

- intent: `COMMITTING`, matching `request_id`, `first_turn_request_id`,
  `first_user_message_id`, `target_context_id`, `target_session_id`
- source: open formal canonical, frozen identity
- target: `manual_staged`, `provisional`, `resident_generation=1`,
  `switch_request_id`, `claude_session_id`
- Registry: identity + `scan_offset=first_turn_start_offset`, `scan_status=READY`
- user message + target lease (`lease_owner=first_turn_request_id`)

Writes only:

1. close source (`close_reason=manual`, `version+1`)
2. promote target `window_mode=manual` (same id/epoch/generation/session)
3. intent `HANDOFF_PENDING`, `first_delta_committed_at`, clear `first_turn_error_code`

No second target insert, no process kill in txn, no assistant/cursor/last-good.

## Post-DB process barrier (handoff lock)

1. `install_target_resident_after_swap` / binding helpers from `daily_runtime`
2. owner + forged watermark cursor + `LocalResidentBinding`
3. `mark_intent_committed`
4. close old resident
5. release buffered first delta

## Failure boundaries

**Clean (pre-commit):** no stdin and/or no JSONL growth past frozen offset →
source canonical, target `manual_staged`, intent `READY`, same
`first_user_message_id`, retry allowed.

**Dirty (pre-commit):** stdin sent + JSONL grew, no DB commit →
`first_turn_error_code`, `orphan_jsonl_state=precommit_dirty`, stop auto retry
on same candidate session.

**Post-DB swap fail:** source stays closed, target formal, `HANDOFF_PENDING`,
delta still held → `recover_first_turn_handoff_pending` resumes same session,
no resend user.

## Frozen intent fields

Nullable on `context_switch_intents`:

`first_turn_request_id`, `first_user_message_id`, `first_turn_started_at`,
`first_turn_start_offset`, `first_delta_committed_at`, `first_turn_end_offset`,
`first_assistant_message_id`, `first_turn_completed_at`, `first_turn_error_code`

Unique partial indexes on non-null `first_turn_request_id` and
`first_user_message_id`.

`first_turn_completed_at` only after: full provider result path (caller),
assistant idempotent persist, cursor CAS, lease release.

## Tests (three paths)

1. Happy: user→target, buffer, CAS+handoff before release, assistant+cursor
2. Clean failure: source/target unchanged, retry same message id
3. DB commit + swap fail: `HANDOFF_PENDING` recovery → `COMMITTED`, no resend
