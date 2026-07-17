#!/usr/bin/env python3.11
"""
私人梦史 · 潜梦库（dream_latents）

类型：place / object / figure / action / phrase / sensation
来源：memory / dream / fallback_dream / synthetic

用 recurrence + last_used_at 代理 novelty，不另建 novelty 列。
"""
from __future__ import annotations

import random
import re
import sqlite3
from datetime import datetime, timedelta

DB_PATH = '/opt/frontend/memories.db'

VALID_TYPES = ('place', 'object', 'figure', 'action', 'phrase', 'sensation')
VALID_ORIGINS = ('memory', 'dream', 'fallback_dream', 'synthetic')

MOTIF_EXTRACTION_WEIGHT = {
    'normal_dream': 1.0,
    'fallback_dream': 0.2,
}
MOTIF_COOLDOWN_DREAMS = 5

# 冷启动 / synthetic 种子词库
SYNTHETIC_SEED = {
    'place': [
        '没有出口的地下候车室', '室内还在下雪的房间',
        '楼梯只向下却通向天空的塔', '所有座位都朝向窗外的教室',
    ],
    'object': [
        '内部仍在下雪的玻璃杯', '一封装着影子的信',
        '被反复抄写的夜晚', '停在半空的秒针',
    ],
    'action': [
        '把日期从每张纸上撕掉', '把影子折好寄出',
        '数一段没有尽头的电梯提示音', '给不存在的门上锁',
    ],
    'sensation': [
        '潮湿的铁锈味', '指尖发凉却出汗',
        '远处持续的低鸣', '袖口突然变重',
    ],
    'figure': [
        '一扇打不开的门', '知道回程的鸟',
        '留在椅背上的外套', '电话里的呼吸',
    ],
    'phrase': [
        '根本不算是梦呀', '白天事件的拼',
        '记不清是谁先开口', '门后面还有门',
    ],
}

_BANNED_RE = re.compile(
    r'哈娅|哈雅娜|费奥多尔|费佳|微信|小红书|'
    r'今天|昨天|前天|明天|'
    r'\d{1,2}月\d{1,2}日|\d{4}年'
)


def _now():
    return datetime.utcnow() + timedelta(hours=8)


def _now_str():
    return _now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_latents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            content TEXT,
            origin TEXT,
            source_id INTEGER,
            valence REAL,
            arousal REAL,
            recurrence INTEGER DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT DEFAULT (datetime('now', '+8 hours'))
        )
        """
    )
    conn.commit()


def seed_synthetic_if_empty(conn: sqlite3.Connection) -> int:
    """库空时从静态词库入库一批 synthetic，返回新增条数。"""
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM dream_latents WHERE origin='synthetic'"
    ).fetchone()['c']
    if n > 0:
        return 0
    added = 0
    for typ, items in SYNTHETIC_SEED.items():
        for content in items:
            conn.execute(
                """
                INSERT INTO dream_latents (type, content, origin, source_id,
                    valence, arousal, recurrence, created_at)
                VALUES (?, ?, 'synthetic', NULL, 0.5, 0.4, 0, ?)
                """,
                (typ, content, _now_str()),
            )
            added += 1
    conn.commit()
    return added


def pick_synthetic(conn: sqlite3.Connection, n: int) -> list[dict]:
    if n <= 0:
        return []
    seed_synthetic_if_empty(conn)
    rows = conn.execute(
        """
        SELECT id, type, content, origin, source_id, valence, arousal, recurrence
        FROM dream_latents
        WHERE origin='synthetic' AND TRIM(COALESCE(content,'')) != ''
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_fallback_latents(conn: sqlite3.Connection, limit=5) -> list[dict]:
    """fallback 优先旧母题；冷却 7 天未用，recurrence 2~5 优先。"""
    rows = conn.execute(
        """
        SELECT id, type, content, recurrence
        FROM dream_latents
        WHERE origin IN ('dream', 'synthetic')
          AND TRIM(COALESCE(content,'')) != ''
          AND (last_used_at IS NULL
               OR last_used_at < datetime('now', '-7 days'))
        ORDER BY CASE
            WHEN recurrence BETWEEN 2 AND 5 THEN 0
            ELSE 1
        END, RANDOM()
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _decay_since_last_use(last_used_at: str | None) -> float:
    if not last_used_at:
        return 1.0
    try:
        dt = datetime.strptime(last_used_at, '%Y-%m-%d %H:%M:%S')
        days = max((_now() - dt).total_seconds() / 86400.0, 0.0)
        return min(1.0, 0.2 + days / 14.0)
    except Exception:
        return 0.5


def pick_motifs(conn: sqlite3.Connection, n: int,
                recent_dream_count: int | None = None) -> list[dict]:
    """
    抽取权重 recurrence × decay_since_last_use；
    距 last_used_at 不足 MOTIF_COOLDOWN_DREAMS 场的不参与。
    用时间近似冷却：每场梦约对应数小时，冷却窗口用最近 N 条 dream 的 created_at。
    """
    if n <= 0:
        return []
    cooldown_cutoff = None
    try:
        row = conn.execute(
            """
            SELECT created_at FROM dream_pool
            ORDER BY id DESC LIMIT 1 OFFSET ?
            """,
            (MOTIF_COOLDOWN_DREAMS - 1,),
        ).fetchone()
        if row and row['created_at']:
            cooldown_cutoff = row['created_at']
    except Exception:
        cooldown_cutoff = None

    rows = conn.execute(
        """
        SELECT id, type, content, origin, recurrence, last_used_at, valence, arousal
        FROM dream_latents
        WHERE origin IN ('dream', 'synthetic', 'memory')
          AND TRIM(COALESCE(content,'')) != ''
        """
    ).fetchall()
    candidates = []
    for r in rows:
        d = dict(r)
        if cooldown_cutoff and d.get('last_used_at'):
            if d['last_used_at'] >= cooldown_cutoff:
                continue
        rec = max(int(d.get('recurrence') or 0), 0)
        weight = (rec + 1) * _decay_since_last_use(d.get('last_used_at'))
        candidates.append((weight, d))
    if not candidates:
        return []
    # 加权无放回
    picked = []
    pool = list(candidates)
    for _ in range(min(n, len(pool))):
        weights = [max(w, 1e-6) for w, _ in pool]
        idx = random.choices(range(len(pool)), weights=weights, k=1)[0]
        picked.append(pool[idx][1])
        pool.pop(idx)
    return picked


def mark_used(conn: sqlite3.Connection, latent_ids: list[int]) -> None:
    if not latent_ids:
        return
    now = _now_str()
    for lid in latent_ids:
        conn.execute(
            """
            UPDATE dream_latents
            SET last_used_at=?, recurrence=COALESCE(recurrence,0)+1
            WHERE id=?
            """,
            (now, lid),
        )
    conn.commit()


def contains_banned(text: str) -> bool:
    if not text:
        return True
    return bool(_BANNED_RE.search(text))


def random_cut(text: str, min_len=8, max_len=25) -> str | None:
    text = re.sub(r'\s+', '', (text or '').strip())
    if len(text) < min_len:
        return None
    max_len = min(max_len, len(text))
    length = random.randint(min_len, max_len)
    if len(text) == length:
        return text
    start = random.randint(0, len(text) - length)
    return text[start:start + length]


def extract_shards(fragments: list[dict], max_shards=2) -> list[str]:
    """原文残渣：8~25 字，必须来自不同 source_id；禁姓名/日期/应用名。"""
    if not fragments or max_shards <= 0:
        return []
    order = list(fragments)
    random.shuffle(order)
    seen_sources = set()
    shards = []
    for f in order:
        sid = f.get('source_id')
        if sid in seen_sources:
            continue
        shard = random_cut(f.get('content') or '', min_len=8, max_len=25)
        if not shard or contains_banned(shard):
            continue
        shards.append(shard)
        seen_sources.add(sid)
        if len(shards) >= max_shards:
            break
    return shards


_IMAGERY_SPLIT_RE = re.compile(r'[，。！？；、\n]+')


def extract_dream_latents(dream_text: str, max_items=3) -> list[dict]:
    """从梦正文粗抽 1~3 个意象（规则版，非 LLM）。"""
    if not dream_text:
        return []
    parts = [p.strip() for p in _IMAGERY_SPLIT_RE.split(dream_text) if p.strip()]
    # 偏好带名词感的中等长度片段
    candidates = [p for p in parts if 6 <= len(p) <= 22 and not contains_banned(p)]
    if not candidates:
        candidates = [p for p in parts if 4 <= len(p) <= 28]
    if not candidates:
        return []
    random.shuffle(candidates)
    picked = candidates[:max(1, min(max_items, len(candidates)))]
    out = []
    type_cycle = ['phrase', 'object', 'place', 'sensation', 'action', 'figure']
    for i, content in enumerate(picked):
        out.append({
            'type': type_cycle[i % len(type_cycle)],
            'content': content,
        })
    return out


def insert_latents(conn: sqlite3.Connection, items: list[dict],
                   origin: str, source_id=None,
                   valence=0.5, arousal=0.4,
                   weight: float = 1.0) -> int:
    """
    回流写入。weight<1 时按概率跳过（fallback 梦用 0.2 防回路）。
    """
    if origin not in VALID_ORIGINS:
        origin = 'dream'
    added = 0
    now = _now_str()
    for it in items:
        if weight < 1.0 and random.random() > weight:
            continue
        content = (it.get('content') or '').strip()
        if not content or contains_banned(content):
            continue
        typ = it.get('type') or 'phrase'
        if typ not in VALID_TYPES:
            typ = 'phrase'
        # 去重：同 content 则 recurrence+1
        existing = conn.execute(
            'SELECT id FROM dream_latents WHERE content=? LIMIT 1',
            (content,),
        ).fetchone()
        if existing:
            conn.execute(
                'UPDATE dream_latents SET recurrence=COALESCE(recurrence,0)+1 WHERE id=?',
                (existing['id'],),
            )
        else:
            conn.execute(
                """
                INSERT INTO dream_latents
                    (type, content, origin, source_id, valence, arousal,
                     recurrence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (typ, content, origin, source_id, valence, arousal, now),
            )
            added += 1
    conn.commit()
    return added
