"""Passive memory surfacing for a Daily turn: at most one memory, or none.

One user message in, 0 or 1 Kernel memory out, as its original text:

  entry gate -> candidate pool -> two ranked lists (Ombre ranking, keyword
  evidence) -> RRF -> gate (evidence floor, cooldown, already in context)
  -> the single best survivor, or nothing.

Ombre only orders candidates; its score mixes importance, activation and
recency, so it cannot say whether a memory is about what she just said. The
gate is keyword evidence: rare words she used that the memory also contains,
weighted by how rare they are among the candidates.

Every decision (including "nothing surfaced" and failures) is written to
``memory_surface_log`` with the reason, so a quiet turn is never a silent one.
Decisions are idempotent per user message: re-preparing the same turn
(capacity swap, cold rebuild, retry) reuses the first decision instead of
drawing a different memory.

Modes (runtime_config ``MEMORY_SURFACE_MODE``):
  off     nothing runs, nothing is written;
  shadow  decide and log only: no prompt block, no UI, no touch, no cooldown;
  on      the chosen memory goes into the prompt and the UI.

Deciding never consumes anything. Only ``commit`` -- called once the reply that
carried the memory is persisted -- starts its cooldown and touches it in Ombre,
so a failed, retried or abandoned turn costs nothing.

Read-only on memories: the Kernel and Ombre are never written. The only side
effects are the two surface tables in memories.db (the log pruned after
``MEMORY_SURFACE_LOG_RETENTION_DAYS``) and an Ombre ``touch`` on commit.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

_LOG = logging.getLogger("hayagarden.memory_surface")

USER_AUTHORS = ("hayana", "haya", "user")
RRF_K = 60
# FACTs too: Daily carries no FACT list, and one already in this turn's context
# is skipped by the in-context check like any other memory.
POOL_TYPES = frozenset({"MEMORY", "MISS", "FACT"})
MODES = ("off", "shadow", "on")
OMBRE_LIMIT = 20
MIN_QUERY_CHARS = 4
QUERY_CHARS = 200
# A word counts as a clue only if it is rare in their conversation: "地方" or "一次"
# shows up in hundreds of messages and proves nothing about which memory is meant.
CHAT_DF_WINDOW = 4000
_PASTED = re.compile(r"【.*?】|```.*?```", re.S)
PREVIEW_CHARS = 80
INJECT_CHARS = 600
# A word in more than this share of the pool says nothing about which memory is meant.
COMMON_SHARE = 0.3
# Pet names, fillers and frequent function words: present everywhere, prove nothing.
STOPWORDS = frozenset("""
宝宝 宝贝 爸爸 小猫 猫猫 主人 费佳 费奥多尔 哈娅 哈雅娜 老公 老婆
我们 你们 他们 她们 它们 自己 什么 怎么 怎样 这个 那个 这些 那些 这样 那样 这里 那里
就是 还是 然后 一下 一个 一些 没有 可以 可能 知道 觉得 现在 今天 明天 昨天 时候 真的 有点
因为 所以 但是 不是 而且 如果 虽然 已经 一起 还有 其实 应该 需要 好像 感觉 东西 事情
喜欢 想要 一点 一样 之后 之前 以后 以前 不要 不会 不能 怎么样 为什么 是不是 有没有
嗯嗯 哈哈 哈哈哈 好耶 呜呜 呜呜呜 嘿嘿 嘿嘿嘿 喵喵 喵喵喵 啊啊 好吧 好的 对吧 是的
""".split())
ENGLISH_STOPWORDS = frozenset("""
the and you are for with this that what have has was were will would can could should not but
your yours our ours they them their from into about just like then than there here when where
who why how all any some more most very really also only even still been being its it's i'm
you're don't can't
""".split())
_ASCII = re.compile(r"^[a-z0-9'._-]+$")
_WORD = re.compile(r"[一-鿿A-Za-z]")


@dataclass
class SurfaceConfig:
    mode: str = "shadow"
    log_retention_days: int = 30
    cooldown_turns: int = 60
    # At least two clues (words rare in their chat) found in the memory.
    evidence_min: float = 6.0
    min_words: int = 2
    chat_df_max: int = 20
    # One clue is enough only when it is very rare and Ombre independently ranks
    # the same memory near the top: two lanes agreeing.
    single_clue_df_max: int = 0  # 0: only words unseen in recent chat; wider proved noisy
    single_clue_ombre_rank: int = 3
    recent_days: int = 2


@dataclass
class SurfaceResult:
    decision: str  # surfaced | none | skipped | failed
    reason: str
    mode: str = "on"
    ref: str = ""
    block: str = ""
    items: list[dict[str, str]] = field(default_factory=list)
    bucket_id: str = ""
    projected: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def injected(self) -> bool:
        """Goes into this turn's prompt and UI (mode ``on`` only)."""
        return self.mode == "on" and self.decision == "surfaced" and bool(self.block)


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS memory_surface_cooldown (
        ref TEXT PRIMARY KEY,
        surfaced_turn INTEGER NOT NULL,
        surfaced_message_id INTEGER NOT NULL,
        surfaced_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS memory_surface_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_message_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        mode TEXT NOT NULL,
        decision TEXT NOT NULL,
        reason TEXT NOT NULL,
        ref TEXT,
        committed INTEGER NOT NULL DEFAULT 0,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_surface_log_msg ON memory_surface_log(user_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_surface_log_created ON memory_surface_log(created_at)",
    # The owner's verdict on one decision, with a frozen snapshot of what was decided:
    # the error set survives log pruning and later memory edits.
    """CREATE TABLE IF NOT EXISTS memory_surface_labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_id INTEGER NOT NULL UNIQUE,
        user_message_id INTEGER NOT NULL,
        label TEXT NOT NULL,
        expected_ref TEXT,
        note TEXT,
        snapshot_json TEXT NOT NULL,
        labeled_at TEXT NOT NULL
    )""",
)
# Columns added after the log table first shipped (2026-10-22): the touch outbox.
_LOG_COLUMNS = (
    ("touched", "INTEGER NOT NULL DEFAULT 0"),
    ("touch_attempts", "INTEGER NOT NULL DEFAULT 0"),
)
LABELS = ("ok", "missed", "wrong")
MAX_TOUCH_ATTEMPTS = 3
_FLUSH_LOCK = threading.Lock()  # one outbox flush at a time, so a pending row is never touched twice


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)
    have = {r[1] for r in conn.execute("PRAGMA table_info(memory_surface_log)")}
    for name, ddl in _LOG_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE memory_surface_log ADD COLUMN {name} {ddl}")


def load_config() -> SurfaceConfig:
    try:
        import config_store
    except Exception:  # tests / tools without the runtime config
        return SurfaceConfig()
    mode = str(config_store.get("MEMORY_SURFACE_MODE", "shadow") or "").strip().lower()
    if mode not in MODES:
        _LOG.warning("MEMORY_SURFACE_MODE=%r is not off/shadow/on; using shadow", mode)
        mode = "shadow"
    return SurfaceConfig(
        mode=mode,
        log_retention_days=config_store.get_int("MEMORY_SURFACE_LOG_RETENTION_DAYS", 30),
        cooldown_turns=config_store.get_int("MEMORY_SURFACE_COOLDOWN_TURNS", 60),
        evidence_min=config_store.get_float("MEMORY_SURFACE_EVIDENCE_MIN", 6.0),
        chat_df_max=config_store.get_int("MEMORY_SURFACE_CHAT_DF_MAX", 20),
        single_clue_df_max=config_store.get_int("MEMORY_SURFACE_SINGLE_CLUE_DF_MAX", 0),
        single_clue_ombre_rank=config_store.get_int("MEMORY_SURFACE_SINGLE_CLUE_OMBRE_RANK", 3),
        min_words=config_store.get_int("MEMORY_SURFACE_MIN_WORDS", 2),
        recent_days=config_store.get_int("MEMORY_SURFACE_RECENT_DAYS", 2),
    )


def keywords(text: str) -> list[str]:
    """Distinct content words of ``text`` in first-seen order (jieba, stopwords removed)."""
    import jieba
    seen: dict[str, None] = {}
    for word in jieba.lcut(str(text or "")):
        word = word.strip().lower()
        if len(word) < 2 or word in STOPWORDS or not _WORD.search(word):
            continue
        if _ASCII.match(word) and (len(word) < 3 or word in ENGLISH_STOPWORDS):
            continue
        if _only_stopwords(word):  # 爸爸爸爸, 费佳费佳
            continue
        seen.setdefault(word, None)
    return list(seen)


def _only_stopwords(word: str) -> bool:
    rest = word
    for stop in sorted(STOPWORDS, key=len, reverse=True):
        rest = rest.replace(stop, "")
    return not rest.strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def query_text(text: str) -> str:
    """Her own words: pasted 【…】 / fenced blocks removed, capped."""
    return _PASTED.sub(" ", str(text or ""))[:QUERY_CHARS]


def chat_frequency(
    conn: sqlite3.Connection, words: list[str], *, exclude_message_id: int,
    window: int = CHAT_DF_WINDOW,
) -> tuple[dict[str, int], int]:
    """In how many of the latest ``window`` earlier chat messages each word appears, and how many there are.

    The message being answered is already stored when Daily decides; counting it
    would make every word she just said look seen at least once.
    """
    recent = "SELECT id, content FROM chat_messages WHERE id != ? ORDER BY id DESC LIMIT ?"
    args = (int(exclude_message_id), int(window))
    total = int(conn.execute(f"SELECT COUNT(*) FROM ({recent})", args).fetchone()[0] or 0)
    out = {}
    for word in words:
        out[word] = int(conn.execute(
            f"SELECT COUNT(*) FROM ({recent}) WHERE instr(lower(content), ?) > 0", (*args, word),
        ).fetchone()[0] or 0)
    return out, total


def entry_gate(text: str) -> Optional[str]:
    """Why this message is not a recall query, or None when it is."""
    stripped = str(text or "").strip()
    if not stripped or stripped == "[image]":
        return "no_text"
    if stripped.startswith("/"):
        return "command"
    if len(_norm(stripped)) < MIN_QUERY_CHARS:
        return "too_short"
    return None


def keyword_list(
    pool: list[dict[str, Any]],
    words: list[str],
    chat_df: dict[str, int],
    chat_n: int,
) -> tuple[list[str], dict[str, float], dict[str, list[str]]]:
    """Refs ranked by keyword evidence: summed IDF (rarity in their chat) of her words in each memory."""
    n = len(pool)
    if not n or not words:
        return [], {}, {}
    norms = {item["ref"]: _norm(item.get("content")).lower() for item in pool}
    idf = {}
    for w in words:
        pool_df = sum(1 for text in norms.values() if w in text)
        if pool_df == 0 or pool_df / n > COMMON_SHARE:
            continue
        df = chat_df.get(w, 0)
        idf[w] = math.log((chat_n - df + 0.5) / (df + 0.5) + 1.0)
    evidence: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for ref, text in norms.items():
        hits = [w for w in idf if w in text]
        if hits:
            evidence[ref] = sum(idf[w] for w in hits)
            matched[ref] = hits
    ranked = sorted(evidence, key=lambda ref: (-evidence[ref], ref))
    return ranked, evidence, matched


def rrf(*lists: list[str]) -> dict[str, float]:
    fused: dict[str, float] = {}
    for ranked in lists:
        for rank, ref in enumerate(ranked):
            fused[ref] = fused.get(ref, 0.0) + 1.0 / (RRF_K + rank + 1)
    return fused


def _turn_number(conn: sqlite3.Connection, user_message_id: int) -> int:
    marks = ",".join("?" for _ in USER_AUTHORS)
    row = conn.execute(
        f"SELECT COUNT(*) FROM chat_messages WHERE lower(author) IN ({marks}) AND id<=?",
        (*USER_AUTHORS, int(user_message_id)),
    ).fetchone()
    return int(row[0] or 0)


def _cooling(conn: sqlite3.Connection, turn: int, cooldown_turns: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT ref FROM memory_surface_cooldown WHERE surfaced_turn > ?", (turn - cooldown_turns,))}


def _previous(conn: sqlite3.Connection, user_message_id: int, mode: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT decision, reason, ref, detail_json FROM memory_surface_log WHERE user_message_id=? "
        "AND mode=? AND decision IN ('surfaced','none','skipped') ORDER BY id ASC LIMIT 1",
        (int(user_message_id), mode),
    ).fetchone()


def _beijing_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)


def render(item: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    date = str(item.get("created_at") or "")[:10]
    content = str(item.get("content") or "").strip()
    block = (
        "<recalled-memory>\n"
        f"这是一条自动浮现的旧记忆（{date}），可能和她刚说的话有关。"
        "相关就自然融入，不相关就忽略，别硬用；不要向哈娅提及这个标签。\n\n"
        f"{content[:INJECT_CHARS]}\n"
        "</recalled-memory>"
    )
    items = [{"type": str(item.get("type") or "MEMORY"), "date": date, "preview": content[:PREVIEW_CHARS]}]
    return block, items


def _log(conn: sqlite3.Connection, user_message_id: int, result: SurfaceResult, now: datetime,
         retention_days: int) -> None:
    conn.execute(
        "INSERT INTO memory_surface_log (user_message_id, created_at, mode, decision, reason, ref, detail_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (int(user_message_id), now.strftime("%Y-%m-%d %H:%M:%S"), result.mode, result.decision, result.reason,
         result.ref or None, json.dumps(result.detail, ensure_ascii=False, sort_keys=True)),
    )
    if retention_days > 0:
        conn.execute("DELETE FROM memory_surface_log WHERE created_at < ?",
                     ((now - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S"),))


def decide(
    *,
    user_message_id: int,
    user_text: str,
    context_text: str,
    db_path: str | Path,
    config: Optional[SurfaceConfig] = None,
    pool_loader: Optional[Callable[[Path], list[dict[str, Any]]]] = None,
    ombre: Optional[Callable[[str, int], Optional[list[str]]]] = None,
    now: Optional[datetime] = None,
) -> SurfaceResult:
    """Pick 0 or 1 memory for this user message and record why. Never raises, never consumes."""
    config = config or load_config()
    if config.mode == "off":
        return SurfaceResult("skipped", "off", mode="off")
    now = now or _beijing_now()
    db_path = Path(db_path)
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        _LOG.warning("memory surface unavailable: %s", type(exc).__name__)
        return SurfaceResult("failed", "db_unavailable", mode=config.mode)
    try:
        with conn:
            ensure_schema(conn)
            previous = _previous(conn, user_message_id, config.mode)
        if previous is not None:
            conn.close()
            return _replay(previous, db_path, pool_loader, config.mode)
        result = _decide(conn, user_message_id=user_message_id, user_text=user_text,
                         context_text=context_text, db_path=db_path, config=config,
                         pool_loader=pool_loader, ombre=ombre, now=now)
    except Exception as exc:
        _LOG.exception("memory surface failed; turn continues without it")
        result = SurfaceResult("failed", "error", detail={"exc": type(exc).__name__})
    result.mode = config.mode
    if config.mode != "on":  # shadow: the decision is logged, nothing reaches the turn
        result.block, result.items = "", []
    try:
        with conn:
            _log(conn, user_message_id, result, now, config.log_retention_days)
    except Exception:
        # Every decision that reaches a turn must be on record (commit() finds it
        # there); an unlogged memory never surfaces.
        _LOG.exception("memory surface log write failed; nothing surfaces this turn")
        result = SurfaceResult("failed", "log_write_failed", mode=config.mode,
                               detail={"unlogged_decision": result.decision, "unlogged_ref": result.ref})
    finally:
        conn.close()
    return result


def _pool(db_path: Path, pool_loader) -> list[dict[str, Any]]:
    if pool_loader is not None:
        return pool_loader(db_path)
    from tools import memory_kernel_read as mkr
    return mkr.memory_items(db_path, mode=mkr.AUTOMATIC)


def _replay(previous: sqlite3.Row, db_path: Path, pool_loader, mode: str) -> SurfaceResult:
    """The same user message gets the same answer, even while that memory is cooling down."""
    detail = json.loads(previous["detail_json"] or "{}")
    if previous["decision"] != "surfaced":
        return SurfaceResult(previous["decision"], previous["reason"], mode=mode, detail=dict(detail, replayed=True))
    if mode != "on":
        return SurfaceResult("surfaced", previous["reason"], mode=mode, ref=previous["ref"],
                             detail=dict(detail, replayed=True))
    item = next((i for i in _pool(db_path, pool_loader) if i.get("ref") == previous["ref"]), None)
    if item is None:  # retired or revised since: do not resurrect it
        return SurfaceResult("none", "replayed_ref_gone", mode=mode, detail={"replayed": True})
    block, items = render(item)
    return SurfaceResult("surfaced", previous["reason"], mode=mode, ref=previous["ref"], block=block,
                         items=items, bucket_id=str(item.get("bucket_id") or ""),
                         projected=bool(item.get("projected")), detail=dict(detail, replayed=True))


def _decide(conn, *, user_message_id, user_text, context_text, db_path, config, pool_loader, ombre, now):
    if pool_loader is None:
        from tools.memory_kernel_switch import cutover_enabled
        if not cutover_enabled(db_path):
            return SurfaceResult("skipped", "kernel_cutover_off")
    gate = entry_gate(user_text)
    if gate:
        return SurfaceResult("skipped", gate)
    words = keywords(query_text(user_text))
    if not words:
        return SurfaceResult("skipped", "no_keywords")
    chat_df, chat_n = chat_frequency(conn, words[:12], exclude_message_id=user_message_id)
    clues = [w for w in words[:12] if chat_df.get(w, 0) <= config.chat_df_max]
    if not clues:
        return SurfaceResult("none", "no_clues", detail={"words": words[:12], "chat_df": chat_df})
    words = clues
    turn = _turn_number(conn, user_message_id)
    cutoff = (now - timedelta(days=config.recent_days)).strftime("%Y-%m-%d %H:%M:%S")
    everything = _pool(db_path, pool_loader)
    # Recent memories are still in the day's context.
    pool = [i for i in everything if str(i.get("type") or "MEMORY") in POOL_TYPES
            and str(i.get("created_at") or "") < cutoff and i.get("ref")]
    detail: dict[str, Any] = {"turn": turn, "words": words[:12], "chat_df": chat_df, "pool": len(pool)}
    if not pool:
        return SurfaceResult("none", "empty_pool", detail=detail)

    by_ref = {i["ref"]: i for i in pool}
    # Rarity is measured against a full window: a short history (new install) must not
    # make every word look common and close the gate for good.
    kw_ranked, evidence, matched = keyword_list(pool, words, chat_df, max(chat_n, CHAT_DF_WINDOW))
    if ombre is None:
        from tools import ombre_adapter
        ombre = ombre_adapter.search_bucket_ids
    try:
        bucket_ids = ombre(" ".join(words[:8]), OMBRE_LIMIT)
    except Exception:
        bucket_ids = None
    by_bucket = {str(i.get("bucket_id")): i["ref"] for i in pool if i.get("bucket_id")}
    ombre_ranked = [by_bucket[b] for b in (bucket_ids or []) if b in by_bucket]
    detail["ombre"] = "unavailable" if bucket_ids is None else len(ombre_ranked)
    # Only memories sharing at least one clue are candidates: Ombre orders them, it
    # never nominates a memory her words do not touch.
    fused = rrf([ref for ref in ombre_ranked if ref in evidence], kw_ranked)
    order = sorted(fused, key=lambda ref: (-fused[ref], ref))
    if not order:
        return SurfaceResult("none", "no_candidates", detail=detail)

    # Only the best candidate is judged. If it is blocked, nothing surfaces: a weaker
    # runner-up is never promoted in its place.
    top = order[0]
    score = round(evidence.get(top, 0.0), 2)
    hits = matched.get(top, ())
    corroborated = (
        len(hits) == 1
        and chat_df.get(hits[0], CHAT_DF_WINDOW) <= config.single_clue_df_max
        and top in ombre_ranked[:config.single_clue_ombre_rank]
    )
    head = _norm(by_ref[top].get("content"))[:24]
    if not corroborated and (score < config.evidence_min or len(hits) < config.min_words):
        verdict = "below_evidence"
    elif top in _cooling(conn, turn, config.cooldown_turns):
        verdict = "cooldown"
    elif head and head in _norm(context_text):
        verdict = "in_context"
    else:
        verdict = "chosen"
    detail["trail"] = [[top, verdict, score]]
    detail["runner_up"] = order[1] if len(order) > 1 else None
    if verdict != "chosen":
        return SurfaceResult("none", "gate_closed", detail=detail)
    chosen = top
    detail["matched"] = matched.get(chosen, [])
    detail["ombre_rank"] = ombre_ranked.index(chosen) + 1 if chosen in ombre_ranked else None
    item = by_ref[chosen]
    # commit() needs these after the reply is persisted, without reloading the pool.
    detail["bucket_id"] = str(item.get("bucket_id") or "")
    detail["projected"] = bool(item.get("projected"))
    block, items = render(item)
    return SurfaceResult("surfaced", "gate_passed", ref=chosen, block=block, items=items,
                         bucket_id=str(item.get("bucket_id") or ""), projected=bool(item.get("projected")),
                         detail=detail)


def commit(*, user_message_id: int, db_path: str | Path, now: Optional[datetime] = None,
           wait: bool = False) -> Optional[str]:
    """The reply that carried this message's surfaced memory is persisted: it has surfaced.

    Starts the memory's cooldown and marks the decision committed, then hands the
    Ombre touch to the outbox (``flush_touches``, off the request path), which
    also retries touches an earlier crash or Ombre outage left pending. Only an
    ``on`` decision commits; a second call is a no-op. Returns the committed ref,
    or None. Never raises.
    """
    now = now or _beijing_now()
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                ensure_schema(conn)
                row = conn.execute(
                    "SELECT ref, detail_json FROM memory_surface_log WHERE user_message_id=? AND mode='on' "
                    "AND decision='surfaced' AND committed=0 ORDER BY id ASC LIMIT 1",
                    (int(user_message_id),),
                ).fetchone()
                if row is None:
                    return None
                detail = json.loads(row["detail_json"] or "{}")
                # Nothing to reinforce for a memory Ombre does not hold yet: it leaves the outbox now.
                touchable = bool(detail.get("projected") and detail.get("bucket_id"))
                conn.execute(
                    "UPDATE memory_surface_log SET committed=1, touched=? WHERE user_message_id=? AND mode='on' "
                    "AND decision='surfaced'", (0 if touchable else 1, int(user_message_id)))
                conn.execute(
                    "INSERT INTO memory_surface_cooldown (ref, surfaced_turn, surfaced_message_id, surfaced_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(ref) DO UPDATE SET surfaced_turn=excluded.surfaced_turn, "
                    "surfaced_message_id=excluded.surfaced_message_id, surfaced_at=excluded.surfaced_at",
                    (row["ref"], int(detail.get("turn") or 0), int(user_message_id),
                     now.strftime("%Y-%m-%d %H:%M:%S")),
                )
        finally:
            conn.close()
    except Exception:
        _LOG.exception("memory surface commit failed")
        return None
    thread = threading.Thread(target=flush_touches, args=(db_path,), daemon=True, name="memory-surface-touch")
    thread.start()
    if wait:
        thread.join()
    return row["ref"]


def flush_touches(db_path: str | Path, *, runtime: Any = None) -> int:
    """Touch every committed memory whose Ombre touch has not landed yet (the outbox).

    Committing and touching cannot be one transaction (Ombre is another process),
    so a crash or an Ombre outage between them leaves the row ``touched=0``; the
    next flush retries it, up to ``MAX_TOUCH_ATTEMPTS``. Returns how many landed.
    Never raises.
    """
    with _FLUSH_LOCK:
        return _flush_touches(db_path, runtime)


def _flush_touches(db_path: str | Path, runtime: Any) -> int:
    from tools import memory_kernel_projection as projection
    try:
        if runtime is None:
            runtime = projection.OmbreRuntime.from_env()
        if runtime is None:
            return 0  # no Ombre runtime configured: rows wait, attempts are not spent
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                ensure_schema(conn)
                rows = conn.execute(
                    "SELECT id, detail_json FROM memory_surface_log WHERE mode='on' AND committed=1 "
                    "AND touched=0 AND touch_attempts < ? ORDER BY id", (MAX_TOUCH_ATTEMPTS,)).fetchall()
            ids = [r["id"] for r in rows]
            buckets = sorted({str(json.loads(r["detail_json"] or "{}").get("bucket_id") or "") for r in rows} - {""})
            if not ids:
                return 0
            try:
                [result] = runtime.batch([{"op": "touch", "bucket_ids": buckets}])
                landed = not isinstance(result, projection.RequestFailed)
            except Exception as exc:
                _LOG.warning("memory surface touch failed: %s", type(exc).__name__)
                landed = False
            marks = ",".join("?" for _ in ids)
            with conn:
                if landed:
                    conn.execute(f"UPDATE memory_surface_log SET touched=1 WHERE id IN ({marks})", ids)
                else:
                    conn.execute(f"UPDATE memory_surface_log SET touch_attempts=touch_attempts+1 "
                                 f"WHERE id IN ({marks})", ids)
            return len(ids) if landed else 0
        finally:
            conn.close()
    except Exception:
        _LOG.exception("memory surface touch outbox failed")
        return 0


REVIEW_FILTERS = ("all", "unlabeled", "labeled", "surfaced", "blocked")
REVIEW_TEXT_CHARS = 300


def _ro_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _memory_previews(db_path: Path, refs: set[str], pool_loader=None) -> dict[str, dict[str, str]]:
    """Current text of the memories a review page names (retired ones included: it is history)."""
    if not refs:
        return {}
    if pool_loader is not None:
        items = pool_loader(db_path)
    else:
        from tools import memory_kernel_read as mkr
        items = mkr.memory_items(db_path, mode=mkr.EVERY_STORED)
    return {
        i["ref"]: {"ref": i["ref"], "date": str(i.get("created_at") or "")[:10],
                   "type": str(i.get("type") or "MEMORY"),
                   "text": str(i.get("content") or "")[:REVIEW_TEXT_CHARS]}
        for i in items if i.get("ref") in refs
    }


def _review_row(row: sqlite3.Row, message: str, label: Optional[sqlite3.Row],
                previews: dict[str, dict[str, str]]) -> dict[str, Any]:
    detail = json.loads(row["detail_json"] or "{}")
    trail = detail.get("trail") or []
    top = trail[0] if trail else None
    top_ref = row["ref"] or (top[0] if top else None)
    runner_up = detail.get("runner_up")
    return {
        "id": row["id"],
        "user_message_id": row["user_message_id"],
        "created_at": row["created_at"],
        "mode": row["mode"],
        "decision": row["decision"],
        "reason": row["reason"],
        "verdict": top[1] if top else None,
        "evidence": top[2] if top else None,
        "committed": bool(row["committed"]),
        "message": message[:REVIEW_TEXT_CHARS],
        "clues": list(detail.get("words") or []),
        "chat_df": dict(detail.get("chat_df") or {}),
        "matched": list(detail.get("matched") or []),
        "top": previews.get(top_ref) if top_ref else None,
        "runner_up": previews.get(runner_up) if runner_up else None,
        "label": None if label is None else {
            "label": label["label"], "note": label["note"] or "", "expected_ref": label["expected_ref"],
            "labeled_at": label["labeled_at"],
        },
    }


def review_rows(db_path: str | Path, *, limit: int = 50, before_id: Optional[int] = None,
                only: str = "all", pool_loader=None) -> dict[str, Any]:
    """Route-log decisions newest first, each with her words, the memories involved and its label."""
    if only not in REVIEW_FILTERS:
        raise ValueError(f"unknown filter {only!r}")
    limit = max(1, min(int(limit), 200))
    conn = _ro_conn(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_surface_log'").fetchone():
            return {"rows": [], "counts": {"total": 0, "labeled": 0, **{k: 0 for k in LABELS}}, "next_before": None}
        has_labels = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memory_surface_labels'").fetchone())
        where, args = ["1=1"], []
        if before_id is not None:
            where.append("l.id < ?")
            args.append(int(before_id))
        if only == "surfaced":
            where.append("l.decision='surfaced'")
        elif only == "blocked":
            where.append("l.decision='none' AND l.reason='gate_closed'")
        if has_labels and only in ("labeled", "unlabeled"):
            where.append(("" if only == "labeled" else "NOT ") +
                         "EXISTS (SELECT 1 FROM memory_surface_labels b WHERE b.log_id=l.id)")
        elif only == "labeled":
            where.append("0")
        rows = conn.execute(
            f"SELECT l.* FROM memory_surface_log l WHERE {' AND '.join(where)} ORDER BY l.id DESC LIMIT ?",
            (*args, limit)).fetchall()
        ids = [r["id"] for r in rows]
        marks = ",".join("?" for _ in ids)
        labels = {}
        if has_labels and ids:
            labels = {r["log_id"]: r for r in conn.execute(
                f"SELECT * FROM memory_surface_labels WHERE log_id IN ({marks})", ids)}
        messages = {}
        msg_ids = [r["user_message_id"] for r in rows]
        if msg_ids:
            mmarks = ",".join("?" for _ in msg_ids)
            messages = {r[0]: str(r[1] or "") for r in conn.execute(
                f"SELECT id, content FROM chat_messages WHERE id IN ({mmarks})", msg_ids)}
        refs: set[str] = set()
        for r in rows:
            detail = json.loads(r["detail_json"] or "{}")
            trail = detail.get("trail") or []
            refs.update(x for x in (r["ref"], trail[0][0] if trail else None, detail.get("runner_up")) if x)
        previews = _memory_previews(Path(db_path), refs, pool_loader)
        counts = {"total": conn.execute("SELECT COUNT(*) FROM memory_surface_log").fetchone()[0]}
        counts.update({k: 0 for k in LABELS})
        if has_labels:
            for label, n in conn.execute("SELECT label, COUNT(*) FROM memory_surface_labels GROUP BY label"):
                counts[label] = n
        counts["labeled"] = sum(counts[k] for k in LABELS)
        return {
            "rows": [_review_row(r, messages.get(r["user_message_id"], ""), labels.get(r["id"]), previews)
                     for r in rows],
            "counts": counts,
            "next_before": ids[-1] if len(ids) == limit else None,
        }
    finally:
        conn.close()


def set_label(db_path: str | Path, *, log_id: int, label: str, note: str = "",
              expected_ref: Optional[str] = None, now: Optional[datetime] = None,
              pool_loader=None) -> Optional[dict[str, Any]]:
    """Record the owner's verdict on one decision (``clear`` removes it).

    ``ok``: the decision was right. ``missed``: a memory should have surfaced
    (optionally which, ``expected_ref``). ``wrong``: what surfaced, or was about
    to, should not have. The decision is snapshotted so the label keeps meaning
    after the route log is pruned. Returns the labeled review row, or None when
    the decision does not exist.
    """
    if label not in LABELS + ("clear",):
        raise ValueError(f"unknown label {label!r}")
    now = now or _beijing_now()
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            ensure_schema(conn)
            row = conn.execute("SELECT * FROM memory_surface_log WHERE id=?", (int(log_id),)).fetchone()
            if row is None:
                return None
            if label == "clear":
                conn.execute("DELETE FROM memory_surface_labels WHERE log_id=?", (int(log_id),))
            else:
                message = conn.execute("SELECT content FROM chat_messages WHERE id=?",
                                       (row["user_message_id"],)).fetchone()
                detail = json.loads(row["detail_json"] or "{}")
                trail = detail.get("trail") or []
                refs = {x for x in (row["ref"], trail[0][0] if trail else None, detail.get("runner_up"),
                                    expected_ref) if x}
                snapshot = _review_row(row, str(message[0] if message else ""), None,
                                       _memory_previews(Path(db_path), refs, pool_loader))
                snapshot.pop("label", None)
                if expected_ref:
                    snapshot["expected"] = _memory_previews(Path(db_path), {expected_ref}, pool_loader).get(
                        expected_ref)
                conn.execute(
                    "INSERT INTO memory_surface_labels (log_id, user_message_id, label, expected_ref, note, "
                    "snapshot_json, labeled_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(log_id) DO UPDATE SET "
                    "label=excluded.label, expected_ref=excluded.expected_ref, note=excluded.note, "
                    "snapshot_json=excluded.snapshot_json, labeled_at=excluded.labeled_at",
                    (int(log_id), int(row["user_message_id"]), label, expected_ref or None,
                     str(note or "")[:500], json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                     now.strftime("%Y-%m-%d %H:%M:%S")),
                )
    finally:
        conn.close()
    page = review_rows(db_path, before_id=int(log_id) + 1, limit=1, pool_loader=pool_loader)
    return page["rows"][0] if page["rows"] else None


def recent_log(db_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """Newest route-log rows, for review."""
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memory_surface_log ORDER BY id DESC LIMIT ?", (int(limit),))]
    finally:
        conn.close()
