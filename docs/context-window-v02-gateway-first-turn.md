# Context Window v0.2 — Gateway / BFF first-turn wiring

Draft wiring of Forge publish, Target prepare, staged resident, and first-turn
core into the formal Claude Code chat entry.

Flag-off. No merge. No deploy. No live Claude. No production DB.

## Frozen path

```text
prepare-only switch
→ READY (source remains canonical, target manual_staged)
→ chat_stream inserts the sole formal user row
→ claim_and_start_first_turn(user_message_id=…)
→ staged.send_turn
→ buffer think/tool_* until first non-empty text
→ ingest_first_turn_text_delta (handoff)
→ release buffered prefix + first text in order
→ complete_first_turn_round
→ done
```

## Allowlist

| File | Role |
|------|------|
| `gateway.py` | prepare-only `_gw_run_seamless_switch`; READY bypass; first-turn stream |
| `chat/context_window_first_turn.py` | optional `user_message_id` claim (no second user) |
| `app/src/lib/manualContextWindow.ts` | prepare response parser |
| `tests/test_context_window_first_turn.py` | three focused paths + prepare contract |
| `app/scripts/test-manual-context-window.mjs` | prepare parse / reject assertions |
| This doc | wiring contract |

Frozen without new evidence: `context_window_bff.py`, `context_window_routes.py`,
`chat/daily_runtime.py`, `manualContextWindowController.ts`.

Forbidden: React pages, CarryoverModal copy/style, App routes, flag defaults,
Capacity Swap, Canary, last-good/fallback, cross-process first-delta recovery,
new tables / state machines / workers / CI.

## Prepare-only switch

`_gw_run_seamless_switch` must not call `switch_context_window`.

Order:

1. `publish_context_window_forge_candidate`
2. `prepare_context_window_target`
3. assemble public prepare response

Internal Forge constants (not HTTP fields):

```python
preview_id = request_id
thinking_policy = ThinkingPolicy.DROP
```

Success means **prepared, not switched**:

- `intent.status = ready`
- `target.window_mode = manual_staged`
- Registry ready
- source remains canonical

Must not close source, replace formal resident, call first-turn core, write
formal handoff, or return `switched_at`.

### Public response whitelist

```text
ok
prepare_status          # READY | ALREADY_READY
request_id
source_context_id
source_context_epoch
source_resident_generation
target_context_id
target_context_epoch
candidate_session_id
jsonl_sha256
jsonl_size
staged_ready_at
recovered
status                  # always "ready"
```

Never publish: `jsonl_path`, cwd, Claude home, transcript path, `st_dev` /
`st_ino`, staged handle, `switched_at`.

## Existing user claim

`claim_and_start_first_turn(..., user_message_id: Optional[int] = None)`:

- `None` → legacy offline INSERT (tests / internal)
- Gateway path → must pass the id created by `chat_stream.insert_user_message`

Same-transaction checks when id is provided: row exists, author is formal user,
content matches, map to target (idempotent if already mapped to same identity),
reject other-context mapping, never insert a second user.

Stable codes:

```text
FIRST_TURN_USER_MESSAGE_MISSING
FIRST_TURN_USER_CONTENT_CONFLICT
FIRST_TURN_USER_CONTEXT_CONFLICT
```

## Gateway READY bypass

After `insert_user_message`, before `prepare_daily_turn`:

| Intent | Behavior |
|--------|----------|
| none | ordinary daily soft-window path unchanged |
| READY + source match | first-turn path only |
| READY + source mismatch | reject; do not answer on wrong window |
| COMMITTING / HANDOFF_PENDING without in-process `FirstTurnSession` | single busy/`err`; no model; no user resend |

Cross-process first-delta recovery is **not** implemented
(`NO_AUTOMATIC_CROSS_PROCESS_FIRST_DELTA_RECOVERY`).

## First-turn stream

Reuse the staged resident from claim. External SSE stays `{"t","d"}` only.

### Stdin marker

Do **not** call `mark_first_turn_stdin_sent` before `send_turn`. Order:

```text
event_iter = iter(staged.send_turn(user))
first_event = next(event_iter)   # stdin.write+flush already done inside send_turn
mark_first_turn_stdin_sent(session)
chain(first_event, event_iter)…
```

If the first `next()` fails and JSONL has not grown past `start_offset` → clean.

### Unified pre-commit terminal

`_gw_first_turn_precommit_terminal` is the only pre-DB-commit closer. Evidence:

```text
jsonl_grew = current_size > session.start_offset
```

| State | Action |
|-------|--------|
| `not _db_committed` and not `jsonl_grew` | `abort_first_turn_clean` → READY, lease released, retryable |
| `not _db_committed` and `jsonl_grew` | `mark_first_turn_precommit_dirty` (no model retry) |
| `_db_committed` | no clean rollback; same-process HANDOFF_PENDING recover only |

In-memory `stdin_sent` must not override a non-grown JSONL.

### GeneratorExit / client disconnect

Catch `GeneratorExit`, best-effort `event_iter.close()` (resident kill path),
run the unified pre-commit terminal if first text not yet committed, clear
unreleased pending SSE, **do not yield** `err`/`done`, re-raise.

### Done / EOF before first text

If provider `done` or iterator EOF arrives while `first_released` is false:

- do not call `complete_first_turn_round`
- do not insert assistant
- do not send success `done`
- unified clean/dirty terminal + single failure `err`

Before first non-empty text: hold `think` / `tool_use` / `tool_result` /
`trace_summary` inside the generator. On first text: ingest → wait DB+handoff →
release held prefix in order → release first text → pass subsequent events.

Post-DB handoff failure: one same-process `recover_first_turn_handoff_pending`
while the generator still holds `FirstTurnSession`. First text released once;
never resend user or re-call the model.

Round end: `complete_first_turn_round` before success `done`. If assistant is
persisted but cursor finish fails → single `err`, no second assistant, no success
`done`. Usage SSE semantics unchanged.

## Frontend

Only `manualContextWindow.ts` protocol types/parser. Request body of
`switchWindow` unchanged. Controller source-identity checks and modal-close on
success unchanged. Reject legacy `switched_at` / `window_mode` /
`selected_message_ids` payloads.

## Focused acceptance (flag-off, no-live, no production DB)

1. prepare → existing user claim → buffer → first-text handoff → single assistant → done  
2. clean failure → same `user_message_id` retry; source canonical; target staged  
3. same-process HANDOFF_PENDING → recover once; first text once; no second user/assistant  
