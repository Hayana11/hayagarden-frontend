"""Shared per-request chat turn lifecycle for moments chat-collection intents."""

from __future__ import annotations

from typing import Any, Callable

import moments_intent

DEFAULT_CONVERSATION_ID = 'hayana-chat'


def begin_turn(turn_data: dict[str, Any] | None = None, *, conversation_id: str = DEFAULT_CONVERSATION_ID) -> dict[str, Any]:
    moments_intent.clear_pending(conversation_id)
    return dict(turn_data or {})


def insert_user_message(
    get_db_fn: Callable[[], Any],
    turn_data: dict[str, Any],
    content: str,
) -> dict[str, Any]:
    turn_data = dict(turn_data)
    text = (content or '').strip()
    if not text:
        return turn_data
    conn = get_db_fn()
    try:
        cur = conn.execute(
            "INSERT INTO chat_messages (author,content) VALUES ('hayana',?)",
            (text,),
        )
        conn.commit()
        turn_data['user_message_id'] = int(cur.lastrowid)
    finally:
        conn.close()
    return turn_data


def release_turn(*, conversation_id: str = DEFAULT_CONVERSATION_ID, persisted: bool = False) -> None:
    if not persisted:
        moments_intent.clear_pending(conversation_id)
