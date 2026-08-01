# Context Window v0.2 — Cold Fallback Runtime / Owner Canary (R0)

**Frozen:** 2026-08-01  
**Baseline:** `fd0dc1d51ce0544ca075d8d0b9d9ab1bede79f26`

```text
CONTRACT_PASS
IMPLEMENTATION (this PR)
OWNER_CANARY structural path verified in tests
OWNER live Claude cold turn: ENVIRONMENT_BLOCKED / not required for Draft
NIGHTLY_NOT_AUTHORIZED
NO_AUTOMATIC_CROSS_PROCESS_FIRST_DELTA_RECOVERY
```

## Scope

Implemented:

1. Owner-explicit abandon of a failed first-turn (`committing` / `handoff_pending`)
2. Brand-new cold fallback recovery context from last-good target checkpoint
3. One-shot isolated Owner Canary (structural; live Claude optional / gated)

Not implemented:

- Resend failed user / forge missing assistant / guess JSONL
- Reopen old source as canonical
- Auto clear dirty / cross-process first-delta recovery
- Nightly / cron / CI runner / monitoring
- Capacity Swap / frontend / flag-on / real-user chat acceptance

## Owner CLI (sole production mutation entry)

```bash
python3 tools/context_window_admin.py inspect-first-turn \
  --request-id <failed_switch_request_id>

python3 tools/context_window_admin.py recover-from-last-good \
  --request-id <failed_switch_request_id> \
  --expected-status <committing|handoff_pending> \
  --expected-first-turn-request-id <first_turn_request_id> \
  --reason "<human reason>" \
  --confirm-abandon-failed-turn
```

No HTTP API, no frontend, no chat-path trigger, no auto-trigger.

## Recover semantics

- last-good identity only via complete five-field checkpoint
- `safe_cursor >= floor_cursor` with Registry / mapping proof; else floor; else `FALLBACK_CHECKPOINT_UNPROVEN`
- New recovery context: `window_mode=manual`, `epoch=max+1`, `generation=1`, `boundary=safe_cursor`, `source_context_id=last_good.context_id`
- Materials = last-good inherited carryover ∪ complete formal rounds ≤ safe_cursor (deduped, original order)
- Never includes failed user / failed target messages
- Single `BEGIN IMMEDIATE`; failure → full rollback; intent stays active; busy remains
- Failed intent → `released` + `FIRST_TURN_ABANDONED_BY_OWNER`; `orphan_jsonl_state` preserved
- Close failure-scene canonical with `close_reason=cold_fallback`
- Does **not** call the model or start a resident

## Owner Canary

```bash
python3 tools/context_window_admin.py owner-canary --confirm-live
```

Temp SQLite / Claude home / cwd only; refuses repo or `/opt/frontend` roots; never opens production DB. Cleanup deletes the temp root.

```text
NIGHTLY_NOT_AUTHORIZED
```

Nightly requires a separate authorized phase after this implementation PASSes and the project map is synced.

## Allowlist

| File | Role |
|------|------|
| `chat/daily_context.py` | Owner audit columns |
| `chat/context_window.py` | `CLOSE_REASON_COLD_FALLBACK` |
| `chat/context_window_fallback.py` | recover + inspect + canary fixture |
| `tools/context_window_admin.py` | Owner CLI |
| `tests/test_context_window_fallback.py` | Paths A/B |
| `tests/test_context_window_owner_canary.py` | Path C structural |
| This doc | Contract |

Forbidden: `gateway.py`, `daily_runtime.py`, `session_registry.py`, `cc_resident.py`, frontend, CI, deploy, flag defaults.
