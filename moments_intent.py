"""Request-local pending chat collection intents for collect_chat_moment."""

from __future__ import annotations

import threading
from dataclasses import dataclass

_local = threading.local()


@dataclass(frozen=True)
class PendingChatCollection:
    previous_turns: int
    caption: str


def _store() -> dict[str, PendingChatCollection]:
    bucket = getattr(_local, 'pending', None)
    if bucket is None:
        bucket = {}
        _local.pending = bucket
    return bucket


def set_pending(conversation_id: str, *, previous_turns: int, caption: str) -> None:
    turns = int(previous_turns)
    if turns < 0 or turns > 2:
        raise ValueError('previous_turns must be between 0 and 2')
    text = (caption or '').strip()
    if len(text) > 500:
        raise ValueError('caption too long')
    key = (conversation_id or 'default').strip() or 'default'
    _store()[key] = PendingChatCollection(previous_turns=turns, caption=text)


def pop_pending(conversation_id: str) -> PendingChatCollection | None:
    key = (conversation_id or 'default').strip() or 'default'
    return _store().pop(key, None)


def clear_pending(conversation_id: str) -> None:
    key = (conversation_id or 'default').strip() or 'default'
    _store().pop(key, None)
