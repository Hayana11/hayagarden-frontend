# Context Window v0.2 — Cold Fallback Runtime / Owner Canary (R0)

**Frozen:** 2026-08-01  
**Baseline:** `fd0dc1d51ce0544ca075d8d0b9d9ab1bede79f26`

```text
CONTRACT_PASS
IMPLEMENTATION
OWNER_CANARY structural path: PASS (temp DB/home/cwd; cleanup verified)
OWNER live Claude: isolated auth preflight + optional ephemeral native login
NIGHTLY_NOT_AUTHORIZED
NO_AUTOMATIC_CROSS_PROCESS_FIRST_DELTA_RECOVERY
```

## Scope

Implemented:

1. Owner-explicit abandon of a failed first-turn (`committing` / `handoff_pending`)
2. Brand-new cold fallback recovery context from last-good target checkpoint
3. One-shot isolated Owner Canary — structural paths + live auth preflight / conditional cold turn
4. Optional one-time native Claude.ai login inside the **same ephemeral Canary auth home** when isolated auth is absent

Not implemented:

- Resend failed user / forge missing assistant / guess JSONL
- Reopen old source as canonical
- Auto clear dirty / cross-process first-delta recovery
- Import or reuse production `CLAUDE_CODE_OAUTH_TOKEN`, production `~/.claude`, production `~/.claude.json`, API key, Bedrock or Vertex auth
- Persistent second credential store / custom credential format
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

Normal preflight-only behavior remains:

```bash
python3 tools/context_window_admin.py owner-canary --confirm-live
```

If the new ephemeral auth home is not logged in, it returns probe-derived `ENVIRONMENT_BLOCKED` and does not start the cold turn.

For the one-shot real live acceptance, the owner may explicitly authorize native login:

```bash
python3 tools/context_window_admin.py owner-canary --confirm-live --login
```

Temp SQLite / Claude config / OS home / cwd only; refuses repo or `/opt/frontend` roots; never opens production DB. Cleanup deletes the entire temp root, including login credentials and Canary JSONL.

Claude Code keeps user configuration under its config directory but OAuth session state can also live in user-home data such as `~/.claude.json`. Therefore this Canary pins **both**:

- `CLAUDE_CONFIG_DIR=<temp-root>/claude-home`
- `HOME=<temp-root>/fake-home`

for auth probe, native login, and live cold turn. This prevents the login or subsequent probe from falling back to production `/root/.claude.json` while still allowing the just-created ephemeral OAuth session to be seen by the next step.

`--confirm-live` **must**:

1. Run isolated subscription auth preflight with temp `HOME` + temp `CLAUDE_CONFIG_DIR` (API/OAuth/Bedrock/Vertex overlays cleared)
2. If identity is confirmed → run the isolated cold turn on the recovery context
3. If identity is not confirmed and `--login` is absent → return `ENVIRONMENT_BLOCKED` with the probe reason
4. If identity is not confirmed and `--login` is present → run official `claude auth login --claudeai` with the same temp `HOME` + `CLAUDE_CONFIG_DIR`
5. Re-run `auth status` under the same temp auth home; only a proved Pro/Max subscription may proceed to the cold turn
6. Login command failure, post-login auth failure, or model process non-start remains `ENVIRONMENT_BLOCKED`; a model that actually starts and then fails is `FAIL`

The login subprocess inherits the operator terminal/browser flow. It does **not** capture or print credential contents into the JSON report. It does not copy any production Claude config or token into the Canary.

`structural_only` is a test harness switch that skips live preflight/login/cold turn; it is not a production escape hatch and does not shrink the frozen live acceptance scope.

```text
NIGHTLY_NOT_AUTHORIZED
```

Nightly requires a separate authorized phase after Owner live Canary PASS and project-map sync.

## Allowlist

| File | Role |
|------|------|
| `chat/daily_context.py` | Owner audit columns |
| `chat/context_window.py` | `CLOSE_REASON_COLD_FALLBACK` |
| `chat/context_window_fallback.py` | recover + inspect + canary fixture + auth/cold helpers |
| `tools/context_window_admin.py` | Owner CLI + ephemeral native login orchestration |
| `tests/test_context_window_fallback.py` | Paths A/B |
| `tests/test_context_window_owner_canary.py` | Path C structural + live preflight/login wiring |
| This doc | Contract |

Forbidden: `gateway.py`, `daily_runtime.py`, `session_registry.py`, `cc_resident.py`, frontend, CI, deploy, flag defaults.
