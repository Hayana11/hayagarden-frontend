"""The "出生" breakdown of one Daily turn: what happened between her message
arriving and the first word of the reply, in order, with how long each step took.

Phases are stamped on the turn's timing record (``plan.cold_start_diagnostic``)
by the gateway and the Daily runtime; process / provider milestones are stamped
by the resident. This module only turns those timestamps into the small summary
the chat page shows and keeps with the reply. Pure: no I/O, never raises.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

# A gap no phase accounts for is shown as its own row once it is this long, so
# the rows always add up to the total.
OTHER_MIN_MS = 100
MAX_PHASES = 24


def _now() -> float:
    return round(time.time(), 6)


def record_phase(
    diagnostic: Any,
    key: str,
    label: str,
    start: Optional[float],
    end: Optional[float],
    detail: Optional[str] = None,
) -> None:
    """Append one finished phase to the turn's timing record."""
    try:
        if not isinstance(diagnostic, dict) or start is None or end is None:
            return
        phases = diagnostic.setdefault('phases', [])
        if len(phases) >= MAX_PHASES:
            return
        phases.append({
            'key': str(key)[:40],
            'label': str(label)[:40],
            'start': float(start),
            'end': float(max(end, start)),
            'detail': (str(detail)[:80] if detail else None),
        })
    except Exception:
        pass


@contextmanager
def phase(diagnostic: Any, key: str, label: str) -> Iterator[dict[str, Any]]:
    """Time a block; set ``box['detail']`` inside it to describe what was loaded."""
    box: dict[str, Any] = {}
    start = _now()
    try:
        yield box
    finally:
        record_phase(diagnostic, key, label, start, _now(), box.get('detail'))


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _milestone_phases(diagnostic: dict[str, Any], first_content_at: float) -> list[dict[str, Any]]:
    """Process / provider steps from the resident's milestones, after the last stamped phase."""
    flush = _num(diagnostic.get('stdin_flush_at'))
    init = _num(diagnostic.get('claude_init_event_at'))
    spawn_end = _num(diagnostic.get('ensure_alive_end_at'))
    rows: list[dict[str, Any]] = []
    if flush is not None and spawn_end is not None and flush >= spawn_end:
        rows.append({'key': 'deliver', 'label': '送达 Claude', 'start': spawn_end, 'end': flush, 'detail': None})
    opened = flush
    if flush is not None and init is not None and flush <= init <= first_content_at:
        rows.append({'key': 'tools', 'label': '连接工具', 'start': flush, 'end': init, 'detail': 'MCP 初始化'})
        opened = init
    if opened is not None and opened <= first_content_at:
        rows.append({'key': 'first_word', 'label': '模型开口', 'start': opened, 'end': first_content_at,
                     'detail': None})
    return rows


def summarize(diagnostic: Any, *, first_content_at: Optional[float] = None) -> Optional[dict[str, Any]]:
    """{total_ms, turn_kind, phases: [{key, label, ms, detail}]}, or None without a start."""
    try:
        if not isinstance(diagnostic, dict):
            return None
        origin = _num(diagnostic.get('birth_started_at')) or _num(diagnostic.get('request_received_at'))
        if origin is None:
            return None
        end = _num(first_content_at)
        if end is None:
            end = _num(diagnostic.get('first_thinking_or_text_at'))
        if end is None:
            return None
        stamped = [
            dict(p) for p in diagnostic.get('phases') or ()
            if isinstance(p, dict) and _num(p.get('start')) is not None and _num(p.get('end')) is not None
        ]
        stamped.extend(_milestone_phases(diagnostic, end))
        stamped.sort(key=lambda p: (p['start'], p['end']))

        rows: list[dict[str, Any]] = []
        cursor = origin
        for item in stamped:
            start = max(float(item['start']), cursor)
            stop = min(float(item['end']), end)
            if stop < start:
                continue
            gap_ms = int(round((start - cursor) * 1000))
            if gap_ms >= OTHER_MIN_MS:
                rows.append({'key': 'other', 'label': '其他准备', 'ms': gap_ms, 'detail': None})
            rows.append({
                'key': item['key'],
                'label': item['label'],
                'ms': int(round((stop - start) * 1000)),
                'detail': item.get('detail'),
            })
            cursor = stop
        tail_ms = int(round((end - cursor) * 1000))
        if tail_ms >= OTHER_MIN_MS:
            rows.append({'key': 'other', 'label': '其他准备', 'ms': tail_ms, 'detail': None})
        return {
            'total_ms': int(round(max(0.0, end - origin) * 1000)),
            'turn_kind': str(diagnostic.get('turn_kind') or '')[:20] or None,
            'phases': rows[:MAX_PHASES],
        }
    except Exception:
        return None
