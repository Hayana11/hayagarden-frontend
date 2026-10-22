"""Wake modes that run as one proactive round on the shared main Chat resident.

These modes reach the user through the single ``_CC_RESIDENT`` (UH-A1): one
read-only observation, then one provider-only trigger turn. A delivered round
is canonical conversation history for every mode listed here, so Chat mapping
and continuity must treat them alike.
"""
from __future__ import annotations

UNIFIED_MAIN_CHAT_WAKE_MODES = frozenset((
    'normal', 'morning', 'nightwatch', 'self_trigger',
))


def is_unified_main_chat_wake_mode(mode) -> bool:
    return (str(mode or '').strip() or 'normal') in UNIFIED_MAIN_CHAT_WAKE_MODES
