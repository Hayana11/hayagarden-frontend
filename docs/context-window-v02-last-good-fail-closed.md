# Context Window v0.2 — Last-good / Core Fail-closed / Structural Canary

Flag-off. Draft only. No deploy. No live Claude. No production DB.

## Status labels (explicit non-goals)

```text
STRUCTURAL_CANARY_ONLY
OWNER_CANARY_NOT_IMPLEMENTED
NIGHTLY_SCHEDULER_NOT_IMPLEMENTED
```

Also not implemented in this PR:

```text
cold fallback runtime
cross-process first-delta automatic recovery
  (NO_AUTOMATIC_CROSS_PROCESS_FIRST_DELTA_RECOVERY)
frontend copy changes
DAILY_SOFT_WINDOW_ENABLED default / flag-on
```

Do not treat unittest structural coverage as Owner or Nightly Canary.

## Allowlist

| File | Role |
|------|------|
| `chat/daily_context.py` | Five nullable last-good columns on `context_switch_intents` |
| `chat/context_window.py` | `get_latest_last_good_checkpoint` (read-only) |
| `chat/context_window_first_turn.py` | Claim fail-closed + complete advances last-good |
| `tests/test_context_window_first_turn.py` | Three focused acceptance paths |
| This doc | Frozen contract |

## Last-good semantics

Last-good is a **complete five-field target checkpoint** written only after a
successful first-turn round:

```text
last_good_context_id                  = target context id
last_good_context_epoch               = target epoch
last_good_resident_generation         = target generation
last_good_history_cursor_message_id   = first assistant id
last_good_recorded_at                 = first_turn_completed_at
```

- It is **not** a source rollback pointer. Source is closed after handoff.
- It cannot rewrite or replay a failed in-flight answer.
- Half-set fields are never treated as a valid checkpoint.
- Reader: `get_latest_last_good_checkpoint` — no mutation, no resident start,
  no canonical guess, no cold-fallback wiring.

### Sole advance point

`complete_first_turn_round` final `BEGIN IMMEDIATE` writes in one UPDATE:

```text
first_turn_end_offset
first_turn_completed_at
first_turn_error_code = NULL
+ five last-good fields
```

Never advance on: candidate publish, prepare, READY, stdin Ack, COMMITTING,
`precommit_dirty`, HANDOFF_PENDING, assistant-without-cursor, cursor-without-lease,
partial canary, or abnormal exit.

Idempotent re-complete keeps the same assistant id and does not refresh
`last_good_recorded_at`. Legacy completed rows with empty last-good may backfill
only when `status=committed` and target cursor already equals
`first_assistant_message_id`; otherwise fail closed.

## Claim fail-closed order

Before user mapping / lease / `prepare_staged`:

1. `orphan_jsonl_state == precommit_dirty` → `FIRST_TURN_PRECOMMIT_DIRTY`
2. `status == committing` → `FIRST_TURN_IN_PROGRESS` (even same request/user ids)
3. `status != ready` → `FIRST_TURN_INTENT_STATUS`
4. Only READY may enter construction (same clean-retry rules as before)

Same-process HANDOFF recovery stays on `recover_first_turn_handoff_pending`
with an existing `FirstTurnSession`. Re-claim must not resume COMMITTING /
HANDOFF_PENDING.

## Structural Canary

Offline only: tempfile SQLite, temp Claude home / cwd / JSONL,
`offline_first_turn_hooks`, fake staged resident. Proves isolation via explicit
injected paths — never opens the default production DB.

```text
OWNER_CANARY_NOT_IMPLEMENTED
NIGHTLY_SCHEDULER_NOT_IMPLEMENTED
```

## Focused acceptance (three paths)

1. Happy complete → target last-good; incomplete stages empty; idempotent complete
2. Dirty / ambiguous COMMITTING refuse claim; clean READY same user still retryable
3. Structural canary isolation on temp roots; failure does not auto-repair or advance last-good
