# Context Window v0.2 — Target / Registry / staged resident prepare

Independent prepare unit after Forge publish+bind. Creates a provisional
`manual_staged` target, binds Session Registry, and health-checks a staged
resident — without closing source, swapping the formal resident, or running
first-turn.

Flag-off. No Gateway route, no frontend, no production publisher calls.

## Scope

| Module | Change |
|--------|--------|
| `chat/context_window_target_prepare.py` | Prepare orchestrator |
| `chat/daily_context.py` | Exclude `manual_staged` from formal-current; unique staged fence |
| `chat/daily_fence.py` | `_CURRENT_ROW_SQL` excludes `manual_staged` |
| `chat/context_window_forge_publish.py` | Public read-only `verify_published_candidate_file` |
| `tests/test_context_window_target_prepare.py` | Three focused paths |
| This doc | Frozen contract |

Frozen: Gateway routes, formal switch handoff, first-turn, source close,
Capacity Swap, Canary, flags, frontend, production JSONL/DB tests.

## Why not the old switch order

Old `switch_context_window` binds prepare into commit:

Forge → staged health → ready → **close source** → create target → swap
`_CC_RESIDENT` → first-turn → late Registry.

This stage splits **prepare the new room** from **hand over the keys**.
Registry binds immediately after the target row exists.

## Frozen order

1. **Candidate file re-verify** from forging intent
   (`target_session_id` / sha / size / `preview_id` / `thinking_policy`) via
   `verify_published_candidate_file` (dir-fd, no symlink follow, regular file,
   mode ≤ 0600, inode/size/hash).
2. **Create staged target** `window_mode='manual_staged'`:
   - same `chat_id` as source
   - `source_context_id` → source
   - `switch_request_id` = request
   - `claude_session_id` = candidate session
   - `status=PROVISIONAL`, `resident_generation=1`, `is_backfill=0`
   - carryover messages copied
   - new `context_epoch` (`_max_epoch + 1`)
   - **not** selected by canonical / formal-current resolvers
   - source `closed_at` / `version` / `epoch` / resident unchanged
3. **Formal-current exclusion** — all latest-formal queries exclude
   `manual_staged` (see list below). Unique index:
   one `manual_staged` row per `chat_id`.
4. **Registry bind** immediately:
   - `source=context_window_forge`
   - `scan_offset=target_jsonl_size`
   - identity chain Registry → target → `switch_request_id` → intent
   - no resident owner / cursor / `_LOCAL_BINDING`
5. **Staged resident** via hooks (`spawn_resumable` + health in production;
   offline fake in tests):
   - must not replace `_CC_RESIDENT` or write local binding / owner
   - no user message / first-turn consume
   - JSONL inode/size/hash unchanged across health
6. **Success** only when all three pass:
   - `intent.status=ready`
   - `intent.target_context_id=<target>`
   - `intent.staged_ready_at=<ts>`

`ready` means: candidate file, provisional doorplate, Registry, and
health-restorable staged resident are prepared; **source remains the only
formal window**.

## Failure / recovery

On failure (while not yet durable-ready):

- discard this request’s staged handle
- delete this request’s Registry row + `manual_staged` target
- clear intent `target_context_id` / `staged_ready_at`
- return intent to `forging`
- keep candidate JSONL / session / hash / size
- do **not** re-run Preview or Forge

Same-request retry:

- exact target + Registry → confirm in place (no second row)
- target present, Registry missing → register only
- identity mismatch → fail closed (no adoption)
- staged handle lost → re-`--resume` + health

## Formal-current exclusions

`window_mode != 'manual_staged'` applied at least to:

- `_active_epoch_high_water`
- `_latest_active_context_row`
- `is_epoch_current`
- `get_latest_active_context`
- `claim_daily_resident_turn`
- `renew_resident_turn_lease`
- `persist_daily_assistant_if_current`
- `chat.daily_fence._CURRENT_ROW_SQL`

Canonical resolver already prefers open `manual` / open `legacy_daily` only.

## Still forbidden in this stage

first-turn · close source · promote target to `manual` · update canonical
current · formal resident swap · formal owner/cursor/local binding ·
last-good/fallback · Capacity Swap · Canary · flag-on · frontend ·
production DB/JSONL tests.
