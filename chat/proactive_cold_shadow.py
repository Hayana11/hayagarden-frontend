"""S1 shadow for proactive cold start (docs/proactive-cold-start-design.md).

When a morning / self_trigger round is skipped because the shared Chat
resident is cold, record whether the S2/S3 rules would have cold-started it
and what would have blocked it. Observation only: a read-only DB connection,
one JSONL append, never a resident, lease, binding or row write. Every
failure is swallowed so a shadow problem can never change the Wake outcome.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
from typing import Any, Mapping, Optional

from chat import day_handoff

_LOG = logging.getLogger('proactive_cold_shadow')

_DEFAULT_OBSERVE_PATH = '/opt/frontend/proactive_cold_shadow.jsonl'
_DEFAULT_CHAT_ID = 'default'

# Hot-check reasons a formal Chat turn would also respawn on (design 4.5).
COLD_STARTABLE_REASONS = frozenset((
    'no_local_binding',
    'resident_stale:idle',
    'resident_stale:soft_context',
    'resident_stale:model_changed',
))
# Daily cold-start cap per chat day (design Q3), overridable in runtime_config.
_DEFAULT_DAILY_CAP = {'morning': 1, 'self_trigger': 2}
_CAP_CONFIG_KEY = 'PROACTIVE_COLD_START_DAILY_CAP_%s'


def observation_path() -> str:
    return os.environ.get('PROACTIVE_COLD_SHADOW_PATH', _DEFAULT_OBSERVE_PATH)


def _beijing_now() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def chat_day(ts: datetime.datetime) -> str:
    """Use the canonical chat-day boundary shared with formal runtime."""
    return day_handoff.chat_day_for_timestamp(ts)


def daily_cap(mode: str) -> int:
    default = _DEFAULT_DAILY_CAP.get(mode, 0)
    try:
        import config_store
        return int(config_store.get_int(_CAP_CONFIG_KEY % mode.upper(), default))
    except Exception:
        return default


def _counterfactual_state(
    path: str,
    mode: str,
    day: str,
) -> tuple[set[str], set[str]]:
    """Return consumed IDs globally and unique cold-start IDs for this day."""
    consumed_ids: set[str] = set()
    cold_start_ids_today: set[str] = set()
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('would_cold_start') is not True:
                    continue
                wake_run_id = str(row.get('wake_run_id') or '').strip()
                if not wake_run_id:
                    continue
                consumed_ids.add(wake_run_id)
                if row.get('mode') == mode and row.get('chat_day') == day:
                    cold_start_ids_today.add(wake_run_id)
    except FileNotFoundError:
        pass
    return consumed_ids, cold_start_ids_today


def _would_cold_start_count(path: str, mode: str, day: str) -> int:
    """Count distinct physical tasks, not JSONL retry rows."""
    _unused_consumed_ids, cold_start_ids_today = _counterfactual_state(path, mode, day)
    return len(cold_start_ids_today)


def _window_facts(db_path: str, chat_id: str) -> dict[str, Any]:
    from chat.context_window import ACTIVE_INTENT_STATUSES, INTENT_COMMITTED
    from chat.daily_context import _FORMAL_CONTEXT_PRED

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctx = conn.execute(
            f'SELECT id, context_epoch, resident_generation, window_mode, closed_at '
            f'FROM daily_contexts WHERE chat_id=? AND {_FORMAL_CONTEXT_PRED} '
            'ORDER BY context_epoch DESC LIMIT 1',
            (chat_id,),
        ).fetchone()
        statuses = tuple(ACTIVE_INTENT_STATUSES)
        switching = conn.execute(
            'SELECT 1 FROM context_switch_intents WHERE chat_id=? AND status IN (%s) LIMIT 1'
            % ','.join('?' for _ in statuses),
            (chat_id, *statuses),
        ).fetchone() is not None
        finalize_pending = conn.execute(
            'SELECT 1 FROM context_switch_intents WHERE chat_id=? AND status=? '
            'AND first_assistant_message_id IS NOT NULL AND first_turn_completed_at IS NULL '
            'LIMIT 1',
            (chat_id, INTENT_COMMITTED),
        ).fetchone() is not None
    finally:
        conn.close()
    return {
        'context_id': int(ctx['id']) if ctx else None,
        'context_epoch': int(ctx['context_epoch']) if ctx else None,
        'resident_generation': int(ctx['resident_generation']) if ctx else None,
        'window_mode': str(ctx['window_mode']) if ctx else None,
        'context_closed': bool(ctx and ctx['closed_at'] is not None),
        'switch_in_progress': switching,
        'first_turn_finalize_pending': finalize_pending,
    }


def evaluate(
    *,
    mode: str,
    hot_reason: str,
    window: Mapping[str, Any],
    already_today: int,
    cap: int,
) -> tuple[bool, list[str]]:
    """Pure S2/S3 decision: (would_cold_start, blockers)."""
    blockers = []
    if mode not in _DEFAULT_DAILY_CAP:
        blockers.append('mode_not_cold_startable')
    if hot_reason not in COLD_STARTABLE_REASONS:
        blockers.append('reason_not_cold_startable')
    if window.get('context_id') is None:
        blockers.append('no_open_context')
    if window.get('context_closed'):
        blockers.append('context_closed')
    if window.get('switch_in_progress'):
        blockers.append('switch_in_progress')
    if window.get('first_turn_finalize_pending'):
        blockers.append('first_turn_finalize_pending')
    if already_today >= cap:
        blockers.append('daily_cap_reached')
    return not blockers, blockers


def record_skip(
    *,
    mode: str,
    hot_reason: str,
    wake_run_id: str,
    db_path: str,
    chat_id: str = _DEFAULT_CHAT_ID,
    now: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    """Append one shadow observation for a cold-resident skip. Never raises."""
    try:
        mode = str(mode or '').strip()
        if mode not in _DEFAULT_DAILY_CAP:
            return None
        now = now or _beijing_now()
        path = observation_path()
        day = chat_day(now)
        try:
            window = _window_facts(db_path, chat_id)
            window_error = None
        except Exception as exc:
            window = {}
            window_error = type(exc).__name__
        cap = daily_cap(mode)
        consumed_ids, cold_start_ids_today = _counterfactual_state(path, mode, day)
        already = len(cold_start_ids_today)
        run_id = str(wake_run_id or '').strip()
        counterfactual_consumed = bool(run_id and run_id in consumed_ids)
        if counterfactual_consumed:
            would, blockers = False, ['already_would_cold_started']
        else:
            would, blockers = evaluate(
                mode=mode, hot_reason=str(hot_reason or ''), window=window,
                already_today=already, cap=cap,
            )
            if window_error:
                would, blockers = False, blockers + ['window_read_failed:' + window_error]
        record = {
            'observed_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'chat_day': day,
            'wake_run_id': str(wake_run_id or ''),
            'mode': mode,
            'hot_reason': str(hot_reason or ''),
            'would_cold_start': would,
            'counterfactual_consumed': counterfactual_consumed,
            'blockers': blockers,
            'cold_starts_today_before': already,
            'daily_cap': cap,
            **window,
            'shadow_only': True,
        }
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
        return record
    except Exception as exc:
        _LOG.warning('proactive cold shadow skipped: %s', exc)
        return None
