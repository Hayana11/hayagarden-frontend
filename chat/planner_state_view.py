"""B1-1A — immutable Decision-time PlannerStateView.

Forms one SQLite read-transaction snapshot of V3 row S + DecisionClock C,
derives Longing from C, materializes Bond/Drives at the same observed_at,
then releases the DB transaction immediately. Callers carry only the
frozen value object — never a live connection or open transaction.

Authoritative snapshot unavailable ⇒ raise ``PlannerStateViewUnavailable``.
Never invent default Affect/Bond/Drives as a fake valid V.

See ``docs/behavior_authority_b1_shadow_contract.md`` §3.1 / §1.1.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

_LOG = logging.getLogger('planner_state_view')

_DRIVE_KEYS = (
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress', 'fatigue',
)

# Modes that run legacy decide/freeze in inject_snippets.
BEHAVIOR_DECISION_MODES = frozenset({
    'normal', 'morning', 'nightwatch', 'ritual', 'self_trigger',
})


class PlannerStateViewUnavailable(Exception):
    """No authoritative S+C snapshot ⇒ no valid PlannerStateView."""

    def __init__(self, reason: str):
        self.reason = str(reason or 'unavailable')
        super().__init__(self.reason)


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _now_str(dt: Optional[datetime.datetime] = None) -> str:
    return (dt or _now_beijing()).strftime('%Y-%m-%d %H:%M:%S')


def _mapping_proxy(data: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(data))


def b1_1a_v_plumbing_eligible(*, mode: str, live: bool) -> bool:
    """B8: V freeze only for live Behavior-Decision comparison-eligible attempts."""
    return bool(live) and str(mode or '') in BEHAVIOR_DECISION_MODES


@dataclass(frozen=True)
class PlannerStateView:
    """Immutable Decision-time state bundle (contract §3.1)."""

    state_version: int
    observed_at: str
    wake_run_id: Optional[str]
    affect: Mapping[str, Any]
    bond: Mapping[str, Any]
    drives: Mapping[str, float]
    longing_derived: float
    interaction_clock: Mapping[str, Any]
    immutable: bool = True

    @property
    def user_idle_hours(self) -> float:
        raw = self.interaction_clock.get('user_idle_hours')
        if raw is None:
            return 0.0
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    @property
    def effective_idle_hours(self) -> float:
        raw = self.interaction_clock.get('effective_idle_hours')
        if raw is None:
            return self.user_idle_hours
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return self.user_idle_hours

    @property
    def clock_reliable(self) -> bool:
        return bool(self.interaction_clock.get('reliable'))

    def drives_for_engine(self) -> dict:
        """Mutable copy shaped for ``drive_engine.decide(drive=...)``.

        Preserves legitimate ``0.0`` values — must not use ``x or 0.1``.
        """
        out = {}
        for k in _DRIVE_KEYS:
            raw = self.drives.get(k)
            out[k] = float(raw if raw is not None else 0.1)
        return out

    def attachment(self) -> float:
        raw = self.drives.get('attachment')
        return float(raw if raw is not None else 0.0)


def freeze_planner_state_view(
    *,
    db_path: Optional[str] = None,
    observed_at: Optional[datetime.datetime] = None,
    wake_run_id: Optional[str] = None,
) -> PlannerStateView:
    """Open one SQLite snapshot, freeze PlannerStateView, release txn/conn.

    Causal order (hard):
      BEGIN → read S + C at T → derive L → materialize bond/drives
      → freeze V → END txn → close conn → return immutable V only.

    Raises ``PlannerStateViewUnavailable`` when cutover is not ready, V3 row
    is missing, or freeze fails. Never returns a synthetic default persona.
    """
    from chat.affect_bond_authority import (
        check_cutover_ready_on_conn,
        memories_db_path,
    )
    from chat.drive_authority import v3_to_drive_engine_shape
    from chat.interaction_state import read_interaction_clock_from_conn
    from internal_state import derived_longing_curve
    from internal_state_events import materialize_bond, materialize_drives
    import internal_state_store as store

    observed_dt = observed_at or _now_beijing()
    observed_str = _now_str(observed_dt)
    path = memories_db_path(db_path)
    run_id = str(wake_run_id).strip() if wake_run_id else None
    if run_id == '':
        run_id = None

    conn = None
    began = False
    try:
        conn = store.open_store(path)
        conn.execute('BEGIN')
        began = True

        ready = check_cutover_ready_on_conn(conn, db_path=path)
        if not ready.ok:
            conn.execute('ROLLBACK')
            began = False
            raise PlannerStateViewUnavailable(
                f'cutover_not_ready:{ready.status}'
            )

        state = store.read_state(conn)
        clock = read_interaction_clock_from_conn(conn, now=observed_dt)

        if state is None:
            conn.execute('ROLLBACK')
            began = False
            raise PlannerStateViewUnavailable('v3_state_missing')

        # Materialize from frozen Python copies of S+C while txn still open,
        # then release immediately — no live conn carried after return.
        if clock.reliable and clock.user_idle_hours is not None:
            longing_raw = derived_longing_curve(float(clock.user_idle_hours))
            longing = float(longing_raw) if longing_raw is not None else 0.0
            user_idle = float(clock.user_idle_hours)
            effective = float(
                clock.effective_idle_hours
                if clock.effective_idle_hours is not None
                else user_idle
            )
            clock_reliable = True
            clock_reason = clock.reason or 'ok'
        else:
            longing = 0.0
            user_idle = 0.0
            effective = 0.0
            clock_reliable = False
            clock_reason = clock.reason or 'clock_unreliable'

        bond = materialize_bond(state, observed_str)
        drives_v3 = materialize_drives(
            state,
            observed_at=observed_str,
            longing_for_boost=longing,
            passion_for_boost=float(bond['passion']),
        )
        drives = v3_to_drive_engine_shape(drives_v3)
        affect = {
            'pa': float(state.get('pa') if state.get('pa') is not None else 0.5),
            'na': float(state.get('na') if state.get('na') is not None else 0.2),
            'valence': float(
                state.get('valence') if state.get('valence') is not None else 0.6
            ),
            'arousal': float(
                state.get('arousal') if state.get('arousal') is not None else 0.3
            ),
            'mood_word': state.get('mood_word') or '平静',
        }
        view = PlannerStateView(
            state_version=int(state.get('state_version') or 0),
            observed_at=observed_str,
            wake_run_id=run_id,
            affect=_mapping_proxy(affect),
            bond=_mapping_proxy({
                'intimacy': float(bond['intimacy']),
                'passion': float(bond['passion']),
                'commitment': float(bond['commitment']),
            }),
            drives=_mapping_proxy(drives),
            longing_derived=float(longing),
            interaction_clock=_mapping_proxy({
                'user_idle_hours': user_idle,
                'effective_idle_hours': effective,
                'reliable': clock_reliable,
                'reason': clock_reason,
                'last_user_at': (
                    clock.last_user_at.strftime('%Y-%m-%d %H:%M:%S')
                    if clock.last_user_at is not None else None
                ),
                'last_wake_message_at': (
                    clock.last_wake_message_at.strftime('%Y-%m-%d %H:%M:%S')
                    if clock.last_wake_message_at is not None else None
                ),
            }),
            immutable=True,
        )

        conn.execute('COMMIT')
        began = False
        return view
    except PlannerStateViewUnavailable:
        raise
    except Exception as exc:
        _LOG.warning('freeze_planner_state_view failed: %s', exc)
        if conn is not None and began:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
            began = False
        raise PlannerStateViewUnavailable(
            f'freeze_failed:{type(exc).__name__}'
        ) from exc
    finally:
        if conn is not None:
            try:
                if began:
                    conn.execute('ROLLBACK')
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


def decision_hours_from_view(view: PlannerStateView) -> tuple:
    """Return (t2_hours, t_hours) from DecisionClock — never GuardClock."""
    return float(view.user_idle_hours), float(view.effective_idle_hours)
