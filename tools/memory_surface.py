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
(capacity swap, cold rebuild, regeneration) reuses the first decision instead
of drawing a different memory once the first one is cooling down.

Read-only on memories: the Kernel and Ombre are never written. The only side
effects are the two surface tables in memories.db and an Ombre ``touch`` of the
surfaced bucket (reinforcement, off the request path).
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

_LOG = logging.getLogger("hayagarden.memory_surface")

USER_AUTHORS = ("hayana", "haya", "user")
RRF_K = 60
POOL_TYPES = frozenset({"MEMORY", "MISS"})  # FACTs belong to the cold-start list
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
    enabled: bool = True
    cooldown_turns: int = 60
    # At least two clues (words rare in their chat) found in the memory.
    evidence_min: float = 6.0
    min_words: int = 2
    chat_df_max: int = 20
    recent_days: int = 2


@dataclass
class SurfaceResult:
    decision: str  # surfaced | none | skipped | failed
    reason: str
    ref: str = ""
    block: str = ""
    items: list[dict[str, str]] = field(default_factory=list)
    bucket_id: str = ""
    projected: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def surfaced(self) -> bool:
        return self.decision == "surfaced" and bool(self.block)


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
        decision TEXT NOT NULL,
        reason TEXT NOT NULL,
        ref TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_surface_log_msg ON memory_surface_log(user_message_id)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def load_config() -> SurfaceConfig:
    try:
        import config_store
    except Exception:  # tests / tools without the runtime config
        return SurfaceConfig()
    return SurfaceConfig(
        enabled=config_store.get_bool("MEMORY_SURFACE_ENABLED", True),
        cooldown_turns=config_store.get_int("MEMORY_SURFACE_COOLDOWN_TURNS", 60),
        evidence_min=config_store.get_float("MEMORY_SURFACE_EVIDENCE_MIN", 6.0),
        chat_df_max=config_store.get_int("MEMORY_SURFACE_CHAT_DF_MAX", 20),
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


def chat_frequency(conn: sqlite3.Connection, words: list[str], window: int = CHAT_DF_WINDOW) -> dict[str, int]:
    """In how many of the latest ``window`` chat messages each word appears."""
    out = {}
    for word in words:
        out[word] = int(conn.execute(
            "SELECT COUNT(*) FROM (SELECT content FROM chat_messages ORDER BY id DESC LIMIT ?) "
            "WHERE instr(lower(content), ?) > 0", (int(window), word)).fetchone()[0] or 0)
    return out


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


def keyword_list(pool: list[dict[str, Any]], words: list[str]) -> tuple[list[str], dict[str, float], dict[str, list[str]]]:
    """Refs ranked by keyword evidence: summed IDF of her words found in each memory."""
    n = len(pool)
    if not n or not words:
        return [], {}, {}
    norms = {item["ref"]: _norm(item.get("content")).lower() for item in pool}
    df = {w: sum(1 for text in norms.values() if w in text) for w in words}
    idf = {}
    for w, count in df.items():
        if count == 0 or count / n > COMMON_SHARE:
            continue
        idf[w] = math.log((n - count + 0.5) / (count + 0.5) + 1.0)
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


def _previous(conn: sqlite3.Connection, user_message_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT decision, reason, ref, detail_json FROM memory_surface_log WHERE user_message_id=? "
        "AND decision IN ('surfaced','none','skipped') ORDER BY id ASC LIMIT 1",
        (int(user_message_id),),
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


def _log(conn: sqlite3.Connection, user_message_id: int, result: SurfaceResult, now: datetime) -> None:
    conn.execute(
        "INSERT INTO memory_surface_log (user_message_id, created_at, decision, reason, ref, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (int(user_message_id), now.strftime("%Y-%m-%d %H:%M:%S"), result.decision, result.reason,
         result.ref or None, json.dumps(result.detail, ensure_ascii=False, sort_keys=True)),
    )


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
    """Pick 0 or 1 memory for this user message and record why. Never raises."""
    config = config or load_config()
    now = now or _beijing_now()
    db_path = Path(db_path)
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        _LOG.warning("memory surface unavailable: %s", type(exc).__name__)
        return SurfaceResult("failed", "db_unavailable")
    try:
        with conn:
            ensure_schema(conn)
            previous = _previous(conn, user_message_id)
        if previous is not None:
            conn.close()
            return _replay(previous, db_path, pool_loader)
        result = _decide(conn, user_message_id=user_message_id, user_text=user_text,
                         context_text=context_text, db_path=db_path, config=config,
                         pool_loader=pool_loader, ombre=ombre, now=now)
    except Exception as exc:
        _LOG.exception("memory surface failed; turn continues without it")
        result = SurfaceResult("failed", "error", detail={"exc": type(exc).__name__})
    try:
        with conn:
            if result.surfaced:
                conn.execute(
                    "INSERT INTO memory_surface_cooldown (ref, surfaced_turn, surfaced_message_id, surfaced_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(ref) DO UPDATE SET surfaced_turn=excluded.surfaced_turn, "
                    "surfaced_message_id=excluded.surfaced_message_id, surfaced_at=excluded.surfaced_at",
                    (result.ref, int(result.detail.get("turn") or 0), int(user_message_id),
                     now.strftime("%Y-%m-%d %H:%M:%S")),
                )
            _log(conn, user_message_id, result, now)
    except Exception:
        _LOG.exception("memory surface log write failed")
    finally:
        conn.close()
    return result


def _pool(db_path: Path, pool_loader) -> list[dict[str, Any]]:
    if pool_loader is not None:
        return pool_loader(db_path)
    from tools import memory_kernel_read as mkr
    return mkr.memory_items(db_path, mode=mkr.AUTOMATIC)


def _replay(previous: sqlite3.Row, db_path: Path, pool_loader) -> SurfaceResult:
    """The same user message gets the same answer, even while that memory is cooling down."""
    detail = json.loads(previous["detail_json"] or "{}")
    if previous["decision"] != "surfaced":
        return SurfaceResult(previous["decision"], previous["reason"], detail=dict(detail, replayed=True))
    item = next((i for i in _pool(db_path, pool_loader) if i.get("ref") == previous["ref"]), None)
    if item is None:  # retired or revised since: do not resurrect it
        return SurfaceResult("none", "replayed_ref_gone", detail={"replayed": True})
    block, items = render(item)
    return SurfaceResult("surfaced", previous["reason"], ref=previous["ref"], block=block, items=items,
                         bucket_id=str(item.get("bucket_id") or ""), projected=bool(item.get("projected")),
                         detail=dict(detail, replayed=True))


def _decide(conn, *, user_message_id, user_text, context_text, db_path, config, pool_loader, ombre, now):
    if not config.enabled:
        return SurfaceResult("skipped", "disabled")
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
    chat_df = chat_frequency(conn, words[:12])
    clues = [w for w in words[:12] if chat_df.get(w, 0) <= config.chat_df_max]
    if not clues:
        return SurfaceResult("none", "no_clues", detail={"words": words[:12], "chat_df": chat_df})
    words = clues
    turn = _turn_number(conn, user_message_id)
    cutoff = (now - timedelta(days=config.recent_days)).strftime("%Y-%m-%d %H:%M:%S")
    everything = _pool(db_path, pool_loader)
    # Recent memories are still in the day's context; FACTs go through cold start.
    pool = [i for i in everything if str(i.get("type") or "MEMORY") in POOL_TYPES
            and str(i.get("created_at") or "") < cutoff and i.get("ref")]
    detail: dict[str, Any] = {"turn": turn, "words": words[:12], "chat_df": chat_df, "pool": len(pool)}
    if not pool:
        return SurfaceResult("none", "empty_pool", detail=detail)

    by_ref = {i["ref"]: i for i in pool}
    kw_ranked, evidence, matched = keyword_list(pool, words)
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
    fused = rrf(ombre_ranked, kw_ranked)
    order = sorted(fused, key=lambda ref: (-fused[ref], ref))
    cooling = _cooling(conn, turn, config.cooldown_turns)
    context_norm = _norm(context_text)

    trail = []
    chosen = None
    for ref in order:
        score = round(evidence.get(ref, 0.0), 2)
        if score < config.evidence_min or len(matched.get(ref, ())) < config.min_words:
            trail.append([ref, "below_evidence", score])
            continue
        if ref in cooling:
            trail.append([ref, "cooldown", score])
            continue
        head = _norm(by_ref[ref].get("content"))[:24]
        if head and head in context_norm:
            trail.append([ref, "in_context", score])
            continue
        chosen = ref
        trail.append([ref, "chosen", score])
        break
    detail["trail"] = trail[:6]
    if chosen is None:
        return SurfaceResult("none", "gate_closed" if order else "no_candidates", detail=detail)
    detail["matched"] = matched.get(chosen, [])
    detail["ombre_rank"] = ombre_ranked.index(chosen) + 1 if chosen in ombre_ranked else None
    item = by_ref[chosen]
    block, items = render(item)
    return SurfaceResult("surfaced", "gate_passed", ref=chosen, block=block, items=items,
                         bucket_id=str(item.get("bucket_id") or ""), projected=bool(item.get("projected")),
                         detail=detail)


def touch(result: SurfaceResult) -> None:
    """Reinforce the surfaced bucket in Ombre (projected Kernel memories only), off the request path."""
    if not (result.surfaced and result.projected and result.bucket_id):
        return
    try:
        from tools import memory_kernel_read as mkr
        mkr.touch_buckets([result.bucket_id])
    except Exception:
        _LOG.exception("memory surface touch failed")


def recent_log(db_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """Newest route-log rows, for review."""
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memory_surface_log ORDER BY id DESC LIMIT ?", (int(limit),))]
    finally:
        conn.close()
