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
    """Request routing: an empty request mode means normal."""
    return (str(mode or '').strip() or 'normal') in UNIFIED_MAIN_CHAT_WAKE_MODES


def is_unified_wake_provenance_mode(wake_mode) -> bool:
    """Persisted provenance: wake_mode must be recorded explicitly.

    A row missing wake_mode is never canonical, whatever else it claims.
    """
    return isinstance(wake_mode, str) and wake_mode in UNIFIED_MAIN_CHAT_WAKE_MODES
