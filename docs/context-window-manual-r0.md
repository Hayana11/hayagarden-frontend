# Manual context window R0

P-CONTEXT-MANUAL-WINDOW-R0: manual-first context window contract and atomic
switch API. **Default off** (`DAILY_SOFT_WINDOW_ENABLED=0`).

## Product truth

- Calendar date change does **not** imply a context switch.
- Asia/Shanghai 04:00 is for stats/labels/ledger/period/diary only — not context creation.
- Formal chat send never auto-switches or auto-locks carryover to 0.
- Only an explicit **switch transaction** opens the next window.
- Multiple manual switches per calendar day are allowed; one window may span days.
- Carryover is measured in full conversation **rounds** (user-first).
- Handoff, long-term memory, and verbatim carryover are separate layers (not in R0).

## Canonical API

| Method | Path |
|--------|------|
| GET | `/api/context-window/current` |
| GET | `/api/context-window/carryover-candidates` |
| POST | `/api/context-window/switch` |

Browser BFF (nginx strips `/api/gw`):

| Method | Path |
|--------|------|
| GET | `/api/gw/context-window/current` |
| GET | `/api/gw/context-window/carryover-candidates` |
| POST | `/api/gw/context-window/switch` |

Legacy daily endpoints (`/api/daily-context/*`) remain for flag-off compatibility and
**do not** call the manual switch mutation.

## Schema

Table `daily_contexts` keeps its name. New columns:

- `window_mode`: `legacy_daily` | `manual`
- `opened_at`, `closed_at`, `close_reason` (`manual` | `capacity_rescue` reserved)
- `source_context_id`, `switch_request_id`

Partial unique indexes:

- `legacy_daily`: `(chat_id, local_day)`
- open `manual`: one per `chat_id` where `closed_at IS NULL`
- idempotency: `(chat_id, switch_request_id)` when set

Migration is a single atomic transaction:

1. `BEGIN IMMEDIATE`
2. `CREATE TABLE ... (id INTEGER PRIMARY KEY AUTOINCREMENT, ...)` via `conn.execute`
   (**not** `sqlite3.executescript`, which implicitly commits)
3. copy rows → verify row count, ID set, `MAX(id)`, related-table `context_id` maps
4. `DROP` / `RENAME`
5. sync `sqlite_sequence`, create all partial indexes
6. final verify → `COMMIT`

On failure: rollback + drop any leftover `daily_contexts__mw_new`. Original table and
indexes remain; next `ensure_schema` can retry. Related maps verified for:

- `daily_carryover_messages`
- `daily_resident_cursors`
- `daily_resident_turn_leases`
- `daily_message_contexts`
- `daily_resident_owners`

Manual `local_day` / `opened_local_day` is the Shanghai **natural calendar day** at open
time (`strftime('%Y-%m-%d')`), not the legacy 04:00 chat-day.

## Resolver

`chat/context_window.py` → `get_current_context_window()` / `current_window_summary()`:

1. Open `manual` window (highest epoch), else
2. Open `legacy_daily` with `closed_at IS NULL`, else
3. If any historical rows exist → **fail closed** (`NoOpenContextWindowError` / HTTP 409
   `no_open_context`) — never resurrect a closed manual or closed legacy, else
4. One-time bootstrap on empty DB (not a switch).

`current_window_summary` assembles context row, formal rounds, carryover IDs, lease, and
`can_switch` on **one connection / one snapshot**.

No 04:00 rollover side effects. No model/Wake/handoff.

### Fail-closed recovery

R0 does not auto-heal a chat that only has closed windows. Operators / a later release
must open a new window via an explicit switch (or a dedicated recovery path). Silent
re-open of closed rows is forbidden.

## Legacy daily resolver

`resolve_current_daily_context_for_api()` and friends remain for formal runtime:

> **legacy compatibility — scheduled for removal after manual FE/runtime wiring**

## Future layers (not implemented in R0)

```text
A. Verbatim carryover — user-selected 0/3/5/10 full rounds
B. facts-only handoff — decisions, todos, preferences, commitments
C. optional continuity cue — one-shot interaction tone; not long-term memory
```

## Emergency rescue

`capacity_rescue` exists as an internal enum only. No thresholds, timers, background
jobs, or silent switches in R0.

## Tests

```bash
python3 -m unittest tests.test_context_window tests.test_context_window_routes tests.test_context_window_bff -v
```
