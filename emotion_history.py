"""Persist and query emotion snapshots for the Moments timeline chart."""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3

from valence_scale import normalize_arousal, normalize_valence

SHANGHAI_OFFSET = datetime.timedelta(hours=8)

# Rows projected from a V3 user_scored event's settled Affect. They carry the
# event's key, so the projection is idempotent and rebuildable from
# internal_state_events.result_json. Older rows carry source='score' (the
# scorer blend, written unconditionally) and no event key.
SOURCE_V3_AFFECT = 'v3_affect'

_LOG = logging.getLogger('emotion_history')


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: str) -> None:
    # Check-then-ALTER runs under one write lock: two first-use callers after a
    # deploy must not both see the old table and both ADD COLUMN.
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS emotion_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                valence REAL NOT NULL,
                arousal REAL NOT NULL,
                mood_word TEXT,
                source TEXT DEFAULT 'score',
                source_event_key TEXT
            )
            """
        )
        cols = {row['name'] for row in conn.execute('PRAGMA table_info(emotion_history)')}
        if 'source_event_key' not in cols:
            conn.execute('ALTER TABLE emotion_history ADD COLUMN source_event_key TEXT')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_emotion_history_recorded '
            'ON emotion_history(recorded_at)'
        )
        conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_emotion_history_event_key '
            'ON emotion_history(source_event_key) WHERE source_event_key IS NOT NULL'
        )
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def _now_str() -> str:
    return (datetime.datetime.utcnow() + SHANGHAI_OFFSET).strftime('%Y-%m-%d %H:%M:%S')


def _insert(conn, valence_unipolar, arousal, mood_word, *, source, recorded_at,
            source_event_key) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO emotion_history
            (recorded_at, valence, arousal, mood_word, source, source_event_key)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            recorded_at or _now_str(),
            round(float(valence_unipolar), 4),
            round(float(arousal), 4),
            (mood_word or '')[:30],
            source,
            source_event_key,
        ),
    )
    return cur.rowcount == 1


def append_snapshot(
    valence_unipolar: float,
    arousal: float,
    mood_word: str,
    *,
    db_path: str,
    source: str = 'score',
    recorded_at: str | None = None,
    source_event_key: str | None = None,
) -> bool:
    """Append one point; with ``source_event_key`` a repeat is ignored. True if inserted."""
    ensure_schema(db_path)
    conn = _conn(db_path)
    try:
        inserted = _insert(
            conn, valence_unipolar, arousal, mood_word, source=source,
            recorded_at=recorded_at, source_event_key=source_event_key,
        )
        conn.commit()
        return inserted
    finally:
        conn.close()


def settled_affect_from_result(result) -> dict | None:
    """The ``settled_affect`` a V3 user_scored event froze, or None."""
    if not isinstance(result, dict):
        return None
    affect = result.get('settled_affect')
    if not isinstance(affect, dict):
        return None
    if affect.get('valence') is None or affect.get('arousal') is None:
        return None
    return affect


def reconcile_from_events(db_path: str, *, since: str | None = None) -> int:
    """Project applied user_scored events' settled Affect into the timeline.

    Idempotent (keyed by event_key). Fills points whose write was lost after
    V3 committed. Returns the number of points inserted.
    """
    ensure_schema(db_path)
    conn = _conn(db_path)
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='internal_state_events'"
        ).fetchone():
            return 0
        sql = (
            "SELECT event_key, result_json FROM internal_state_events "
            "WHERE event_type='user_scored' AND status='applied' "
            "AND result_json IS NOT NULL"
        )
        params: tuple = ()
        if since:
            sql += ' AND applied_at >= ?'
            params = (since,)
        inserted = 0
        for row in conn.execute(sql, params).fetchall():
            try:
                affect = settled_affect_from_result(json.loads(row['result_json']))
            except (TypeError, ValueError):
                affect = None
            if affect is None:
                continue
            if _insert(
                conn, affect['valence'], affect['arousal'], affect.get('mood_word') or '',
                source=SOURCE_V3_AFFECT, recorded_at=affect.get('scored_at'),
                source_event_key=row['event_key'],
            ):
                inserted += 1
        conn.commit()
        return inserted
    finally:
        conn.close()


def _cutoff(days: int) -> str:
    now = datetime.datetime.utcnow() + SHANGHAI_OFFSET
    start = now - datetime.timedelta(days=max(1, int(days)) - 1)
    return start.strftime('%Y-%m-%d 00:00:00')


def fetch_series(days: int, *, db_path: str) -> list[dict]:
    """Return one averaged bipolar point per calendar day (Shanghai), oldest first."""
    try:
        reconcile_from_events(db_path, since=_cutoff(days))
    except Exception as exc:
        _LOG.warning('emotion_history reconcile skipped: %s', exc)
    ensure_schema(db_path)
    conn = _conn(db_path)
    rows = conn.execute(
        """
        SELECT substr(recorded_at, 1, 10) AS day,
               AVG(valence) AS valence,
               AVG(arousal) AS arousal,
               MAX(mood_word) AS mood_word,
               COUNT(*) AS samples
        FROM emotion_history
        WHERE recorded_at >= ?
        GROUP BY day
        ORDER BY day ASC
        """,
        (_cutoff(days),),
    ).fetchall()
    conn.close()

    series: list[dict] = []
    for row in rows:
        bipolar_v = normalize_valence(float(row['valence']), scale='unipolar')
        arousal = normalize_arousal(float(row['arousal']))
        series.append({
            'day': row['day'],
            'valence': round(bipolar_v, 3),
            'arousal': round(arousal, 3),
            'mood_word': row['mood_word'] or '',
            'samples': int(row['samples'] or 0),
        })
    return series
