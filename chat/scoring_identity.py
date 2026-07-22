"""Chat turn scoring identity — resolve message_id before score_async.

Normal send: new message_id from /api/chat/send.
Redo/regen: reuse the user message id that preceded the deleted assistant row.
Edit: new message row + new message_id (edited text must not reuse old shadow identity).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from chat.interaction_state import USER_AUTHOR_SQL

_log = logging.getLogger(__name__)

_AI_AUTHORS = ('fyodor', 'claude', 'assistant')


def parse_scoring_message_id(value: Any) -> Optional[int]:
    """Strict positive int for score_async; rejects bool/str/float/None."""
    if value is None:
        return None
    try:
        import internal_state_store as store
        return store.require_positive_message_id(value, field='message_id')
    except Exception:
        return None


def find_user_message_before(conn, message_id: int) -> Optional[int]:
    """Latest user chat_messages id strictly before ``message_id``."""
    row = conn.execute(
        f'SELECT MAX(id) AS mid FROM chat_messages '
        f'WHERE id < ? AND {USER_AUTHOR_SQL}',
        (int(message_id),),
    ).fetchone()
    if not row:
        return None
    mid = row['mid'] if hasattr(row, 'keys') else row[0]
    if mid is None:
        return None
    return int(mid)


def message_already_scored(get_db_fn: Callable[[], Any], message_id: int) -> bool:
    """True when legacy proof or shadow user_scored already recorded."""
    mid = parse_scoring_message_id(message_id)
    if mid is None:
        return False
    conn = get_db_fn()
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='internal_state_score_applied'",
        ).fetchone()
        if row:
            hit = conn.execute(
                'SELECT 1 FROM internal_state_score_applied WHERE message_id=? LIMIT 1',
                (mid,),
            ).fetchone()
            if hit:
                return True
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='internal_state_events'",
        ).fetchone()
        if row:
            hit = conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_key=? LIMIT 1",
                (f'user_scored:{mid}',),
            ).fetchone()
            if hit:
                return True
    finally:
        conn.close()
    return False


def trigger_turn_scoring(
    *,
    user_excerpt: str,
    assistant_text: str,
    message_id: Any,
    get_db_fn: Callable[[], Any],
) -> bool:
    """Schedule async scoring when identity is valid and not yet scored."""
    mid = parse_scoring_message_id(message_id)
    if mid is None:
        _log.warning('skip score_async: missing or invalid message_id=%r', message_id)
        return False
    if message_already_scored(get_db_fn, mid):
        _log.info('skip score_async: message_id=%s already scored', mid)
        return False
    try:
        import emotion_engine as _ee
        excerpt = ((user_excerpt or '') + '\n' + (assistant_text or '')).strip()[:2000]
        _ee.score_async(excerpt, message_id=mid)
        return True
    except Exception:
        _log.exception('score_async failed for message_id=%s', mid)
        return False
