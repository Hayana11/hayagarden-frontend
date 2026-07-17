"""Resolve dream list rows: titles, tone, and bipolar V/A for the Moments API."""
from __future__ import annotations

import sqlite3

from tools import summary_title
from valence_scale import normalize_arousal, normalize_valence

VALID_TONES = frozenset({'vivid', 'warm', 'anxious', 'heavy', 'drifting'})

TONE_LABELS: dict[str, str] = {
    'vivid': '鲜活',
    'warm': '温柔',
    'anxious': '不安',
    'heavy': '沉重',
    'drifting': '漂浮',
}


def emotion_label_for_tone(tone: str | None) -> str:
    return TONE_LABELS.get((tone or '').strip(), '朦胧')


def _parse_tone_from_tags(tags: str | None) -> str | None:
    raw = (tags or '').strip()
    if raw in VALID_TONES:
        return raw
    if raw.startswith('dream_tone:'):
        candidate = raw.split(':', 1)[1].strip()
        return candidate if candidate in VALID_TONES else None
    return None


def _coerce_unipolar(value: object, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number == 0.0:
        return default
    return max(0.0, min(1.0, number))


def _pool_row_for_post(conn: sqlite3.Connection, content: str, created_at: str | None) -> dict | None:
    row = conn.execute(
        "SELECT valence, arousal, tone FROM dream_pool WHERE content=? ORDER BY id DESC LIMIT 1",
        (content,),
    ).fetchone()
    if row:
        return dict(row)
    if created_at:
        row = conn.execute(
            "SELECT valence, arousal, tone FROM dream_pool WHERE created_at=? ORDER BY id DESC LIMIT 1",
            (created_at,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def resolve_dream_title(row: dict) -> str:
    title = (row.get('summary_title') or '').strip()
    if title:
        return title
    content = (row.get('content') or '').strip()
    return summary_title.rule_summary_title(content) or '无题'


def resolve_dream_fields(row: dict, pool_row: dict | None = None) -> dict:
    pool_row = pool_row or {}
    tone = _parse_tone_from_tags(row.get('tags')) or (pool_row.get('tone') or '').strip() or None
    if tone and tone not in VALID_TONES:
        tone = None

    valence_u = _coerce_unipolar(row.get('valence'), default=0.0)
    arousal_u = _coerce_unipolar(row.get('arousal'), default=0.0)
    if valence_u == 0.0:
        valence_u = _coerce_unipolar(pool_row.get('valence'), default=0.5)
    if arousal_u == 0.0:
        arousal_u = _coerce_unipolar(pool_row.get('arousal'), default=0.5)

    if not tone:
        if valence_u >= 0.6 and arousal_u >= 0.6:
            tone = 'vivid'
        elif valence_u >= 0.6 and arousal_u < 0.6:
            tone = 'warm'
        elif valence_u < 0.4 and arousal_u >= 0.6:
            tone = 'anxious'
        elif valence_u < 0.4 and arousal_u < 0.6:
            tone = 'heavy'
        else:
            tone = 'drifting'

    return {
        'title': resolve_dream_title(row),
        'tone': tone,
        'emotion': emotion_label_for_tone(tone),
        'valence': round(normalize_valence(valence_u, scale='unipolar'), 3),
        'arousal': round(normalize_arousal(arousal_u), 3),
    }


def build_dream_api_item(row: dict, pool_row: dict | None = None, *, include_id: bool = True) -> dict:
    content = (row.get('content') or '').strip()
    created_at = row.get('created_at')
    meta = resolve_dream_fields(row, pool_row)
    item = {
        'author': (row.get('author') or 'fyodor').strip() or 'fyodor',
        'created_at': created_at,
        'date': created_at[:10] if created_at else '—',
        'title': meta['title'],
        'content': content,
        'emotion': meta['emotion'],
        'tone': meta['tone'],
        'valence': meta['valence'],
        'arousal': meta['arousal'],
    }
    if include_id and row.get('id') is not None:
        item['id'] = int(row['id'])
    return item


def fetch_dream_items(conn: sqlite3.Connection, limit: int = 10, *, include_id: bool = True) -> list[dict]:
    rows = conn.execute(
        "SELECT id, author, content, created_at, valence, arousal, tags, summary_title "
        "FROM posts WHERE type='DREAM' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        data = dict(row)
        content = (data.get('content') or '').strip()
        if not content or content in seen:
            continue
        seen.add(content)
        pool_row = _pool_row_for_post(conn, content, data.get('created_at'))
        items.append(build_dream_api_item(data, pool_row, include_id=include_id))
    return items
