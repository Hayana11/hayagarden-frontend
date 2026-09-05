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

### Authoritative stdin-flush ack

`ResidentSession.send_turn(..., on_stdin_flushed=None)` calls the optional
callback **synchronously after successful `stdin.write + flush`**, and **before**
`_commit_sent_context` / any stdout read. Write/flush failure skips the callback.
Callback failure kills the resident and re-raises (message already sent).

Gateway:

```python
stdin_flushed = False

def on_stdin_flushed():
    nonlocal stdin_flushed
    stdin_flushed = True
    mark_first_turn_stdin_sent(session)

event_iter = staged.send_turn(user, on_stdin_flushed=on_stdin_flushed)
```

Do **not** infer send success from first `next()` or JSONL growth alone.

### Unified pre-commit terminal

`_gw_first_turn_precommit_terminal` is the only pre-DB-commit closer.

Priority:

```text
DB committed > stdin flush ack > JSONL auxiliary evidence
```

| State | Action |
|-------|--------|
| `_db_committed` | no clean rollback; same-process HANDOFF_PENDING recover only |
| `stdin_flushed` (or `session.stdin_sent`) | `mark_first_turn_precommit_dirty` — never READY |
| not flushed and JSONL not grown | `abort_first_turn_clean` → READY, lease released, retryable |
| not flushed but JSONL grown / unreadable | `mark_first_turn_precommit_dirty` |

`abort_first_turn_clean` fails closed when `session.stdin_sent` is true
(`FIRST_TURN_NOT_CLEAN`).

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
release held prefix in order (including `tool_result`) → release first text →
pass subsequent events.

`tool_result` SSE matches the formal CC chat stream (`t`/`d`/`idx`, plus
compatible `tool_call` dup), with a local tool-call accumulator keyed by
`tool_use_id`.

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


## Transcript-attested first-turn recovery

Provider stdout abnormal exit remains fail-closed by default. Before
`abort_first_turn_postcommit`, the Manual Context Window first-turn path may
use the exact `first_turn_start_offset -> stable EOF` transcript range only
when all of the following are proven: target Registry/session identity matches,
the range contains exactly one unique main-chain candidate round, the terminal
assistant is non-sidechain and non-empty with `message.stop_reason=end_turn`,
the range contains no tools or pending approval, and the transcript text is
equal to the text already streamed by Gateway. EOF must be stable across
consecutive reads. On success the existing `complete_first_turn_round`
transaction is reused, with no resident-generation bump and no
`FIRST_TURN_POSTCOMMIT_ABORT`. Any failed or ambiguous proof preserves the
existing abort behavior.
