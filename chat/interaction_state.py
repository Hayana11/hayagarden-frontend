"""Unified interaction clock and touch path for chat + wake.

Authoritative user-idle fact for Wake:
  chat_messages 中最近一条真实用户消息的 created_at

Fail closed: when the clock cannot be read reliably, Wake must skip rather
than invent a huge idle (e.g. 999h) and accuse the user of disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import Callable, Optional

_LOG = logging.getLogger('interaction_state')
USER_AUTHOR_SQL = "author NOT IN ('fyodor','assistant','claude')"


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _parse_dt(value) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


@dataclass(frozen=True)
class InteractionClock:
    last_user_at: Optional[datetime.datetime]
    last_wake_message_at: Optional[datetime.datetime]
    user_idle_hours: Optional[float]
    effective_idle_hours: Optional[float]
    reliable: bool
    reason: str = ''

    @property
    def user_idle_minutes(self) -> Optional[float]:
        if self.user_idle_hours is None:
            return None
        return self.user_idle_hours * 60.0

    @property
    def effective_idle_minutes(self) -> Optional[float]:
        if self.effective_idle_hours is None:
            return None
        return self.effective_idle_hours * 60.0


def touch_user_interaction(get_db_fn: Optional[Callable] = None) -> None:
    """Compatibility hook after a user message is persisted.

    Stage D: drive rest / attachment discharge are NOT applied here.
    Authoritative user-message drive settlement is Canonical ``user_rule``
    on ``internal_state_v3``. Legacy ``emotion_engine.touch_interaction`` /
    ``desire.touch_hayana`` are retired no-ops (no DB writes).

    get_db_fn is accepted for call-site symmetry. Never raises into the chat path.
    """
    del get_db_fn  # reserved for future shared writes
    try:
        import emotion_engine as _ee
        _ee.touch_interaction()
    except Exception as exc:
        _LOG.warning('touch emotion_engine failed: %s', exc)
    try:
        import desire as _des
        _des.touch_hayana()
    except Exception as exc:
        _LOG.warning('touch desire failed: %s', exc)


def read_interaction_clock_from_conn(
    conn,
    now: Optional[datetime.datetime] = None,
) -> InteractionClock:
    """在调用方已打开的连接上读时钟；不关闭连接。

    供 Shadow bootstrap 同事务采集使用。永不抛出；失败 fail closed。
    """
    now = now or _now_beijing()
    try:
        last_user = conn.execute(
            "SELECT created_at FROM chat_messages "
            f"WHERE {USER_AUTHOR_SQL} ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_wake = conn.execute(
            "SELECT woke_at FROM wake_log WHERE action='message' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception as exc:
        _LOG.warning('read_interaction_clock_from_conn db error: %s', exc)
        return InteractionClock(
            last_user_at=None,
            last_wake_message_at=None,
            user_idle_hours=None,
            effective_idle_hours=None,
            reliable=False,
            reason='clock_unreadable',
        )

    user_raw = None
    if last_user is not None:
        user_raw = last_user['created_at'] if hasattr(last_user, 'keys') else last_user[0]
    wake_raw = None
    if last_wake is not None:
        wake_raw = last_wake['woke_at'] if hasattr(last_wake, 'keys') else last_wake[0]

    last_user_at = _parse_dt(user_raw)
    last_wake_at = _parse_dt(wake_raw)

    if last_user_at is None:
        return InteractionClock(
            last_user_at=None,
            last_wake_message_at=last_wake_at,
            user_idle_hours=None,
            effective_idle_hours=None,
            reliable=False,
            reason='missing_user_timestamp',
        )

    user_idle = max(0.0, (now - last_user_at).total_seconds() / 3600.0)
    effective = user_idle
    if last_wake_at is not None:
        wake_idle = max(0.0, (now - last_wake_at).total_seconds() / 3600.0)
        effective = min(effective, wake_idle)

    return InteractionClock(
        last_user_at=last_user_at,
        last_wake_message_at=last_wake_at,
        user_idle_hours=user_idle,
        effective_idle_hours=effective,
        reliable=True,
        reason='ok',
    )


def read_interaction_clock(
    get_db_fn: Callable,
    now: Optional[datetime.datetime] = None,
) -> InteractionClock:
    """Read the shared interaction clock. Never raises; fail closed on errors."""
    conn = None
    try:
        conn = get_db_fn()
        return read_interaction_clock_from_conn(conn, now=now)
    except Exception as exc:
        _LOG.warning('read_interaction_clock db error: %s', exc)
        return InteractionClock(
            last_user_at=None,
            last_wake_message_at=None,
            user_idle_hours=None,
            effective_idle_hours=None,
            reliable=False,
            reason='clock_unreadable',
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def wake_guard_reason(
    clock: InteractionClock,
    *,
    mode: str = 'normal',
    min_idle_minutes: float = 30.0,
    chat_busy: bool = False,
    wake_busy: bool = False,
) -> Optional[str]:
    """Return a skip reason, or None when Wake may call the model."""
    if wake_busy:
        return 'wake_in_progress'
    if chat_busy:
        return 'chat_generating'
    # Ordinary autonomous wake and fixed morning both require a reliable clock
    # and the idle floor. self_trigger bypasses the floor (an alarm must not
    # self-destruct), but still respects the busy guards above.
    # nightwatch/ritual/dream/summarize skip the idle floor.
    if mode in ('', 'normal', 'morning'):
        if not clock.reliable or clock.effective_idle_hours is None:
            return clock.reason or 'clock_unreliable'
        if clock.effective_idle_hours < (float(min_idle_minutes) / 60.0):
            return 'recent_interaction'
    return None
