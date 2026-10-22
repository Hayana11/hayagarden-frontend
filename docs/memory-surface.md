# Passive memory surfacing (phase 1)

One message from her → **0 or 1** memory, as its original text. Nothing surfaces
rather than a weak guess. Implemented in `tools/memory_surface.py`; the Daily
path (`chat/daily_runtime.py`) puts the chosen memory in front of her words as a
per-turn prefix, the same way task feedback rides.

## One recall entry per turn

| turn | path | recall |
|---|---|---|
| her message | Daily soft window | `memory_surface` only |
| regenerate / rewrite (`rewrite_id`) | classic resident | legacy `gateway._recall_memories` |
| heartbeat wake | classic resident | legacy `gateway._recall_memories` |

The Daily stream returns before the classic code, so a Daily turn never runs
the legacy recall; `tests/test_memory_surface_daily.py` pins this for cold and
hot turns, with the legacy recall returning content.

## Route

1. **Entry gate**: skip empty text, `[image]`, `/commands` and messages under 4
   characters. Text inside `【…】` and code fences counts as pasted material, not
   her words, and only the first 200 characters are read.
2. **Clues**: jieba words minus pet names, fillers and English stopwords. A word
   is a *clue* only if it appears in at most `MEMORY_SURFACE_CHAT_DF_MAX` (20) of
   the latest 4000 earlier chat messages. "地方" or "一次" are everywhere and prove
   nothing. The message being answered is already stored when Daily decides, so it
   is left out of the count.
3. **Candidate pool**: Kernel MEMORY / MISS / FACT heads the AUTOMATIC
   lifecycle allows. Daily carries no FACT list, so FACTs compete like any
   memory. Anything this turn already carries (static system, handoff,
   carryover, today's history) is skipped by the in-context check. Memories from
   the last `MEMORY_SURFACE_RECENT_DAYS` (2) days are excluded: they are still
   in the day's context.
4. **Two lists, merged with RRF**, over the memories that contain at least one
   clue:
   - Ombre's ranking (`search_bucket_ids`);
   - keyword evidence, which is the summed rarity (IDF over their chat) of the
     clues found in each memory.

   Ombre's own score mixes importance and recency, so it orders candidates but
   never nominates a memory her words do not touch, and never opens the gate.
5. **Gate**, applied to the **top candidate only**: it passes only with at least `MEMORY_SURFACE_MIN_WORDS` (2)
   clues and evidence of at least `MEMORY_SURFACE_EVIDENCE_MIN` (6.0). A single
   clue passes only when it never appears in recent chat
   (`MEMORY_SURFACE_SINGLE_CLUE_DF_MAX` = 0) **and** Ombre ranks the same memory
   within its top 3. After that:
   - **cooldown**: a memory that surfaced stays quiet for
     `MEMORY_SURFACE_COOLDOWN_TURNS` (60) of her messages;
   - **already in context**: a memory whose opening already appears in today's
     history is skipped.

   If the top candidate fails any of these, nothing surfaces: the runner-up is
   never promoted in its place (it is recorded as `runner_up` in the log).
6. **Deliver** (mode `on`): `<recalled-memory>` with the date and the original
   text (600 characters at most). It carries the instruction
   "相关就自然融入，不相关就忽略".
7. **Commit**: deciding consumes nothing. Only after the reply that carried the
   memory is persisted does the gateway call `memory_surface.commit`, which
   starts the cooldown and marks the log row `committed=1`, then hands the Ombre
   touch to the outbox. A failed, abandoned or retried turn costs nothing.
8. **Fail closed**: if the route-log row cannot be written, nothing surfaces
   that turn. Every memory that reaches a prompt is on record, which is what
   `commit` relies on.

**Touch outbox.** Committing and touching cannot share a transaction (Ombre is
another process). A committed row starts `touched=0`, unless the memory is not
projected into Ombre yet, in which case there is nothing to touch and it starts at `touched=1`.
`flush_touches` runs on a background thread after every commit. It sends one
Ombre `touch` batch for every committed row still at `touched=0`, then:

- on success, sets `touched=1`;
- on failure, adds one to `touch_attempts`. A row stops being retried after
  `MAX_TOUCH_ATTEMPTS` (3).

A crash or an Ombre outage between commit and touch is retried on the next
commit. With no Ombre runtime configured, rows wait and no attempt is spent.

The same user message always gets the same decision: cold rebuilds, capacity
swaps and retries replay the logged decision instead of drawing again. A
replayed memory that has been retired since then is not resurrected.

## Switches (runtime_config, read every turn)

| key | default | meaning |
|---|---|---|
| `MEMORY_SURFACE_MODE` | `shadow` | `off`: nothing runs or is written. `shadow`: decide and log only; no prompt block, no UI, no touch, no cooldown. `on`: inject and show. Anything else reads as `shadow` (with a warning) |
| `MEMORY_SURFACE_LOG_RETENTION_DAYS` | `30` | route-log rows older than this are deleted on each write |
| `MEMORY_SURFACE_COOLDOWN_TURNS` | `60` | her messages before a memory may surface again |
| `MEMORY_SURFACE_EVIDENCE_MIN` | `6.0` | summed clue rarity needed |
| `MEMORY_SURFACE_MIN_WORDS` | `2` | distinct clues needed |
| `MEMORY_SURFACE_CHAT_DF_MAX` | `20` | a clue appears in at most this many of the last 4000 messages |
| `MEMORY_SURFACE_SINGLE_CLUE_DF_MAX` | `0` | a lone clue this rare may pass when Ombre agrees (top 3) |
| `MEMORY_SURFACE_RECENT_DAYS` | `2` | memories younger than this are not candidates |

Surfacing only runs while `memory.kernel_write` is on: it reads the Kernel.

## Route log

Every decision is a row in `memories.db`, including skips and failures:

- `memory_surface_log` columns:
  - `user_message_id`;
  - `mode`: `shadow` / `on`;
  - `decision`: `surfaced` / `none` / `skipped` / `failed`;
  - `committed`: 1 once the reply carrying it was persisted (mode `on` only);
  - `touched`, `touch_attempts`: the touch outbox (added to an existing table on first use);
  - `reason`: for example `gate_passed`, `gate_closed`, `no_clues`, `too_short`, `kernel_cutover_off`, `error`;
  - `ref`: the chosen `kernel:<lineage>`;
  - `detail_json`: words, clue frequencies, pool size, Ombre status, and the first gate steps with each candidate's verdict.
- `memory_surface_cooldown`: `ref` → the turn number it last surfaced (committed only).

`tools.memory_surface.recent_log(db_path)` returns the newest rows. The Daily
manifest also records `auto_recall_injected`, `memory_recall_mode`,
`memory_recall_decision`, `memory_recall_reason` and `memory_recall_ref` for
every turn.

## Review console (`/dash/memory-review`)

The owner labels decisions, so the gate can be tuned against real misses rather than guesses. It is
reached from the "复盘" chip on the memory library header.

- `GET /api/memory-surface/review?filter=&limit=&before=` returns decisions newest first, each with:
  - her words;
  - the clues and their chat frequency;
  - the first candidate with its verdict and evidence;
  - the runner-up;
  - the label, if any.

  The filters are `unlabeled`, `surfaced`, `blocked` (`gate_closed`), `labeled` and `all`.
- `POST /api/memory-surface/review/<log_id>/label` with `{label, note, expected_ref}` sets one label:
  - `ok`: the decision was right;
  - `missed`: a memory should have surfaced;
  - `wrong`: what surfaced, or would have surfaced in shadow, should not have.

  `clear` removes the label.
- Labels live in `memory_surface_labels`, one per log row. Each label stores a
  JSON snapshot of the decision, so it keeps its meaning after the 30-day
  route-log pruning removes the row it points at.

Both routes sit under `/api/*`, which the upstream proxy already protects.

## Rollout

1. Deploy with the default `shadow`; read the route log against real turns.
2. Owner true-device check on Huawei Chrome78: switch to `on` briefly in their own
   chat, look at the "◇ N 条记忆浮现 ›" pill live and after a reload.
3. `on` once both look right; `off` is always one runtime_config write away.

## Calibration (2026-10-22, last 80 messages, on a copy of memories.db)

3 of 80 messages surfaced a memory. Two were right: "记忆库统一" brought back the
memory-architecture note, and "flow studio / hidden" brought back the Hidden Flow
night. One was a literal coincidence (two rare words shared with an unrelated
memory). A wider single-clue rule (a lone clue in at most 5 chat messages, with
Ombre's agreement) added 4 surfacings and 3 of them were noise, so it stays at 0.

Cross-domain recall (希腊菜 → 赫泰拉) needs meaning, not words. That is phase 2:
a small model that reads the query and rechecks gray-zone candidates.

## UI

The gateway sends `memory_recall` (`{count, items: [{type, date, preview}]}`)
once provider output starts, and stores it in the reply's
`cache_info.memory_recall`. `/dash/chat` shows "◇ N 条记忆浮现 ›" above thinking,
both live and from history. Tapping it opens the recalled text.
