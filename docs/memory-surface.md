# Passive memory surfacing (phase 1)

One message from her → **0 or 1** memory, as its original text. Nothing surfaces
rather than a weak guess. Implemented in `tools/memory_surface.py`; the Daily
path (`chat/daily_runtime.py`) puts the chosen memory in front of her words as a
per-turn prefix, the same way task feedback rides.

## Route

1. **Entry gate**: skip empty text, `[image]`, `/commands` and messages under 4
   characters. Text inside `【…】` and code fences counts as pasted material, not
   her words, and only the first 200 characters are read.
2. **Clues**: jieba words minus pet names, fillers and English stopwords. A word
   is a *clue* only if it appears in at most `MEMORY_SURFACE_CHAT_DF_MAX` (20) of
   the latest 4000 chat messages. "地方" or "一次" are everywhere and prove nothing.
3. **Candidate pool**: Kernel MEMORY / MISS heads the AUTOMATIC lifecycle
   allows. FACTs belong to the cold start. Memories from the last
   `MEMORY_SURFACE_RECENT_DAYS` (2) days are excluded: they are still in the
   day's context.
4. **Two lists, merged with RRF**:
   - Ombre's ranking (`search_bucket_ids`);
   - keyword evidence, which is the summed rarity (IDF over their chat) of the
     clues found in each memory.

   Ombre's own score mixes importance and recency, so it orders candidates but
   never opens the gate.
5. **Gate**: a memory passes only with at least `MEMORY_SURFACE_MIN_WORDS` (2)
   clues and evidence of at least `MEMORY_SURFACE_EVIDENCE_MIN` (6.0). A single
   clue passes only when it never appears in recent chat
   (`MEMORY_SURFACE_SINGLE_CLUE_DF_MAX` = 0) **and** Ombre ranks the same memory
   within its top 3. After that:
   - **cooldown**: a memory that surfaced stays quiet for
     `MEMORY_SURFACE_COOLDOWN_TURNS` (60) of her messages;
   - **already in context**: a memory whose opening already appears in today's
     history is skipped.

   The first survivor wins, and nothing is promoted to replace a rejected one.
6. **Deliver**: `<recalled-memory>` with the date and the original text (600
   characters at most). It carries the instruction "相关就自然融入，不相关就忽略". The
   surfaced bucket is touched in Ombre.

The same user message always gets the same decision: cold rebuilds, capacity
swaps and regenerations replay the logged decision instead of drawing again. A
replayed memory that has been retired since then is not resurrected.

## Switches (runtime_config, read every turn)

| key | default | meaning |
|---|---|---|
| `MEMORY_SURFACE_ENABLED` | `1` | `0` turns surfacing off; turns run as before |
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
  - `decision`: `surfaced` / `none` / `skipped` / `failed`;
  - `reason`: for example `gate_passed`, `gate_closed`, `no_clues`, `too_short`, `disabled`, `kernel_cutover_off`, `error`;
  - `ref`: the chosen `kernel:<lineage>`;
  - `detail_json`: words, clue frequencies, pool size, Ombre status, and the first gate steps with each candidate's verdict.
- `memory_surface_cooldown`: `ref` → the turn number it last surfaced.

`tools.memory_surface.recent_log(db_path)` returns the newest rows. The Daily
manifest also records `auto_recall_injected`, `memory_recall_decision`,
`memory_recall_reason` and `memory_recall_ref` for every turn.

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
