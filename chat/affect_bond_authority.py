"""ISV3-1B Stage C — Affect + Bond production authority.

Sole production facts for Current Affect and Bond live in ``internal_state_v3``,
mutated only through Canonical Event apply paths:

  user_rule:{message_id}   → Bond (keyword observation)
  user_scored:{message_id} → Affect + Bond (async scorer observation)

``emotion_engine`` may evaluate (DeepSeek / rule / Ombre) and project
compatibility snapshots, but must not own a second mutable heart.
"""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

_LOG = logging.getLogger('affect_bond_authority')

_BOOTSTRAP_EVENT_KEY = 'bootstrap:initial'
_BOOTSTRAP_SOURCE_ID = 'stage_c_affect_bond_authority'
_DEFAULT_DB = '/opt/frontend/memories.db'


def memories_db_path(db_path: Optional[str] = None) -> str:
    return str(db_path or os.environ.get('MEMORIES_DB', _DEFAULT_DB))


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _now_str() -> str:
    return _now_beijing().strftime('%Y-%m-%d %H:%M:%S')


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    import internal_state_store as store
    return store.open_store(memories_db_path(db_path))


def _legacy_emotion_row(conn: sqlite3.Connection) -> Optional[dict]:
    try:
        row = conn.execute('SELECT * FROM emotion_state WHERE id=1').fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return dict(row) if hasattr(row, 'keys') else None


def _cutover_watermark(conn: sqlite3.Connection) -> int:
    """Positive watermark for Stage C bootstrap; never invents 0/None."""
    import internal_state_store as store

    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='internal_state_score_applied'"
        ).fetchone()
        if exists:
            row = conn.execute(
                'SELECT MAX(message_id) AS mid FROM internal_state_score_applied'
            ).fetchone()
            mid = row['mid'] if row is not None and hasattr(row, 'keys') else (
                row[0] if row else None
            )
            if mid is not None:
                return store.require_positive_message_id(
                    int(mid), field='last_scored_message_id',
                )
    except Exception:
        pass
    try:
        from chat.interaction_state import USER_AUTHOR_SQL
        row = conn.execute(
            f'SELECT MAX(id) AS mid FROM chat_messages WHERE {USER_AUTHOR_SQL}'
        ).fetchone()
        mid = row['mid'] if row is not None and hasattr(row, 'keys') else (
            row[0] if row else None
        )
        if mid is not None:
            return store.require_positive_message_id(
                int(mid), field='last_scored_message_id',
            )
    except Exception:
        pass
    # Greenfield: allow first real message/score after clearing watermark.
    return 1


def _snapshot_from_legacy(emotion: Mapping[str, Any], observed_at: str):
    return SimpleNamespace(
        observed_at=observed_at,
        affect=SimpleNamespace(
            pa=float(emotion.get('pa') if emotion.get('pa') is not None else 0.5),
            na=float(emotion.get('na') if emotion.get('na') is not None else 0.2),
            valence=float(
                emotion.get('valence') if emotion.get('valence') is not None else 0.6
            ),
            arousal=float(
                emotion.get('arousal') if emotion.get('arousal') is not None else 0.3
            ),
            mood_word=emotion.get('mood_word') or '平静',
        ),
        bond=SimpleNamespace(
            intimacy=float(
                emotion.get('sternberg_i')
                if emotion.get('sternberg_i') is not None else 0.3
            ),
            passion=float(
                emotion.get('sternberg_p')
                if emotion.get('sternberg_p') is not None else 0.0
            ),
            commitment=float(
                emotion.get('sternberg_c')
                if emotion.get('sternberg_c') is not None else 0.7
            ),
        ),
        candidate_unified_drives=SimpleNamespace(
            attachment=0.10, curiosity=0.20, reflection=0.10, social=0.10,
            duty=0.15, libido=0.0, stress=0.10, fatigue=0.20,
        ),
        diagnostics=SimpleNamespace(source_timestamps={}),
    )


def ensure_authority_ready(db_path: Optional[str] = None) -> bool:
    """Ensure ``internal_state_v3`` exists and is bootstrapped. Never raises."""
    import internal_state_store as store

    path = memories_db_path(db_path)
    conn = None
    try:
        conn = store.open_store(path)
        store.ensure_schema(conn)
        state = store.read_state(conn)
        if state is not None:
            return True
        emotion = _legacy_emotion_row(conn) or {}
        observed = _now_str()
        snap = _snapshot_from_legacy(emotion, observed)
        watermark = _cutover_watermark(conn)
        result = store.bootstrap_from_snapshot(
            conn,
            snap,
            last_scored_message_id=watermark,
            last_scored_message_id_source='stage_c_cutover_watermark',
            capture_mode='stage_c_affect_bond_v1',
            event_key=_BOOTSTRAP_EVENT_KEY,
            source_id=_BOOTSTRAP_SOURCE_ID,
        )
        if result.status not in ('applied', 'duplicate'):
            _LOG.warning(
                'affect_bond bootstrap failed: %s (%s)',
                result.status, result.error,
            )
            return False
        # Greenfield: no prior score proof → clear watermark so first score applies.
        try:
            proof = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='internal_state_score_applied'"
            ).fetchone()
            has_proof = False
            if proof:
                has_proof = conn.execute(
                    'SELECT 1 FROM internal_state_score_applied LIMIT 1'
                ).fetchone() is not None
            has_scored_event = conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_type='user_scored' LIMIT 1"
            ).fetchone() is not None
            if not has_proof and not has_scored_event:
                conn.execute(
                    'UPDATE internal_state_v3 '
                    'SET last_scored_message_id=NULL WHERE id=1'
                )
                conn.commit()
        except Exception as exc:
            _LOG.warning('affect_bond watermark clear skipped: %s', exc)
        return store.read_state(conn) is not None
    except Exception as exc:
        _LOG.warning('ensure_authority_ready failed: %s', exc)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def read_v3_state(db_path: Optional[str] = None) -> Optional[dict]:
    """Authoritative Affect+Bond row, or None if unavailable."""
    import internal_state_store as store

    if not ensure_authority_ready(db_path):
        return None
    conn = None
    try:
        conn = store.open_store(memories_db_path(db_path))
        return store.read_state(conn)
    except Exception as exc:
        _LOG.warning('read_v3_state failed: %s', exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def v3_to_legacy_emotion_shape(
    v3: Mapping[str, Any],
    *,
    last_interaction: Optional[str] = None,
    longing: float = 0.0,
) -> dict:
    """Map V3 Affect/Bond into the legacy emotion_state dict shape."""
    return {
        'pa': float(v3.get('pa') if v3.get('pa') is not None else 0.5),
        'na': float(v3.get('na') if v3.get('na') is not None else 0.2),
        'valence': float(v3.get('valence') if v3.get('valence') is not None else 0.6),
        'arousal': float(v3.get('arousal') if v3.get('arousal') is not None else 0.3),
        'mood_word': v3.get('mood_word') or '平静',
        'longing': float(longing or 0.0),
        'sternberg_i': float(
            v3.get('intimacy') if v3.get('intimacy') is not None else 0.3
        ),
        'sternberg_p': float(
            v3.get('passion') if v3.get('passion') is not None else 0.0
        ),
        'sternberg_c': float(
            v3.get('commitment') if v3.get('commitment') is not None else 0.7
        ),
        'p_updated_at': v3.get('p_updated_at'),
        'i_updated_at': v3.get('i_updated_at'),
        'last_interaction': last_interaction,
        'updated_at': v3.get('updated_at'),
    }


def project_v3_to_emotion_state(db_path: Optional[str] = None) -> None:
    """Best-effort compatibility snapshot into emotion_state (not authority)."""
    v3 = read_v3_state(db_path)
    if not v3:
        return
    path = memories_db_path(db_path)
    conn = None
    try:
        conn = sqlite3.connect(path)
        shaped = v3_to_legacy_emotion_shape(v3)
        conn.execute(
            """
            UPDATE emotion_state SET
                pa=?, na=?, valence=?, arousal=?, mood_word=?,
                sternberg_p=?, sternberg_i=?, sternberg_c=?,
                p_updated_at=?, i_updated_at=?, updated_at=?
            WHERE id=1
            """,
            (
                shaped['pa'], shaped['na'], shaped['valence'], shaped['arousal'],
                shaped['mood_word'],
                shaped['sternberg_p'], shaped['sternberg_i'], shaped['sternberg_c'],
                shaped['p_updated_at'], shaped['i_updated_at'], shaped['updated_at'],
            ),
        )
        conn.commit()
    except Exception as exc:
        _LOG.warning('project_v3_to_emotion_state failed: %s', exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def apply_user_rule_observation(
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
    db_path: Optional[str] = None,
) -> Any:
    """Canonical Bond mutation for a user message (idempotent)."""
    import internal_state_events as events
    import internal_state_store as store

    if not ensure_authority_ready(db_path):
        raise store.StoreError('affect_bond authority not ready for user_rule')
    conn = None
    try:
        conn = store.open_store(memories_db_path(db_path))
        result = events.observe_user_message(
            conn,
            message_id=message_id,
            text=text,
            created_at=created_at,
            previous_user_at=previous_user_at,
        )
        if result.status in ('applied', 'duplicate'):
            try:
                project_v3_to_emotion_state(db_path)
            except Exception:
                pass
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def apply_scored_observation(
    *,
    message_id: int,
    scores: Mapping[str, Any],
    scored_at: str,
    db_path: Optional[str] = None,
    max_version_retries: int = 3,
) -> Any:
    """Canonical Affect+Bond mutation from async scorer (idempotent / stale-safe)."""
    import internal_state_events as events
    import internal_state_store as store

    if not ensure_authority_ready(db_path):
        raise store.StoreError('affect_bond authority not ready for user_scored')

    path = memories_db_path(db_path)
    last = None
    for _ in range(max(1, int(max_version_retries))):
        conn = None
        try:
            conn = store.open_store(path)
            state = store.read_state(conn)
            expected = int(state['state_version']) if state else None
            last = events.observe_scored(
                conn,
                message_id=message_id,
                scores=scores,
                scored_at=scored_at,
                expected_state_version=expected,
            )
            if last.status != 'version_conflict':
                if last.status in ('applied', 'duplicate', 'stale_skipped'):
                    try:
                        project_v3_to_emotion_state(db_path)
                    except Exception:
                        pass
                return last
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return last


def apply_user_rule_best_effort(
    *,
    message_id: int,
    text: str,
    created_at: str,
    previous_user_at: Optional[str],
    db_path: Optional[str] = None,
) -> bool:
    try:
        result = apply_user_rule_observation(
            message_id=message_id,
            text=text,
            created_at=created_at,
            previous_user_at=previous_user_at,
            db_path=db_path,
        )
        return getattr(result, 'status', None) in (
            'applied', 'duplicate', 'stale_skipped',
        )
    except Exception as exc:
        _LOG.warning('apply_user_rule_best_effort failed: %s', exc)
        return False


def apply_scored_best_effort(
    *,
    message_id: int,
    scores: Mapping[str, Any],
    scored_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    try:
        result = apply_scored_observation(
            message_id=message_id,
            scores=scores,
            scored_at=scored_at or _now_str(),
            db_path=db_path,
        )
        return getattr(result, 'status', None) in (
            'applied', 'duplicate', 'stale_skipped',
        )
    except Exception as exc:
        _LOG.warning('apply_scored_best_effort failed: %s', exc)
        return False
