"""Chat turn scoring identity — resolve message_id before score_async.

Normal send: new message_id from /api/chat/send.
Redo/regen: reuse the user message id that preceded the deleted assistant row.
Edit: new message row + new message_id (edited text must not reuse old shadow identity).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Tuple

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


def resolve_scoring_user_message(
    get_db_fn: Callable[[], Any],
    message_id: Any,
) -> Optional[Tuple[int, str]]:
    """Validate ``message_id`` and load authoritative user row text from DB."""
    mid = parse_scoring_message_id(message_id)
    if mid is None:
        return None
    conn = get_db_fn()
    try:
        row = conn.execute(
            f'SELECT id, content FROM chat_messages '
            f'WHERE id=? AND {USER_AUTHOR_SQL}',
            (mid,),
        ).fetchone()
        if not row:
            return None
        content = row['content'] if hasattr(row, 'keys') else row[1]
        return mid, str(content or '')
    finally:
        conn.close()


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
    assistant_text: str,
    message_id: Any,
    get_db_fn: Callable[[], Any],
) -> bool:
    """Schedule async scoring when identity resolves to a real user row."""
    resolved = resolve_scoring_user_message(get_db_fn, message_id)
    if resolved is None:
        _log.warning(
            'skip score_async: user message not found for message_id=%r',
            message_id,
        )
        return False
    mid, user_content = resolved
    if message_already_scored(get_db_fn, mid):
        _log.info('skip score_async: message_id=%s already scored', mid)
        return False
    excerpt = (user_content + '\n' + (assistant_text or '')).strip()[:2000]
    if not excerpt:
        _log.warning('skip score_async: empty excerpt for message_id=%s', mid)
        return False
    try:
        import emotion_engine as _ee
        _ee.score_async(excerpt, message_id=mid)
        return True
    except Exception:
        _log.exception('score_async failed for message_id=%s', mid)
        return False
