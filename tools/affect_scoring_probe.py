"""Freshness probe for V3 Affect scoring (user_scored).

V3 Affect only moves when the async scorer applies a ``user_scored`` event.
If the scorer's provider stops answering, Affect silently freezes at its last
value. This probe looks at the latest completed Chat turn and reports when its
user message is still unscored after a grace period.

Read-only. Returns an error string for wake_health, or None when fine or when
there is nothing to judge yet.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import Optional

from chat.interaction_state import USER_AUTHOR_SQL

_ASSISTANT_AUTHOR_SQL = "author IN ('fyodor','assistant','claude')"
_TS_FMT = '%Y-%m-%d %H:%M:%S'


def _beijing_now() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _parse(ts) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(str(ts or '')[:19], _TS_FMT)
    except ValueError:
        return None


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def check_affect_scoring_freshness(
    db_path: str,
    *,
    now: Optional[datetime.datetime] = None,
    grace_minutes: float = 15.0,
) -> Optional[str]:
    now = now or _beijing_now()
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        for table in ('chat_messages', 'internal_state_v3', 'internal_state_events'):
            if not _has_table(conn, table):
                return None
        reply = conn.execute(
            f'SELECT id, created_at FROM chat_messages WHERE {_ASSISTANT_AUTHOR_SQL} '
            'ORDER BY id DESC LIMIT 1'
        ).fetchone()
        if reply is None:
            return None
        user = conn.execute(
            f'SELECT id FROM chat_messages WHERE id < ? AND {USER_AUTHOR_SQL} '
            'ORDER BY id DESC LIMIT 1',
            (int(reply[0]),),
        ).fetchone()
        if user is None:
            return None
        user_id = int(user[0])
        replied_at = _parse(reply[1])
        if replied_at is None or now - replied_at < datetime.timedelta(minutes=grace_minutes):
            return None
        state = conn.execute(
            'SELECT last_scored_message_id, updated_at FROM internal_state_v3 WHERE id=1'
        ).fetchone()
        watermark = int(state[0]) if state and state[0] is not None else None
        if watermark is not None and user_id <= watermark:
            return None  # scored, or superseded by a later scored message
        if conn.execute(
            'SELECT 1 FROM internal_state_events WHERE event_key=?',
            (f'user_scored:{user_id}',),
        ).fetchone():
            return None
        minutes = int((now - replied_at).total_seconds() // 60)
        last = state[1] if state else None
        return (
            f'情绪评分停滞：消息 #{user_id} 的回合已结束 {minutes} 分钟，仍未进入 V3 '
            f'（last_scored_message_id={watermark}，V3 最近更新 {last}）。'
            '检查评分 provider（DeepSeek 额度 / 402）与 gateway 日志。'
        )
    finally:
        conn.close()
