"""B-4: the one read path for memory types (MEMORY / FACT / MISS).

With the cutover switch on (``memory.kernel_write``, see memory_kernel_switch)
memories are written to the Memory Kernel and projected into Ombre, so posts no
longer hold them. Every reader of memory types goes through this module:

- **Kernel** is the authority. ``list_current`` / ``get_current`` /
  ``get_current_by_bucket_ids`` / ``search_current`` return the current head of
  each lineage, typed from ``structured_value.memory``.
- **Ombre** only ranks. ``search`` asks Ombre for bucket ids, maps them to
  lineages through the projection ledger and returns the Kernel's head, so a
  projection that lags never puts stale text into a prompt.
- **Gaps are filled:** heads not yet projected are matched lexically; if Ombre
  cannot be asked at all every head is; posts that fell back on a Kernel write
  failure (``memory_kernel_backfill`` not done) are read until backfilled.
- **Duplicates are dropped:** by stable identity, and by normalised content
  against non-memory posts (the old cleaner held DIARY into Ombre and B-1
  ingested those buckets, so a Kernel head can be a copy of a diary).

Items are posts-shaped dicts so existing callers keep working, plus ``ref``: a
stable, source-qualified id, ``post:<id>`` or ``kernel:<lineage_id>``.

This module never writes and never touches Ombre: reinforcement is the
caller's explicit ``touch_buckets`` on what it actually used.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from tools import memory_kernel_projection as projection
from tools.memory_kernel_store import MEMORY_TYPES, kernel_db_path
from tools.memory_kernel_switch import cutover_enabled, cutover_posts_db  # noqa: F401 (re-exported)

BEIJING = timezone(timedelta(hours=8))
# Ombre bucket kinds that are memories. Plans, letters (lockable) and anything
# newer stay Ombre's own surfaces: posts never held them, so neither do these reads.
MEMORY_OMBRE_TYPES = frozenset({"", "dynamic", "permanent", "archived"})
# Sentinel: the default Ombre search (ombre_adapter.search_bucket_ids).
DEFAULT = object()


def posts_db_of(conn: sqlite3.Connection) -> Path | None:
    """The file behind a caller's posts connection (None for in-memory)."""
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return Path(path)
    return None


def norm(text: Any) -> str:
    folded = unicodedata.normalize("NFKC", re.sub(r"\[\[|\]\]", "", str(text or ""))).lower()
    return "".join(ch for ch in folded if ch.isalnum())


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(v) for v in (value or [])]


def beijing(value: Any) -> str:
    """posts' own clock: Beijing local, 'YYYY-MM-DD HH:MM:SS'."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BEIJING).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- Kernel heads

def _item(row: sqlite3.Row, first_created: str, ledger: dict[str, Any] | None) -> dict[str, Any] | None:
    value = json.loads(row["structured_value"]) if row["structured_value"] else {}
    value = value if isinstance(value, dict) else {}
    memory = value.get("memory") or {}
    legacy = value.get("legacy") or {}
    if str(legacy.get("ombre_type") or "") not in MEMORY_OMBRE_TYPES:
        return None
    lineage = row["lineage_id"]
    tags = _tags(value.get("tags"))
    # Heads imported before memory semantics were recorded carry only representation.
    mtype = memory.get("type") or ("FACT" if row["representation"] == "fact" else "MEMORY")
    bucket_id = (ledger or {}).get("bucket_id") or projection.bucket_id_for(lineage)
    projected = bool(ledger) and ledger.get("status") == "projected" \
        and ledger.get("projected_state_id") == row["state_id"]
    return {
        "id": f"kernel:{lineage}",
        "ref": f"kernel:{lineage}",
        "source": "kernel",
        "type": mtype,
        "content": row["content"],
        "author": memory.get("author") or "",
        "layer": memory.get("layer") or ("core" if legacy.get("ombre_type") == "permanent" else "recent"),
        "tags": ",".join(tags),
        "tag_list": tags,
        "pinned": 1 if (memory.get("pinned") or legacy.get("pinned")) else 0,
        "importance": value.get("importance") or 0,
        "recall_count": 0,  # reinforcement is Ombre's (touch_buckets), not a posts counter
        "valence": value.get("valence"),
        "arousal": value.get("arousal"),
        "summary_title": value.get("title") or "",
        "resolved": 0 if row["status"] == "active" else 1,
        "created_at": beijing(first_created),
        "updated_at": beijing(row["created_at"]),
        "lineage_id": lineage,
        "state_id": row["state_id"],
        "bucket_id": bucket_id,
        "projected": projected,
    }


def _heads(kernel_db: Path) -> list[dict[str, Any]]:
    """Every lineage with exactly one content head (as projection.select_heads)."""
    if not kernel_db.is_file():
        return []
    conn = _ro(kernel_db)
    try:
        first: dict[str, str] = {}
        for lineage, created in conn.execute(
            "SELECT lineage_id, created_at FROM memory_kernel_state "
            "WHERE lineage_id IS NOT NULL ORDER BY rowid"
        ):
            first.setdefault(lineage, created)
        rows = conn.execute(
            "SELECT * FROM memory_kernel_state s WHERE s.lineage_id IS NOT NULL "
            "AND s.content IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM memory_kernel_delta d WHERE d.before_ref = s.state_id) ORDER BY s.rowid"
        ).fetchall()
    finally:
        conn.close()
    ledger: dict[str, dict[str, Any]] = {}
    projection_db = projection.state_path_for(kernel_db)
    if projection_db.is_file():
        pconn = _ro(projection_db)
        try:
            ledger = {r["lineage_id"]: dict(r) for r in pconn.execute(
                "SELECT lineage_id, bucket_id, status, projected_state_id FROM projection_lineage")}
        except sqlite3.Error:
            ledger = {}
        finally:
            pconn.close()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["lineage_id"]] = counts.get(row["lineage_id"], 0) + 1
    items = []
    for row in rows:
        if counts[row["lineage_id"]] != 1:
            continue  # branched: no single current version to show
        item = _item(row, first[row["lineage_id"]], ledger.get(row["lineage_id"]))
        if item is not None:
            items.append(item)
    return items


def _wanted(item: dict[str, Any], *, memory_type: Any, layer: Any, tag: Any, active_only: bool) -> bool:
    if active_only and item["resolved"]:
        return False
    if memory_type and item["type"] not in ((memory_type,) if isinstance(memory_type, str) else tuple(memory_type)):
        return False
    if layer and item["layer"] != layer:
        return False
    if tag and not any(tag in t for t in item["tag_list"]):
        return False
    return True


def _newest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda i: (i["created_at"], str(i["id"])), reverse=True)


def list_current(kernel_db: Path | None = None, *, memory_type: Any = None, layer: str | None = None,
                 tag: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
    """Current heads, newest first; type / layer / tag from structured_value.memory."""
    heads = _heads(Path(kernel_db or kernel_db_path()))
    return _newest_first([h for h in heads if _wanted(
        h, memory_type=memory_type, layer=layer, tag=tag, active_only=active_only)])


def get_current(lineage_ids: Iterable[str], kernel_db: Path | None = None) -> dict[str, dict[str, Any] | None]:
    by_lineage = {h["lineage_id"]: h for h in _heads(Path(kernel_db or kernel_db_path()))}
    return {lineage: by_lineage.get(lineage) for lineage in lineage_ids}


def get_current_by_bucket_ids(bucket_ids: Iterable[str], kernel_db: Path | None = None) -> dict[str, dict[str, Any]]:
    """Ombre bucket id -> the Kernel head it projects. Unknown buckets are left out."""
    by_bucket = {h["bucket_id"]: h for h in _heads(Path(kernel_db or kernel_db_path()))}
    return {b: by_bucket[b] for b in bucket_ids if b in by_bucket}


def _words(query: Any) -> list[str]:
    return [w for w in str(query or "").replace("，", " ").replace(",", " ").split() if w]


def lexical(items: list[dict[str, Any]], query: Any) -> list[dict[str, Any]]:
    """memory_tool.search_memories' semantics: all words, else most words; content + tags."""
    words = _words(query)
    if not words:
        return []
    def hits(item):
        hay = f"{item.get('content') or ''} {item.get('tags') or ''}"
        return sum(1 for w in words if w in hay)
    scored = [(hits(i), i) for i in items]
    every = [i for n, i in scored if n == len(words)]
    if every:
        return every
    if len(words) == 1:
        return []
    return [i for n, i in sorted(scored, key=lambda p: -p[0]) if n > 0]


def search_current(query: Any, kernel_db: Path | None = None, *, memory_type: Any = None,
                   active_only: bool = True, unprojected_only: bool = False) -> list[dict[str, Any]]:
    heads = [h for h in list_current(kernel_db, memory_type=memory_type, active_only=active_only)
             if not (unprojected_only and h["projected"])]
    return lexical(heads, query)


# ------------------------------------------------------------- posts sources

def non_memory_clause(column: str = "type") -> str:
    return f"COALESCE({column}, '') NOT IN ({', '.join(repr(t) for t in MEMORY_TYPES)})"


def pending_fallback_posts(posts_db: Path | None, *, memory_type: Any = None,
                           active_only: bool = True) -> list[dict[str, Any]]:
    """Memory posts left by a failed Kernel write that backfill has not taken yet."""
    if not posts_db or not Path(posts_db).is_file():
        return []
    conn = _ro(Path(posts_db))
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='memory_kernel_backfill'").fetchone():
            return []
        rows = conn.execute(
            "SELECT p.* FROM posts p JOIN memory_kernel_backfill b ON b.post_id = p.id "
            f"WHERE b.done_at IS NULL AND p.type IN ({', '.join('?' * len(MEMORY_TYPES))})",
            MEMORY_TYPES).fetchall()
    finally:
        conn.close()
    wanted = (memory_type,) if isinstance(memory_type, str) else tuple(memory_type or MEMORY_TYPES)
    items = []
    for row in rows:
        item = dict(row)
        if item["type"] not in wanted or (active_only and item.get("resolved")):
            continue
        item.update(ref=f"post:{item['id']}", source="posts_fallback",
                    tag_list=_tags(item.get("tags")), created_at=str(item.get("created_at") or ""))
        items.append(item)
    return items


def non_memory_norms(posts_db: Path | None) -> set[str]:
    """Normalised content of every non-memory post (for dropping Kernel copies)."""
    if not posts_db or not Path(posts_db).is_file():
        return set()
    conn = _ro(Path(posts_db))
    try:
        return {norm(r[0]) for r in conn.execute(
            f"SELECT content FROM posts WHERE {non_memory_clause()}") if r[0]} - {""}
    finally:
        conn.close()


def dedupe(items: Iterable[dict[str, Any]], *, drop_norms: set[str] = frozenset()) -> list[dict[str, Any]]:
    """Keep the first of each stable identity and of each normalised content.

    Order the input by authority: non-memory posts, then Kernel, then fallback posts.
    Memory items whose content equals a non-memory post (``drop_norms``) are dropped.
    """
    seen_ref, seen_norm, kept = set(), set(), []
    for item in items:
        ref = item.get("ref") or f"post:{item.get('id')}"
        key = norm(item.get("content"))
        is_memory = item.get("type") in MEMORY_TYPES
        if ref in seen_ref or (key and key in seen_norm) or (is_memory and key in drop_norms):
            continue
        seen_ref.add(ref)
        if key:
            seen_norm.add(key)
        kept.append(item)
    return kept


def memory_items(posts_db: Path | None, kernel_db: Path | None = None, *, memory_type: Any = None,
                 layer: str | None = None, tag: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
    """Every current memory (Kernel heads + pending fallback posts), newest first."""
    kernel = list_current(kernel_db, memory_type=memory_type, layer=layer, tag=tag, active_only=active_only)
    fallback = [i for i in pending_fallback_posts(posts_db, memory_type=memory_type, active_only=active_only)
                if _wanted(i, memory_type=None, layer=layer, tag=tag, active_only=False)]
    return _newest_first(dedupe(kernel + fallback, drop_norms=non_memory_norms(posts_db)))


def search(query: Any, posts_db: Path | None, kernel_db: Path | None = None, *, memory_type: Any = None,
           active_only: bool = True, limit: int = 20, ombre: Any = DEFAULT) -> list[dict[str, Any]]:
    """Memories for a query: Ombre-ranked heads first, then the gaps, newest first.

    ``ombre(query, limit)`` returns ranked bucket ids, or None when Ombre could not
    be asked; then every Kernel head is matched lexically so nothing is lost.
    """
    if not str(query or "").strip():
        return []
    kernel_db = Path(kernel_db or kernel_db_path())
    if ombre is DEFAULT:
        from tools import ombre_adapter
        ombre = ombre_adapter.search_bucket_ids
    try:
        ranked = ombre(str(query), max(limit, 10)) if ombre else None
    except Exception:
        ranked = None
    heads = list_current(kernel_db, memory_type=memory_type, active_only=active_only)
    by_bucket = {h["bucket_id"]: h for h in heads}
    first = [by_bucket[b] for b in (ranked or []) if b in by_bucket]
    gaps = lexical([h for h in heads if ranked is None or not h["projected"]], query)
    fallback = lexical(pending_fallback_posts(posts_db, memory_type=memory_type, active_only=active_only), query)
    merged = dedupe(first + _newest_first(gaps) + _newest_first(fallback), drop_norms=non_memory_norms(posts_db))
    return merged[:max(0, int(limit))]


# ------------------------------------------------- recipes for the callers

def kernel_posts_db(conn: sqlite3.Connection) -> Path | None:
    """The posts file when the cutover switch is on for this connection, else None."""
    return cutover_posts_db(conn)


def facts(posts_db: Path, kernel_db: Path | None = None, *, limit: int = 15) -> list[dict[str, Any]]:
    """Cold-start FACTs: most important first, then newest (posts: importance, id DESC)."""
    items = memory_items(posts_db, kernel_db, memory_type="FACT")
    items.sort(key=lambda i: (int(i.get("importance") or 0), i["created_at"]), reverse=True)
    return items[:limit]


def long_term(posts_db: Path, kernel_db: Path | None = None, *, limit: int = 3) -> list[dict[str, Any]]:
    """layer 'long-term' across every type: non-memory posts plus Kernel memories."""
    conn = _ro(posts_db)
    try:
        rows = [dict(r, ref=f"post:{r['id']}") for r in conn.execute(
            "SELECT id, type, content, created_at FROM posts WHERE layer='long-term' AND resolved=0 "
            f"AND {non_memory_clause()} ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()
    merged = dedupe(rows + memory_items(posts_db, kernel_db, layer="long-term"))
    return _newest_first_any(merged)[:limit]


def recent_memos(posts_db: Path, kernel_db: Path | None = None, *, hours: int = 24, limit: int = 4,
                 now: datetime | None = None) -> list[dict[str, Any]]:
    """MEMORY tagged memo in the last ``hours`` (posts: created_at >= now+8h-24h), newest first."""
    since = ((now or datetime.now(timezone.utc)).astimezone(BEIJING).replace(tzinfo=None)
             - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    items = memory_items(posts_db, kernel_db, memory_type="MEMORY", tag="memo")
    return [i for i in items if i["created_at"] >= since][:limit]


def _newest_first_any(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda i: (str(i.get("created_at") or ""), str(i.get("id"))), reverse=True)


def recall_candidates(posts_db: Path, query: str, kernel_db: Path | None = None, *, use_ombre: bool = True,
                      ombre_limit: int = 2, ombre: Any = DEFAULT) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Automatic recall: (memories for the caller's own lexical scoring, Ombre-ranked memories).

    The lexical side holds what Ombre cannot find yet (unprojected heads, pending
    fallback posts), or every memory when Ombre is off or could not be asked.
    """
    items = memory_items(posts_db, kernel_db)
    ranked = None
    if use_ombre and str(query or "").strip():
        if ombre is DEFAULT:
            from tools import ombre_adapter
            ombre = ombre_adapter.search_bucket_ids
        try:
            ranked = ombre(str(query), max(ombre_limit * 3, 6))
        except Exception:
            ranked = None
    if ranked is None:
        return items, []
    by_bucket = {i["bucket_id"]: i for i in items if i.get("source") == "kernel"}
    hits = [by_bucket[b] for b in ranked if b in by_bucket][:ombre_limit]
    lexical_side = [i for i in items if not i.get("projected")]
    return lexical_side, hits


def merge_search(memory: list[dict[str, Any]], posts_rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Explicit search: memories (already ranked) then the caller's non-memory posts."""
    kept = {id(i) for i in dedupe(list(posts_rows) + list(memory))}
    return ([i for i in memory if id(i) in kept] + [p for p in posts_rows if id(p) in kept])[:limit]


# ------------------------------------------------------------------ touch

def touch_buckets(bucket_ids: Iterable[str], *, runtime: Any = DEFAULT, wait: bool = False) -> threading.Thread | None:
    """Reinforce what was actually used, through Ombre's own BucketManager.touch_many.

    Off the request path (a worker run costs ~1.7 s). Without OMBRE_RUNTIME_* there
    is no reinforcement: the HTTP search is read-only and never heats anything.
    """
    ids = sorted({str(b) for b in bucket_ids if b})
    if not ids:
        return None
    if runtime is DEFAULT:
        runtime = projection.OmbreRuntime.from_env()
    if runtime is None:
        return None

    def run() -> None:
        try:
            runtime.batch([{"op": "touch", "bucket_ids": ids}])
        except Exception:
            pass

    thread = threading.Thread(target=run, daemon=True, name="memory-kernel-touch")
    thread.start()
    if wait:
        thread.join()
    return thread
