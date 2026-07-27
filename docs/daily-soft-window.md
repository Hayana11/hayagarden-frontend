# P-CONTEXT-DAILY-SOFT-WINDOW-BE-R0

## Status

Backend-only Draft. Default **off** (`DAILY_SOFT_WINDOW_ENABLED=0`).
Does **not** generate day handoffs via LLM, does not change formal chat defaults,
does not enable itself on deploy.

## Product semantics

- Same UI chat window; no new visible session.
- Backend `context_epoch` rolls at Asia/Shanghai **04:00** (half-open `[04:00, next 04:00)`).
- New epoch provider context may include only:
  - production static system
  - optional facts-only formal handoff
  - user-selected yesterday carryover message IDs
  - `build_cc_state(lean=True)` snapshot/delta (explicit call; does not flip global lean flags)
  - current-day messages after `boundary_message_id`

## Schema

Tables (idempotent `ensure_schema`):

- `daily_contexts` — per `(chat_id, local_day)` epoch row + status/lease/generation
- `daily_carryover_messages` — exact selected `message_id`s
- `day_handoffs` — formal handoff storage (fixture/manual insert only in this PR)

## State machine

`ABSENT → COMPACTING → PROVISIONAL → FINALIZED`  
Failure: `COMPACTING → FAILED_RETRYABLE → PROVISIONAL`

Centralized in `chat/daily_context.py` (`_TRANSITIONS` / `assert_transition`).

## Modules

| Module | Role |
|--------|------|
| `chat/daily_context.py` | Schema, day math, get_or_create, carryover, handoff store, epoch fence |
| `chat/daily_history.py` | Provider assembly + manifest |
| `daily_context_routes.py` | Internal Bearer-protected APIs |

## APIs (flag-gated)

- `GET /api/daily-context/current`
- `GET /api/daily-context/carryover-candidates`
- `POST /api/daily-context/select-carryover` body `{ "count": 0\|3\|5\|10 }`

## Explicit non-goals (this PR)

- Auto LLM handoff generation
- Wake rollover wiring
- Morning greeting generation
- Frontend selection card
- Enabling `DAILY_SOFT_WINDOW_ENABLED`
- Changing Clean Window / Daily Candidate Shadow behavior
