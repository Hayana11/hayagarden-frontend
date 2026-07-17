"""Resolve per-turn relay cost for chat cache_info."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Callable

from relay.channel_intelligence import (
    _QUOTA_PER_USD,
    _pick_pricing,
    _pricing_rows,
    _request_json,
    origin_from_url,
)


_PRICING_CACHE: dict[str, tuple[float, list[dict]]] = {}
_GROUP_RATIO_CACHE: dict[str, float] = {}
_CACHE_LOCK = threading.Lock()
_PRICING_TTL = 300
DB_PATH = "/opt/frontend/memories.db"


def estimate_quota(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    cache_creation_5m: int = 0,
    cache_creation_1h: int = 0,
    model_ratio: float,
    completion_ratio: float,
    cache_ratio: float = 0.1,
    cache_creation_ratio: float = 1.25,
    cache_creation_ratio_5m: float | None = None,
    cache_creation_ratio_1h: float | None = None,
    group_ratio: float = 1.0,
    per_request_price: float | None = None,
) -> int:
    """Mirror NewAPI anthropic-style token billing (see service/text_quota.go)."""
    if per_request_price is not None:
        return int(round(float(per_request_price) * float(group_ratio) * float(_QUOTA_PER_USD)))

    ratio_5m = cache_creation_ratio if cache_creation_ratio_5m is None else cache_creation_ratio_5m
    ratio_1h = cache_creation_ratio if cache_creation_ratio_1h is None else cache_creation_ratio_1h
    remaining_create = max(0, int(cache_creation) - int(cache_creation_5m) - int(cache_creation_1h))
    prompt_tokens = (
        int(input_tokens)
        + int(cache_read) * float(cache_ratio)
        + remaining_create * float(cache_creation_ratio)
        + int(cache_creation_5m) * float(ratio_5m)
        + int(cache_creation_1h) * float(ratio_1h)
    )
    billable = prompt_tokens + int(output_tokens) * float(completion_ratio)
    return int(round(billable * float(model_ratio) * float(group_ratio)))


def quota_to_usd(quota: int) -> float:
    return round(quota / float(_QUOTA_PER_USD), 4)


def fetch_billed_quota(
    origin: str,
    api_key: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    request_json: Callable = _request_json,
) -> tuple[int | None, float | None]:
    """Read the latest matching NewAPI token log row and return quota + group_ratio."""
    data, _ = request_json(
        "GET",
        f"{origin.rstrip('/')}/api/log/token?p=1&page_size=10",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=4,
    )
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None, None
    for row in data["data"]:
        if int(row.get("prompt_tokens") or 0) != int(input_tokens):
            continue
        if int(row.get("completion_tokens") or 0) != int(output_tokens):
            continue
        other = row.get("other")
        if isinstance(other, str):
            try:
                other = json.loads(other)
            except Exception:
                other = {}
        if not isinstance(other, dict):
            other = {}
        if int(other.get("cache_tokens") or 0) != int(cache_read):
            continue
        if int(other.get("cache_creation_tokens") or 0) != int(cache_creation):
            continue
        quota = row.get("quota")
        group_ratio = other.get("group_ratio")
        if isinstance(quota, (int, float)) and quota > 0:
            ratio = float(group_ratio) if group_ratio not in (None, "", 0) else None
            return int(quota), ratio
    return None, None


def _pricing_rows_cached(origin: str, api_key: str, request_json: Callable = _request_json) -> list[dict]:
    cache_key = f"{origin}|{api_key[:8]}"
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _PRICING_CACHE.get(cache_key)
        if cached and now - cached[0] < _PRICING_TTL:
            return cached[1]

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    data, _ = request_json("GET", f"{origin.rstrip('/')}/api/pricing", headers=headers, timeout=5)
    if not isinstance(data, dict) or not data.get("data"):
        # anonymous pricing may 401; try without key already failed
        with _CACHE_LOCK:
            cached = _PRICING_CACHE.get(cache_key)
            if cached:
                return cached[1]
        return []

    channel = {"name": "", "base_url": origin}
    rows = _pricing_rows(channel, data)
    with _CACHE_LOCK:
        _PRICING_CACHE[cache_key] = (now, rows)
    return rows


def _raw_pricing_row(origin: str, api_key: str, model: str, request_json: Callable = _request_json) -> dict | None:
    data, _ = request_json(
        "GET",
        f"{origin.rstrip('/')}/api/pricing",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
        timeout=5,
    )
    if not isinstance(data, dict):
        return None
    rows = data.get("data")
    if not isinstance(rows, list):
        return None
    channel = {"name": "", "base_url": origin}
    picked = _pick_pricing(channel, model, _pricing_rows(channel, data))
    if not picked:
        return None
    raw_name = picked.get("raw_name") or model
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("model_name") or "") == raw_name:
            return row
    return None


def _remember_group_ratio(api_key: str, group_ratio: float | None) -> float:
    if group_ratio in (None, 0, ""):
        with _CACHE_LOCK:
            return _GROUP_RATIO_CACHE.get(api_key[:12], 1.0)
    value = float(group_ratio)
    with _CACHE_LOCK:
        _GROUP_RATIO_CACHE[api_key[:12]] = value
    return value


def resolve_turn_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    cache_creation_5m: int = 0,
    cache_creation_1h: int = 0,
    api_url: str,
    api_key: str,
    model: str,
    request_json: Callable = _request_json,
) -> tuple[float | None, bool]:
    """Return (cost_usd, estimated). Prefer billed quota from relay log."""
    if not api_url or (not input_tokens and not output_tokens and not cache_read and not cache_creation):
        return None, False

    try:
        origin = origin_from_url(api_url)
    except Exception:
        return None, False

    billed_quota, billed_group_ratio = fetch_billed_quota(
        origin,
        api_key,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_creation=cache_creation,
        request_json=request_json,
    )
    if billed_quota:
        _remember_group_ratio(api_key, billed_group_ratio)
        return quota_to_usd(billed_quota), False

    raw = _raw_pricing_row(origin, api_key, model, request_json=request_json)
    if not raw:
        return None, True

    quota_type = str(raw.get("quota_type") or "0")
    group_ratio = _remember_group_ratio(api_key, billed_group_ratio)
    if quota_type == "1":
        price = raw.get("model_price")
        try:
            per_request = float(price)
        except (TypeError, ValueError):
            return None, True
        return quota_to_usd(estimate_quota(
            input_tokens=0,
            output_tokens=0,
            group_ratio=group_ratio,
            per_request_price=per_request,
        )), True

    def _num(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    quota = estimate_quota(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_creation=cache_creation,
        cache_creation_5m=cache_creation_5m,
        cache_creation_1h=cache_creation_1h,
        model_ratio=_num(raw.get("model_ratio"), 1.0),
        completion_ratio=_num(raw.get("completion_ratio"), 1.0),
        cache_ratio=_num(raw.get("cache_ratio"), 0.1),
        cache_creation_ratio=_num(raw.get("create_cache_ratio"), 1.25),
        group_ratio=group_ratio,
    )
    return quota_to_usd(quota), True


def enrich_cache_info(
    payload: dict,
    *,
    api_url: str,
    api_key: str,
    model: str,
    request_json: Callable = _request_json,
) -> dict:
    cost, estimated = resolve_turn_cost_usd(
        input_tokens=int(payload.get("input_tokens") or 0),
        output_tokens=int(payload.get("output_tokens") or 0),
        cache_read=int(payload.get("cache_read") or 0),
        cache_creation=int(payload.get("cache_creation") or 0),
        cache_creation_5m=int(payload.get("cache_creation_5m") or 0),
        cache_creation_1h=int(payload.get("cache_creation_1h") or 0),
        api_url=api_url,
        api_key=api_key,
        model=model,
        request_json=request_json,
    )
    if cost is not None and cost > 0:
        payload["cost_usd"] = cost
        payload["cost_estimated"] = estimated
    return payload
