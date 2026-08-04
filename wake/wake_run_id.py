"""Stable wake_run_id builders for scheduler → /wake payloads.

Same physical task retries must reuse the same id; cron slots are floored to
30-minute boundaries to match the */30 dream_wake schedule.
"""

from __future__ import annotations

import datetime

_CRON_SLOT_MINUTES = 30


def _slot_time(now: datetime.datetime) -> datetime.datetime:
    minute = (now.minute // _CRON_SLOT_MINUTES) * _CRON_SLOT_MINUTES
    return now.replace(minute=minute, second=0, microsecond=0)


def make_morning_wake_run_id(now: datetime.datetime) -> str:
    return f"morning-{now.strftime('%Y-%m-%d')}"


def make_normal_wake_run_id(now: datetime.datetime) -> str:
    slot = _slot_time(now)
    return f"normal-{slot.strftime('%Y-%m-%d-%H:%M')}"


def make_nightwatch_wake_run_id(now: datetime.datetime) -> str:
    slot = _slot_time(now)
    return f"nightwatch-{slot.strftime('%Y-%m-%d-%H:%M')}"


def make_self_trigger_wake_run_id(trigger_id: int) -> str:
    return f"self-trigger-{int(trigger_id)}"


def make_ritual_wake_run_id(ritual_type: str, now: datetime.datetime) -> str:
    rtype = (ritual_type or '').strip().lower()
    if not rtype:
        raise ValueError('ritual_type required')
    return f"ritual-{rtype}-{now.strftime('%Y-%m-%d')}"


# Modes that write Action outcomes + V3 wake_outcome on the live path.
_SETTLEMENT_WAKE_MODES_EXCLUDED = frozenset({'dream', 'summarize'})


def missing_live_wake_run_id(mode: str, *, dry_run: bool, wake_run_id: str) -> bool:
    """True when a live settlement Wake is missing outcome identity.

    dry_run / dream / summarize may omit ``wake_run_id``. Pure helper — no
    Flask / gateway imports. Production gateway must call this before any
    model path.
    """
    if dry_run:
        return False
    if str(mode or '').strip() in _SETTLEMENT_WAKE_MODES_EXCLUDED:
        return False
    return not str(wake_run_id or '').strip()
