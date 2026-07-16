"""Persist and query emotion snapshots for the Moments timeline chart."""

from __future__ import annotations

import datetime
import sqlite3

from valence_scale import normalize_arousal, normalize_valence

SHANGHAI_OFFSET = datetime.timedelta(hours=8)


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: str) -> None:
    conn = _conn(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emotion_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            valence REAL NOT NULL,
            arousal REAL NOT NULL,
            mood_word TEXT,
            source TEXT DEFAULT 'score'
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_emotion_history_recorded '
        'ON emotion_history(recorded_at)'
    )
    conn.commit()
    conn.close()


def _now_str() -> str:
    return (datetime.datetime.utcnow() + SHANGHAI_OFFSET).strftime('%Y-%m-%d %H:%M:%S')


def append_snapshot(
    valence_unipolar: float,
    arousal: float,
    mood_word: str,
    *,
    db_path: str,
    source: str = 'score',
) -> None:
    ensure_schema(db_path)
    conn = _conn(db_path)
    conn.execute(
        """
        INSERT INTO emotion_history (recorded_at, valence, arousal, mood_word, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            _now_str(),
            round(float(valence_unipolar), 4),
            round(float(arousal), 4),
            (mood_word or '')[:30],
            source,
        ),
    )
    conn.commit()
    conn.close()


def _cutoff(days: int) -> str:
    now = datetime.datetime.utcnow() + SHANGHAI_OFFSET
    start = now - datetime.timedelta(days=max(1, int(days)) - 1)
    return start.strftime('%Y-%m-%d 00:00:00')


def fetch_series(days: int, *, db_path: str) -> list[dict]:
    """Return one averaged bipolar point per calendar day (Shanghai), oldest first."""
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
