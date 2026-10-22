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
   starts the cooldown, marks the log row `committed=1` and queues the Ombre
   touch in the outbox, all in one transaction. A failed, abandoned or retried
   turn costs nothing.
8. **Fail closed**: if the route-log row cannot be written, nothing surfaces
   that turn. Every memory that reaches a prompt is on record, which is what
   `commit` relies on.

**Touch outbox** (`memory_surface_touch_outbox`). Committing and touching
cannot share a transaction: Ombre is another process. So `commit` queues one
outbox row per committed decision. A memory not projected into Ombre yet gets
no row, because there is nothing to touch. The outbox is its own table:
route-log pruning never removes a pending touch.

`flush_touches` drains it. It runs on a background thread after every commit,
and every five minutes from `memory-surface-touch.timer`
(`python -m tools.memory_surface flush`), so a crash is recovered even if
nothing surfaces for a long time.

- **Claiming**: rows are claimed with a lease (`lease_owner`, `lease_until`)
  in one SQLite statement. Concurrent flushers, in other threads or processes,
  never send a row whose lease still holds.
- **Sending**: every claimed row is its own `touch` request in a single
  worker run, with its own result. A memory that surfaced twice is reinforced
  twice, and a bad bucket fails only its own row.
- **Per-row result**:
  - success → `done`;
  - a failed request → back to `pending` with `last_error`, and `failed`
    (keeping `last_error`) after `MAX_TOUCH_ATTEMPTS` (3). Nothing stops
    silently: `python -m tools.memory_surface outbox --status failed`
    lists them.
- **Ombre unavailable**: when the runtime is missing or unreachable, rows stay
  `pending` and no attempt is spent. The error is kept in `last_error`.
- **Pruning**: `done` rows are pruned after 30 days. `pending` and `failed`
  rows are kept.

Delivery is **at-least-once**. If the process dies after Ombre applied a touch
but before the outbox recorded it, the lease expires (runtime timeout plus 2
minutes) and the touch is sent again. At most one extra reinforcement is
accepted. A lease that expires with no attempts left is marked `failed`
(`lease_expired`).

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
  - `reason`: for example `gate_passed`, `gate_closed`, `no_clues`, `too_short`, `kernel_cutover_off`, `error`;
  - `ref`: the chosen `kernel:<lineage>`;
  - `detail_json`:
    - words, clue frequencies, pool size and Ombre status;
    - the first gate step with the candidate's verdict;
    - `frozen`: her words, the first candidate and the runner-up (date, type
      and text), as the gate saw them when it decided.
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
- **Snapshots**: message and memories come from the decision-time snapshot
  (`frozen`), so a later edit to the chat or the memory does not rewrite what
  the gate saw. Rows logged before snapshots existed (the first shadow day)
  show current text and say so (`frozen: false`).
- `POST /api/memory-surface/review/<log_id>/label` with `{label, note, expected_ref}` sets one label:
  - `ok`: the decision was right;
  - `missed`: a memory should have surfaced;
  - `wrong`: what surfaced, or would have surfaced in shadow, should not have.

  `clear` removes the label.
- **Labels** live in `memory_surface_labels`, one per decision.
  - The first label copies the decision-time snapshot, and later edits keep
    that copy. `expected` is the memory the owner names, with its text at
    labeling time.
  - The list is the route log plus labeled decisions whose log row was
    pruned (`log_pruned`). The error set stays listed, counted, editable and
    clearable after the 30-day pruning.

Both routes sit under `/api/*`, which the upstream proxy already protects.

## Deploy steps for the outbox and labels

1. Take a consistent SQLite backup and keep it at mode 0600:

   ```
   sqlite3 /opt/frontend/memories.db ".backup /var/lib/hayagarden/memories.db.bak-<stamp>"
   chmod 600 /var/lib/hayagarden/memories.db.bak-<stamp>
   ```
2. Migrate explicitly. Do not leave it to the first chat turn:

   ```
   python3.11 -m tools.memory_surface migrate --db /opt/frontend/memories.db
   ```

   It creates `memory_surface_labels` and `memory_surface_touch_outbox`. The
   route log's shape is unchanged. The command prints every table's columns
   for checking.
3. Install the timer:

   ```
   cp deploy/systemd/memory-surface-touch.{service,timer} /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now memory-surface-touch.timer
   ```

   Then check one run with `systemctl status memory-surface-touch.service`.

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
