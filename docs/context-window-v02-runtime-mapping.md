# Context Window v0.2 — Runtime → Session Registry / Mapping (R1)

Minimal formal wiring of the already-landed Session Registry + message-event
Mapping into successful Daily Runtime turns. Flag-off. No Preview / Forge /
Capacity Swap / Canary / deploy.

## Scope

| Module | Change |
|--------|--------|
| `cc_resident.ResidentSession` | Read-only `cwd`; side-effect-free `peek_respawn_reason` |
| `chat/daily_runtime.py` | Transcript observation on plan; generation/session door-lock; post-success Mapping |
| `tests/test_daily_runtime.py` | Four focused Runtime wiring cases |
| This doc | Contract notes |

Frozen (not modified): `gateway.py`, `session_registry.py`, `claude_event_mapping.py`,
`daily_context` schema, Transcript Core, Preview/Forge, frontend, deploy, flags.

## Per successful formal turn

1. **Door-lock** (before `ensure_alive` / stdin): if `(context_id, resident_generation)`
   already has a Registry row and the live resident is about to get a *new* Claude
   session (`peek_respawn_reason` non-null, or live `session_id` missing/mismatched),
   release lease → close resident → `respawn_daily_resident` → reprepare once.
   Same Registry session + no respawn reason may continue even if the plan looks
   cold-like (empty cursor bootstrap). Second mismatch → release current lease →
   `DailyRuntimeError(registered_session_generation_mismatch)` and no stdin.
2. **In-place plan adopt**: registered-session and hot→cold reprepare both call
   `_adopt_reprepared_plan_in_place` so Gateway's original `DailyTurnPlan` object
   keeps the same `id(plan)` while receiving the new context / generation / lease /
   cursor / manifest / transcript fields. Recursion continues on that same object;
   callers must not chase a hidden `new_plan`.
3. **Start capture** (after `ensure_alive`, before `send_turn`): cwd, process
   generation, session id; hot path snapshots JSONL EOF as start offset; cold
   path uses start offset `0` and fills path at done.
4. **End capture** (on `done`, before yield): require session id; re-snapshot;
   enforce path/session/generation consistency and `end >= start`. Observation
   failures only set `transcript_observation_error_code` — never abort a
   successful model reply.
5. **Mapping** (only inside `handle_provider_success`, after
   `complete_daily_turn` cursor CAS + lease release):
   `register_context_claude_session(source='daily_runtime')` then
   `run_mapping_pass`. Success → manifest `MAPPED`. Any registry/mapping
   rejection or exception → `BLOCKED` + warning log only.

## Manifest fields (runtime memory / internal)

- `transcript_mapping_status`: `NOT_ATTEMPTED` \| `MAPPED` \| `BLOCKED`
- `transcript_mapping_error_code`
- `transcript_mapping_event_count`
- `transcript_mapping_scan_offset`

Plan `transcript_*` fields are **not** written to `chat_messages.cache_info`,
SSE, or frontend. Transcript path is never exposed publicly.

## Failure semantics

- Mapping / Registry failure does **not** roll back assistant rows, chat cursor,
  or raise to Gateway.
- Does **not** call `abort_daily_turn`, close/respawn resident, or start workers.
- If Registry is already `BLOCKED`, keep blocked with existing `scan_error_code`;
  do not guess-scan past the old offset.

## Explicit non-goals

- Preview / Forge / first-turn commit / last-good / Capacity Swap / Canary
- flag-on / deploy / live Claude / production DB
- third status table, queue, retry framework, or background compensation
