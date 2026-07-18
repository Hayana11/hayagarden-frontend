"""Usage accounting helpers for the autonomous wake agent loop.

The wake path can make several relay calls for one visible message (tool rounds
plus a final formatting round).  Keep every successful API round so cache and
cost data cannot disappear when the final text is persisted.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_round_from_result(
    result: Mapping[str, Any],
    *,
    index: int,
) -> Optional[dict[str, Any]]:
    """Normalize one non-stream Anthropic response into a Usage v2 round."""
    usage = result.get("usage") if isinstance(result, Mapping) else None
    if not isinstance(usage, Mapping):
        return None

    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
    cache_detail = usage.get("cache_creation")
    if not isinstance(cache_detail, Mapping):
        cache_detail = {}
    cache_creation_5m = _as_int(cache_detail.get("ephemeral_5m_input_tokens"))
    cache_creation_1h = _as_int(cache_detail.get("ephemeral_1h_input_tokens"))

    # A provider may omit usage entirely, but a present all-zero usage object is
    # still evidence for a completed round and must remain visible.
    return {
        "index": int(index),
        "complete": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "cache_creation_5m": cache_creation_5m,
        "cache_creation_1h": cache_creation_1h,
        "context_tokens": input_tokens + cache_read + cache_creation,
    }


def append_usage_round(
    rounds: list[dict[str, Any]],
    result: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    row = usage_round_from_result(result, index=len(rounds) + 1)
    if row is not None:
        rounds.append(row)
    return row


def build_wake_cache_info(
    rounds: Sequence[Mapping[str, Any]],
    *,
    elapsed_sec: float,
    cache_supported: Optional[bool],
    mode: str,
    model: Optional[str],
    payload_builder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate all wake calls and enrich them with the normal cost pipeline."""
    rows = [dict(row) for row in rounds if isinstance(row, Mapping)]
    totals = {
        "cache_read": sum(_as_int(row.get("cache_read")) for row in rows),
        "cache_creation": sum(_as_int(row.get("cache_creation")) for row in rows),
        "cache_creation_5m": sum(_as_int(row.get("cache_creation_5m")) for row in rows),
        "cache_creation_1h": sum(_as_int(row.get("cache_creation_1h")) for row in rows),
        "input_tokens": sum(_as_int(row.get("input_tokens")) for row in rows),
        "output_tokens": sum(_as_int(row.get("output_tokens")) for row in rows),
    }
    payload = payload_builder(
        **totals,
        elapsed_sec=round(max(0.0, float(elapsed_sec or 0.0)), 3),
        cache_supported=cache_supported,
        model=model,
    )
    payload.update({
        "v": 2,
        "provider": "api_relay",
        "source": "wake",
        "wake_mode": str(mode or "normal"),
        "num_rounds": len(rows),
        "rounds": rows,
        "last_round_context": rows[-1]["context_tokens"] if rows else 0,
        "max_round_context": max((row["context_tokens"] for row in rows), default=0),
    })
    return payload
