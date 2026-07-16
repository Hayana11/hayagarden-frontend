"""Hook assistant reply persistence to moments chat-collection finalize."""

from __future__ import annotations

import logging

import moments_intent
import moments_store

_LOG = logging.getLogger(__name__)


def resolve_user_message_id(turn_data) -> int | None:
    raw = (turn_data or {}).get('user_message_id')
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def after_assistant_persisted(
    *,
    memories_db_path: str,
    turn_data,
    assistant_message_id: int,
    conversation_id: str = 'hayana-chat',
) -> None:
    pending = moments_intent.pop_pending(conversation_id)
    if not pending:
        return
    user_message_id = resolve_user_message_id(turn_data)
    if user_message_id is None:
        _LOG.warning('moments chat collection skipped: missing user_message_id for turn')
        return
    try:
        moments_store.finalize_pending_chat_collection(
            memories_db_path=memories_db_path,
            user_message_id=user_message_id,
            assistant_message_id=int(assistant_message_id),
            previous_turns=pending.previous_turns,
            caption=pending.caption,
        )
    except Exception as exc:
        _LOG.warning('moments chat collection finalize failed: %s', exc)
