"""ISV3-1B Stage C — Affect + Bond production authority.

Sole production facts for Current Affect and Bond live in ``internal_state_v3``,
mutated only through Canonical Event apply paths:

  user_rule:{message_id}   → Bond (keyword observation)
  user_scored:{message_id} → Affect + Bond (async scorer observation)

Cutover readiness reuses Shadow bootstrap provenance / proof-gap /
watermark health. Getters are pure reads and never bootstrap.
"""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
from types import SimpleNamespace
from typing import Any, Mapping, Optional

_LOG = logging.getLogger('affect_bond_authority')

_DEFAULT_DB = '/opt/frontend/memories.db'


def memories_db_path(db_path: Optional[str] = None) -> str:
    return str(db_path or os.environ.get('MEMORIES_DB', _DEFAULT_DB))


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _now_str() -> str:
    return _now_beijing().strftime('%Y-%m-%d %H:%M:%S')


def read_v3_state(db_path: Optional[str] = None) -> Optional[dict]:
    """Pure read of ``internal_state_v3``. Never bootstraps / ensures schema / mutates."""
    import internal_state_store as store

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


def check_cutover_ready(db_path: Optional[str] = None) -> SimpleNamespace:
    """Fail-closed Stage C gate. Read-only — never bootstraps or mutates.

    Requires production bootstrap provenance, no unresolved proof gap, and
    no watermark lag (``last_scored_message_id >= proof max`` when proof exists).
    """
    import internal_state_shadow as shadow
    import internal_state_store as store

    path = memories_db_path(db_path)
    conn = None
    try:
        conn = store.open_store(path)
        state, _event, gate = shadow._read_bootstrap_gate(conn)
        if gate == 'missing':
            return SimpleNamespace(
                ok=False, status='bootstrap_missing',
                error='internal_state_v3 bootstrap missing',
            )
        if gate == 'invalid':
            return SimpleNamespace(
                ok=False, status='bootstrap_provenance_invalid',
                error='bootstrap present but provenance invalid; refusing authority',
            )
        if shadow.has_unresolved_proof_gap(conn, db_path=path):
            return SimpleNamespace(
                ok=False, status='proof_gap',
                error='unresolved score proof gap; refusing authority',
            )
        # Watermark health: refuse lag when proof table can speak.
        if shadow.score_proof_schema_ready(conn):
            try:
                proof_max = shadow.resolve_scored_watermark(conn)
            except store.StoreError:
                proof_max = None
            last = None if state is None else state.get('last_scored_message_id')
            if proof_max is not None and last is None:
                return SimpleNamespace(
                    ok=False, status='watermark_lag',
                    error='proof watermark exists but state last_scored is NULL',
                )
            if proof_max is not None and last is not None:
                if int(proof_max) > int(last):
                    return SimpleNamespace(
                        ok=False, status='watermark_lag',
                        error=(
                            f'watermark lag: proof_max={proof_max} > '
                            f'last_scored={last}'
                        ),
                    )
        return SimpleNamespace(ok=True, status='ready', error=None)
    except Exception as exc:
        _LOG.warning('check_cutover_ready failed: %s', exc)
        return SimpleNamespace(
            ok=False, status='failed', error=str(exc),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def ensure_authority_ready(db_path: Optional[str] = None) -> bool:
    """Stage C cutover gate for mutation paths.

    Reuses ``internal_state_shadow.ensure_bootstrapped`` (production provenance,
    no default-personality wash). Never clears watermarks. Never invents
    ``{}`` → default Affect/Bond. Getters must not call this.
    """
    ready = check_cutover_ready(db_path)
    if ready.ok:
        return True

    # Untrusted / damaged structural bootstrap must not be overwritten.
    if ready.status == 'bootstrap_provenance_invalid':
        _LOG.warning('ensure_authority_ready refuse: %s', ready.error)
        return False
    if ready.status in ('proof_gap', 'watermark_lag'):
        _LOG.warning('ensure_authority_ready refuse: %s', ready.error)
        return False

    path = memories_db_path(db_path)
    try:
        import internal_state_shadow as shadow
        result = shadow.ensure_bootstrapped(db_path=path)
        if not result.ok and result.status != 'disabled':
            _LOG.warning(
                'ensure_authority_ready bootstrap: %s (%s)',
                result.status, result.error,
            )
    except Exception as exc:
        _LOG.warning('ensure_authority_ready bootstrap failed: %s', exc)

    ready2 = check_cutover_ready(db_path)
    if not ready2.ok:
        _LOG.warning(
            'ensure_authority_ready still not ready: %s (%s)',
            ready2.status, ready2.error,
        )
    return bool(ready2.ok)


def read_current_bond(
    db_path: Optional[str] = None,
    *,
    observed_at: Optional[str] = None,
) -> Optional[dict]:
    """Canonical current Bond (intimacy/passion/commitment) from V3 state.

    Materialization is owned by ``internal_state_events.materialize_bond``
    (τ6 / τ96). Callers must not re-decay.
    """
    from internal_state_events import materialize_bond

    if not check_cutover_ready(db_path).ok:
        return None
    v3 = read_v3_state(db_path)
    if v3 is None:
        return None
    return materialize_bond(v3, observed_at or _now_str())


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
    max_version_retries: int = 3,
) -> Any:
    """Canonical Bond mutation for a user message (idempotent + version retry)."""
    import internal_state_events as events
    import internal_state_store as store

    if not ensure_authority_ready(db_path):
        raise store.StoreError('affect_bond authority not ready for user_rule')

    path = memories_db_path(db_path)
    last = None
    for _ in range(max(1, int(max_version_retries))):
        conn = None
        try:
            conn = store.open_store(path)
            state = store.read_state(conn)
            expected = int(state['state_version']) if state else None
            last = events.observe_user_message(
                conn,
                message_id=message_id,
                text=text,
                created_at=created_at,
                previous_user_at=previous_user_at,
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
