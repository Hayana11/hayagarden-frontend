"""ISV3-1B Stage D — Eight Drives production authority.

Sole production facts for the eight drives live in ``internal_state_v3``,
mutated only through Canonical Event apply paths:

  user_rule:{message_id}     → attachment / fatigue (+ materialize all)
  wake_outcome:{wake_run_id} → drive settlement (fixed discharge)

Cutover readiness reuses Stage C Affect+Bond gate (bootstrap provenance /
proof-gap / watermark health). Getters are pure reads and never bootstrap.

Bond intimacy/passion are NOT aliases of Drive attachment/libido.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import Any, Mapping, Optional

_LOG = logging.getLogger('drive_authority')

_DRIVE_KEYS = (
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress', 'fatigue',
)
_DESIRE_KEYS = (
    'curiosity', 'reflection', 'duty', 'social',
    'libido', 'stress', 'fatigue',
)


def memories_db_path(db_path: Optional[str] = None) -> str:
    from chat.affect_bond_authority import memories_db_path as _path
    return _path(db_path)


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _now_str() -> str:
    return _now_beijing().strftime('%Y-%m-%d %H:%M:%S')


def check_cutover_ready(db_path: Optional[str] = None):
    """Same fail-closed gate as Stage C Affect+Bond authority."""
    from chat.affect_bond_authority import check_cutover_ready as _check
    return _check(db_path)


def check_cutover_ready_on_conn(conn, db_path: Optional[str] = None):
    """Same gate on an open Action connection (no COMMIT / bootstrap)."""
    from chat.affect_bond_authority import check_cutover_ready_on_conn as _check
    return _check(conn, db_path=db_path)


def ensure_authority_ready(db_path: Optional[str] = None) -> bool:
    from chat.affect_bond_authority import ensure_authority_ready as _ensure
    return _ensure(db_path)


def read_v3_state(db_path: Optional[str] = None) -> Optional[dict]:
    from chat.affect_bond_authority import read_v3_state as _read
    return _read(db_path)


def _longing_for_boost() -> float:
    try:
        from internal_state import read_derived_longing
        longing = read_derived_longing()
        if longing is None:
            return 0.0
        return float(longing)
    except Exception:
        return 0.0


def read_current_drives(
    db_path: Optional[str] = None,
    *,
    observed_at: Optional[str] = None,
) -> Optional[dict]:
    """Canonical current eight drives from V3 (materialized, read-only)."""
    from internal_state_events import materialize_bond, materialize_drives

    if not check_cutover_ready(db_path).ok:
        return None
    v3 = read_v3_state(db_path)
    if v3 is None:
        return None
    at = observed_at or _now_str()
    bond = materialize_bond(v3, at)
    return materialize_drives(
        v3,
        observed_at=at,
        longing_for_boost=_longing_for_boost(),
        passion_for_boost=float(bond['passion']),
    )


def project_v3_to_drive_state(db_path: Optional[str] = None) -> None:
    """Best-effort compatibility snapshot into drive_state (not authority)."""
    v3 = read_v3_state(db_path)
    if not v3:
        return
    path = memories_db_path(db_path)
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            """
            UPDATE drive_state SET
                attachment=?, curiosity=?, reflection=?, social=?,
                duty=?, libido=?, stress=?, fatigue=?,
                last_updated=?
            WHERE id=1
            """,
            (
                float(v3.get('attachment') if v3.get('attachment') is not None else 0.1),
                float(v3.get('curiosity') if v3.get('curiosity') is not None else 0.2),
                float(v3.get('reflection') if v3.get('reflection') is not None else 0.1),
                float(v3.get('social') if v3.get('social') is not None else 0.1),
                float(v3.get('duty') if v3.get('duty') is not None else 0.15),
                float(v3.get('libido') if v3.get('libido') is not None else 0.0),
                float(v3.get('stress') if v3.get('stress') is not None else 0.1),
                float(v3.get('fatigue') if v3.get('fatigue') is not None else 0.2),
                v3.get('drives_updated_at') or _now_str(),
            ),
        )
        conn.commit()
    except Exception as exc:
        _LOG.warning('project_v3_to_drive_state failed: %s', exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def project_v3_to_desire_state(db_path: Optional[str] = None) -> None:
    """Best-effort compatibility snapshot into desire_state (not authority)."""
    v3 = read_v3_state(db_path)
    if not v3:
        return
    path = memories_db_path(db_path)
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            """
            UPDATE desire_state SET
                curiosity=?, reflection=?, duty=?, social=?,
                libido=?, stress=?, fatigue=?,
                last_updated=?
            WHERE id=1
            """,
            (
                float(v3.get('curiosity') if v3.get('curiosity') is not None else 0.2),
                float(v3.get('reflection') if v3.get('reflection') is not None else 0.1),
                float(v3.get('duty') if v3.get('duty') is not None else 0.15),
                float(v3.get('social') if v3.get('social') is not None else 0.1),
                float(v3.get('libido') if v3.get('libido') is not None else 0.0),
                float(v3.get('stress') if v3.get('stress') is not None else 0.1),
                float(v3.get('fatigue') if v3.get('fatigue') is not None else 0.2),
                v3.get('drives_updated_at') or _now_str(),
            ),
        )
        conn.commit()
    except Exception as exc:
        _LOG.warning('project_v3_to_desire_state failed: %s', exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def project_v3_drive_compatibility(db_path: Optional[str] = None) -> None:
    """Project V3 drive bases into both legacy snapshot tables."""
    project_v3_to_drive_state(db_path)
    project_v3_to_desire_state(db_path)


def apply_wake_outcome_observation(
    *,
    wake_run_id: str,
    executor_action: str,
    desire_action: Optional[str],
    fired_drive: Optional[str],
    desire_driven: bool,
    user_idle_hours: float,
    outcome_at: Optional[str] = None,
    db_path: Optional[str] = None,
    max_version_retries: int = 3,
) -> Any:
    """Canonical Wake drive settlement (idempotent + version retry).

    Requires decision-time ``fired_drive`` for non-``none`` actions (fail closed
    inside ``apply_outcome``). Does not infer Intent/Drive from assistant text.
    """
    import internal_state_events as events
    import internal_state_store as store

    if not ensure_authority_ready(db_path):
        raise store.StoreError('drive authority not ready for wake_outcome')

    path = memories_db_path(db_path)
    at = outcome_at or _now_str()
    last = None
    for _ in range(max(1, int(max_version_retries))):
        conn = None
        try:
            conn = store.open_store(path)
            state = store.read_state(conn)
            expected = int(state['state_version']) if state else None
            last = events.apply_outcome(
                conn,
                wake_run_id=wake_run_id,
                executor_action=executor_action,
                desire_action=desire_action,
                fired_drive=fired_drive,
                desire_driven=desire_driven,
                user_idle_hours=float(user_idle_hours),
                outcome_at=at,
                expected_state_version=expected,
            )
            if last.status != 'version_conflict':
                if last.status in ('applied', 'duplicate', 'stale_skipped'):
                    try:
                        project_v3_drive_compatibility(db_path)
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


def apply_wake_outcome_best_effort(
    *,
    wake_run_id: str,
    executor_action: str,
    desire_action: Optional[str],
    fired_drive: Optional[str],
    desire_driven: bool,
    user_idle_hours: float,
    outcome_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Standalone best-effort Wake drive settlement (tests / recovery).

    Production Wake path must settle inside ``wake.executor``'s Action
    transaction via ``apply_wake_outcome_on_conn`` — not this helper.
    Fail closed when non-``none`` action lacks decision-time ``fired_drive``.
    """
    if str(executor_action or '').strip() != 'none' and not fired_drive:
        _LOG.warning(
            'apply_wake_outcome_best_effort skip: missing decision-time '
            'provenance (wake_run_id=%s action=%s)',
            wake_run_id, executor_action,
        )
        return False
    try:
        result = apply_wake_outcome_observation(
            wake_run_id=wake_run_id,
            executor_action=executor_action,
            desire_action=desire_action,
            fired_drive=fired_drive,
            desire_driven=desire_driven,
            user_idle_hours=user_idle_hours,
            outcome_at=outcome_at,
            db_path=db_path,
        )
        return getattr(result, 'status', None) in (
            'applied', 'duplicate', 'stale_skipped',
        )
    except Exception as exc:
        _LOG.warning('apply_wake_outcome_best_effort failed: %s', exc)
        return False


def apply_wake_outcome_on_conn(
    conn,
    *,
    wake_run_id: str,
    executor_action: str,
    desire_action: Optional[str],
    fired_drive: Optional[str],
    desire_driven: bool,
    user_idle_hours: float,
    outcome_at: Optional[str] = None,
    provenance_present: bool = False,
    db_path: Optional[str] = None,
    max_version_retries: int = 3,
) -> Any:
    """Apply ``wake_outcome`` on an open Action transaction (SAVEPOINT join).

    Caller owns BEGIN/COMMIT. Does not invent Action→Drive provenance.
    Enforces cutover readiness on this connection before mutation.
    Raises ``StoreError`` when Decision provenance object is missing, or when
    non-``none`` lacks ``fired_drive``.
    """
    import internal_state_events as events
    import internal_state_store as store

    if not provenance_present:
        raise store.StoreError(
            'missing decision-time provenance object; refusing wake_outcome'
        )
    if str(executor_action or '').strip() != 'none' and not fired_drive:
        raise store.StoreError(
            'missing decision-time primary_drive; refusing wake_outcome'
        )

    ready = check_cutover_ready_on_conn(conn, db_path=db_path)
    if not ready.ok:
        raise store.StoreError(
            f'drive authority not ready for wake_outcome: {ready.status}'
            + (f' ({ready.error})' if ready.error else '')
        )

    at = outcome_at or _now_str()
    last = None
    for _ in range(max(1, int(max_version_retries))):
        state = store.read_state(conn)
        if state is None:
            raise store.StoreError(
                'internal_state_v3 missing; refusing wake_outcome on conn'
            )
        expected = int(state['state_version'])
        last = events.apply_outcome(
            conn,
            wake_run_id=wake_run_id,
            executor_action=executor_action,
            desire_action=desire_action,
            fired_drive=fired_drive,
            desire_driven=desire_driven,
            user_idle_hours=float(user_idle_hours),
            outcome_at=at,
            expected_state_version=expected,
            join_transaction=True,
        )
        if last.status != 'version_conflict':
            return last
    return last


def v3_to_drive_engine_shape(drives: Mapping[str, Any]) -> dict:
    """Map materialized V3 drives into drive_engine.get_drive() shape."""
    out = {}
    for key in _DRIVE_KEYS:
        raw = drives.get(key)
        out[key] = round(float(raw if raw is not None else 0.1), 4)
    return out


def v3_to_desire_shape(drives: Mapping[str, Any]) -> dict:
    """Map materialized V3 drives into desire.get_drive() shape (no attachment)."""
    out = {}
    for key in _DESIRE_KEYS:
        raw = drives.get(key)
        out[key] = round(float(raw if raw is not None else 0.1), 4)
    return out
