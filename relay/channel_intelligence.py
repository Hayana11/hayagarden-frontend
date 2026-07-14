"""Model pricing and health aggregation for saved relay presets.

Adapted from ``blueberriely/newapi-channel-panel`` (MIT), commit
5395be4f5104a89de2ec11c7997d49580674ebad.  HayaGarden keeps its existing
SQLite-backed relay management and only reuses the useful aggregation ideas:

* NewAPI/One-API public pricing with account-group adjustment
* Uptime Kuma, NewAPI embedded status, and custom model-status probes
* exact full-model status matching, then exact route-stripped fallback

Unlike the standalone project, this module never serializes API keys back to a
client and does not need another web process.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
import time
from typing import Callable
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError


SOURCE_URL = "https://github.com/blueberriely/newapi-channel-panel"
_CACHE_TTL = 60
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
_QUOTA_PER_USD = Decimal("500000")


class ChannelInspectionError(RuntimeError):
    pass


def origin_from_url(url: str) -> str:
    raw = (url or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ChannelInspectionError("中转站地址必须是 http 或 https")
    return f"{parsed.scheme}://{parsed.netloc}"


def models_url_from_api_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise ChannelInspectionError("中转站地址为空")
    origin_from_url(raw)
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    if re.search(r"/messages$", raw):
        return re.sub(r"/messages$", "/models", raw)
    if re.search(r"/v1(?:/.*)?$", raw):
        return re.sub(r"/v1(?:/.*)?$", "/v1/models", raw)
    return raw + "/models"


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    payload=None,
    timeout: float = 10,
) -> tuple[object | None, dict | None]:
    body = None
    final_headers = {"Accept": "application/json,text/plain,*/*"}
    if headers:
        final_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    req = Request(url, data=body, method=method, headers=final_headers)
    try:
        with urlopen(req, timeout=max(0.25, timeout)) as response:
            raw = response.read()
        return json.loads(raw), None
    except HTTPError as exc:
        return None, {
            "status": exc.code,
            "auth_required": exc.code in (401, 403),
        }
    except Exception as exc:
        return None, {"error": str(exc)}


def _route_and_model(model_name: str) -> tuple[str | None, str]:
    text = (model_name or "").strip()
    match = re.match(r"^\[([^\]]+)\](.+)$", text)
    if not match:
        return None, text
    return match.group(1).strip() or None, match.group(2).strip() or text


def _norm(value: str) -> str:
    return re.sub(r"[\s_\-·|/｜:：()\[\]（）]+", "", (value or "").lower())


def _loose_match(left: str, right: str) -> bool:
    a, b = _norm(left), _norm(right)
    return bool(a and b and (a in b or b in a))


def _labels(channel: dict) -> list[str]:
    name = str(channel.get("name") or "").strip()
    labels = [name]
    for separator in (" · ", " | ", " / ", "｜"):
        if separator in name:
            labels.extend(part.strip() for part in name.split(separator))
    return [label for label in labels if label]


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _decimal_text(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_newapi_price(row: dict, group_ratio=None) -> str | None:
    """Return NewAPI's USD price per 1M tokens (or per-request price)."""
    quota_type = row.get("quota_type")
    model_price = row.get("model_price")
    model_ratio = row.get("model_ratio")
    completion_ratio = row.get("completion_ratio")
    cache_ratio = row.get("cache_ratio")
    create_cache_ratio = row.get("create_cache_ratio")
    factor = _decimal(group_ratio) or Decimal("1")
    base_rate = Decimal("2")

    if str(quota_type) == "1" and model_price not in (None, ""):
        value = _decimal(model_price)
        return f"${_decimal_text(value * factor)}/次" if value is not None else f"${model_price}/次"

    base = _decimal(model_ratio)
    parts: list[str] = []
    if base is not None:
        parts.append(f"输入 ${_decimal_text(base * factor * base_rate)}/M")
    elif model_ratio not in (None, ""):
        parts.append(f"输入 {model_ratio}")

    for label, multiplier in (
        ("输出", completion_ratio),
        ("缓存", cache_ratio),
        ("写入", create_cache_ratio),
    ):
        if multiplier in (None, ""):
            continue
        value = _decimal(multiplier)
        if base is not None and value is not None:
            parts.append(f"{label} ${_decimal_text(base * value * factor * base_rate)}/M")
        else:
            parts.append(f"{label} {multiplier}")
    return " / ".join(parts) or None


def _pricing_rows(channel: dict, data) -> list[dict]:
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    ratios = data.get("group_ratio") if isinstance(data.get("group_ratio"), dict) else {}
    channel_group = None
    channel_ratio = None
    for label in _labels(channel):
        for group, ratio in ratios.items():
            if _norm(label) == _norm(str(group)):
                channel_group, channel_ratio = str(group), ratio
                break
        if channel_group:
            break
    if not channel_group:
        candidates = [
            (str(group), ratio)
            for label in _labels(channel)
            for group, ratio in ratios.items()
            if _loose_match(label, str(group))
        ]
        if candidates:
            channel_group, channel_ratio = max(candidates, key=lambda pair: len(_norm(pair[0])))

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_name = str(row.get("model_name") or row.get("model") or row.get("id") or "")
        if not raw_name:
            continue
        route, clean_model = _route_and_model(raw_name)
        groups = [str(item) for item in row.get("enable_groups", []) if item is not None] if isinstance(row.get("enable_groups"), list) else []
        applied_group = None
        applied_ratio = None
        if channel_group and (not groups or any(_loose_match(group, channel_group) for group in groups)):
            applied_group, applied_ratio = channel_group, channel_ratio
        normalized.append({
            "raw_name": raw_name,
            "route": route,
            "model": clean_model,
            "groups": groups,
            "group": applied_group,
            "group_ratio": applied_ratio,
            "base_price": format_newapi_price(row),
            "price": format_newapi_price(row, applied_ratio),
        })
    return normalized


def _pick_pricing(channel: dict, model_id: str, rows: list[dict]) -> dict | None:
    _, clean_id = _route_and_model(model_id)
    matches = [
        row for row in rows
        if row["raw_name"] == model_id or row["model"] in (model_id, clean_id)
    ]
    if not matches:
        return None

    def score(row: dict) -> int:
        value = 10 if row["raw_name"] == model_id else 0
        if row.get("route") and any(_loose_match(label, row["route"]) for label in _labels(channel)):
            value += 4
        if any(_loose_match(label, group) for label in _labels(channel) for group in row.get("groups", [])):
            value += 3
        return value

    return max(matches, key=score)


def _pick_status(channel: dict, model_id: str, pricing: dict | None, statuses: list[dict]) -> dict | None:
    """Match full model name first, then the route-stripped name; never guess."""
    _, clean_id = _route_and_model(model_id)
    group_labels = _labels(channel)
    if pricing:
        if pricing.get("group"):
            group_labels.append(pricing["group"])
        group_labels.extend(pricing.get("groups") or [])

    def exact(value: str, target: str) -> bool:
        return bool(_norm(value) and _norm(value) == _norm(target))

    def best(candidates: list[dict]) -> dict | None:
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda status: int(any(
                _loose_match(label, str(status.get("group") or ""))
                for label in group_labels if label
            )),
        )

    full = [status for status in statuses if exact(str(status.get("name") or ""), model_id)]
    if full:
        return best(full)
    bare = [status for status in statuses if clean_id and exact(str(status.get("name") or ""), clean_id)]
    return best(bare)


def _model_options(data) -> list[dict]:
    raw = None
    if isinstance(data, dict):
        raw = data.get("data") or data.get("models") or data.get("results")
    elif isinstance(data, list):
        raw = data
    if not isinstance(raw, list):
        return []
    options = []
    for item in raw:
        if isinstance(item, str):
            options.append({"id": item})
            continue
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("name") or item.get("model_id") or "").strip()
        if not model_id:
            continue
        option = {"id": model_id}
        name = item.get("display_name") or item.get("label") or item.get("name")
        if name and str(name) != model_id:
            option["name"] = str(name)
        direct_price = next((item.get(key) for key in ("price", "prompt_price", "input_price", "price_prompt") if item.get(key) is not None), None)
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else None
        if direct_price not in (None, ""):
            option["price"] = str(direct_price)
        elif pricing:
            prompt_price = next((pricing.get(key) for key in ("prompt", "input", "input_cost") if pricing.get(key) is not None), None)
            if prompt_price not in (None, ""):
                try:
                    option["price"] = f"${float(prompt_price) * 1_000_000:.2f}/M"
                except Exception:
                    option["price"] = str(prompt_price)
        options.append(option)
    return options


def _walk_strings(value):
    if isinstance(value, str):
        yield value
        if value.strip().startswith(("{", "[")):
            try:
                yield from _walk_strings(json.loads(value))
            except Exception:
                pass
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _status_urls(status_url: str) -> list[tuple[str, str, str]]:
    raw = (status_url or "").strip()
    if not raw:
        return []
    origin = origin_from_url(raw)
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    match = re.search(r"/status/([^/?#]+)", parsed.path or "")
    slugs = [match.group(1)] if match else []
    parts = [part for part in (parsed.path or "").split("/") if part]
    if parts and parts[-1] not in ("api", "status", "status-page", "heartbeat"):
        slugs.append(parts[-1])
    slugs.extend(("api", "tree", "status", "main"))
    result = []
    for slug in dict.fromkeys(filter(None, slugs)):
        result.append((
            f"{origin}/api/status-page/{slug}",
            f"{origin}/api/status-page/heartbeat/{slug}",
            f"{origin}/status/{slug}",
        ))
    return result


def _remaining(deadline: float, cap: float) -> float | None:
    value = deadline - time.monotonic()
    return None if value <= 0 else max(0.25, min(cap, value))


def _uptime_kuma_status(
    channel: dict,
    origin: str,
    request_json: Callable,
    deadline: float,
) -> tuple[list[dict], str | None]:
    candidates = list(_status_urls(str(channel.get("status_url") or "")))
    timeout = _remaining(deadline, 1.5)
    config, _ = request_json("GET", f"{origin}/api/status", timeout=timeout or 0.25)
    if config is not None:
        for text in _walk_strings(config):
            if "/status/" in text:
                try:
                    candidates.extend(_status_urls(text))
                except ChannelInspectionError:
                    continue

    parsed = urlparse(origin)
    origins = [origin]
    if not channel.get("status_url"):
        parts = parsed.netloc.split(".")
        if len(parts) >= 3 and parts[0] in ("api", "www", "newapi"):
            origins.append(f"{parsed.scheme}://status.{'.'.join(parts[1:])}")
    for candidate_origin in origins:
        for slug in ("api", "tree", "status", "main"):
            candidates.append((
                f"{candidate_origin}/api/status-page/{slug}",
                f"{candidate_origin}/api/status-page/heartbeat/{slug}",
                f"{candidate_origin}/status/{slug}",
            ))

    seen = set()
    for status_api, heartbeat_api, source in candidates:
        if (status_api, heartbeat_api) in seen:
            continue
        seen.add((status_api, heartbeat_api))
        timeout = _remaining(deadline, 1.5)
        if timeout is None:
            return [], None
        status_data, error = request_json("GET", status_api, timeout=timeout)
        if error or not isinstance(status_data, dict) or not isinstance(status_data.get("publicGroupList"), list):
            continue
        timeout = _remaining(deadline, 1.5)
        heartbeat_data, _ = request_json("GET", heartbeat_api, timeout=timeout or 0.25)
        heartbeat_list = heartbeat_data.get("heartbeatList", {}) if isinstance(heartbeat_data, dict) else {}
        latest = {
            str(key): rows[-1]
            for key, rows in heartbeat_list.items()
            if isinstance(rows, list) and rows
        } if isinstance(heartbeat_list, dict) else {}
        summary = []
        for group in status_data["publicGroupList"]:
            if not isinstance(group, dict):
                continue
            for monitor in group.get("monitorList") or []:
                if not isinstance(monitor, dict):
                    continue
                mid = str(monitor.get("id") or "")
                heartbeat = latest.get(mid, {})
                summary.append({
                    "id": mid,
                    "name": str(monitor.get("name") or ""),
                    "group": str(group.get("name") or ""),
                    "status": heartbeat.get("status"),
                    "time": heartbeat.get("time"),
                    "message": heartbeat.get("msg") or "",
                    "ping": heartbeat.get("ping"),
                })
        return summary, source
    return [], None


def _newapi_status(channel: dict, origin: str, request_json: Callable, deadline: float) -> tuple[list[dict], str | None]:
    origins = []
    if channel.get("status_url"):
        origins.append(origin_from_url(str(channel["status_url"])))
    origins.append(origin)
    for candidate in dict.fromkeys(origins):
        timeout = _remaining(deadline, 2.5)
        if timeout is None:
            return [], None
        config, error = request_json("GET", f"{candidate}/api/model-status/embed/config/selected", timeout=timeout)
        if error or not isinstance(config, dict) or not config.get("success") or not isinstance(config.get("data"), list):
            continue
        models = [str(model) for model in config["data"] if model]
        if not models:
            continue
        window = str(config.get("time_window") or "24h")
        timeout = _remaining(deadline, 6)
        if timeout is None:
            return [], None
        batch, error = request_json(
            "POST",
            f"{candidate}/api/model-status/embed/status/batch?window={quote(window)}",
            payload=models,
            timeout=timeout,
        )
        if error or not isinstance(batch, dict) or not batch.get("success") or not isinstance(batch.get("data"), list):
            continue
        groups = {}
        for group in config.get("custom_groups") or []:
            if not isinstance(group, dict):
                continue
            for model in group.get("models") or []:
                groups[str(model)] = str(group.get("name") or group.get("id") or "")
        summary = []
        for row in batch["data"]:
            if not isinstance(row, dict):
                continue
            model = str(row.get("model_name") or row.get("display_name") or "")
            if not model:
                continue
            color = str(row.get("current_status") or "").lower()
            status = {"green": 1, "red": 0, "yellow": 2, "gray": 2, "grey": 2}.get(color)
            message = []
            if row.get("success_rate") not in (None, ""):
                try:
                    message.append(f"{float(row['success_rate']):.2f}%")
                except Exception:
                    message.append(f"{row['success_rate']}%")
            if row.get("total_requests") not in (None, ""):
                message.append(f"{row['total_requests']} req")
            summary.append({
                "id": model,
                "name": str(row.get("display_name") or model),
                "group": groups.get(model) or str(config.get("site_title") or "模型监控"),
                "status": status,
                "time": str(row.get("time_window") or window),
                "message": " · ".join(message),
                "ping": None,
            })
        if summary:
            return summary, str(channel.get("status_url") or candidate)
    return [], None


def _custom_status(channel: dict, request_json: Callable, deadline: float) -> tuple[list[dict], str | None]:
    status_url = str(channel.get("status_url") or "").strip()
    if not status_url:
        return [], None
    origin = origin_from_url(status_url)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": status_url}
    timeout = _remaining(deadline, 2.5)
    directory, error = request_json("GET", f"{origin}/api/v1/model-status/models", headers=headers, timeout=timeout or 0.25)
    targets = directory.get("models") if isinstance(directory, dict) else None
    if error or not isinstance(targets, list):
        return [], None
    clean = [
        {"group_name": str(item.get("group_name") or ""), "model_name": str(item.get("model_name") or "")}
        for item in targets if isinstance(item, dict) and item.get("group_name") and item.get("model_name")
    ]
    if not clean:
        return [], None
    timeout = _remaining(deadline, 6)
    data, error = request_json("POST", f"{origin}/api/v1/model-status", headers=headers, payload={"targets": clean}, timeout=timeout or 0.25)
    rows = data.get("models") if isinstance(data, dict) else None
    if error or not isinstance(rows, list):
        return [], None
    summary = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("model_name"):
            continue
        availability = row.get("availability_percent")
        try:
            numeric = float(availability)
            status = 0 if numeric <= 0 else 1 if numeric >= 99 else 2
            message = f"{numeric:.1f}%"
        except Exception:
            status, message = None, ""
        response = row.get("response_time_ms") if isinstance(row.get("response_time_ms"), dict) else {}
        if response.get("p50") not in (None, ""):
            message += f" · P50 {response['p50']}ms"
        summary.append({
            "id": f"{row.get('group_name', '')}\u0000{row['model_name']}",
            "name": str(row["model_name"]),
            "group": str(row.get("group_name") or "模型监控"),
            "status": status,
            "time": str(data.get("window_seconds") or ""),
            "message": message.strip(" ·"),
            "ping": response.get("p50"),
        })
    return (summary, status_url) if summary else ([], None)


def _status_summary(channel: dict, origin: str, request_json: Callable) -> tuple[list[dict], str | None]:
    deadline = time.monotonic() + 10
    try:
        summary, source = _uptime_kuma_status(channel, origin, request_json, deadline)
    except ChannelInspectionError:
        summary, source = [], None
    if summary:
        return summary, source
    try:
        summary, source = _newapi_status(channel, origin, request_json, deadline)
    except ChannelInspectionError:
        summary, source = [], None
    if summary:
        return summary, source
    try:
        return _custom_status(channel, request_json, deadline)
    except ChannelInspectionError:
        return [], None


def _cache_key(channel: dict, include_status: bool) -> str:
    payload = json.dumps({
        "id": channel.get("id"),
        "name": channel.get("name"),
        "url": channel.get("base_url"),
        "status": channel.get("status_url"),
        "key_hash": hashlib.sha256(str(channel.get("api_key") or "").encode()).hexdigest(),
        "include_status": include_status,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _quota_amount(value) -> tuple[float | None, float | None]:
    quota = _decimal(value)
    if quota is None:
        return None, None
    return float(quota), float(quota / _QUOTA_PER_USD)


def query_channel_balance(channel: dict, *, request_json: Callable = _request_json) -> dict:
    """Query NewAPI's read-only API-key quota endpoint without exposing the key."""
    api_url = str(channel.get("base_url") or "").strip()
    api_key = str(channel.get("api_key") or "")
    source = f"{origin_from_url(api_url)}/api/usage/token/"
    if not api_key:
        return {
            "supported": False,
            "error": "missing_key",
            "source": source,
        }

    data, error = request_json(
        "GET",
        source,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    if error:
        status = error.get("status")
        if status in (401, 403):
            reason = "unauthorized"
        elif status == 404:
            reason = "unsupported"
        else:
            reason = "unavailable"
        return {
            "supported": False,
            "error": reason,
            "source": source,
        }

    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return {
            "supported": False,
            "error": "invalid_response",
            "source": source,
        }

    granted_quota, granted_usd = _quota_amount(payload.get("total_granted"))
    used_quota, used_usd = _quota_amount(payload.get("total_used"))
    available_quota, available_usd = _quota_amount(payload.get("total_available"))
    unlimited = bool(payload.get("unlimited_quota"))
    expires_at = payload.get("expires_at")
    try:
        expires_at = int(expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0

    if not unlimited and all(value is None for value in (granted_quota, used_quota, available_quota)):
        return {
            "supported": False,
            "error": "invalid_response",
            "source": source,
        }
    return {
        "supported": True,
        "error": None,
        "source": source,
        "name": str(payload.get("name") or ""),
        "unlimited": unlimited,
        "expires_at": expires_at,
        "total_granted_quota": granted_quota,
        "total_used_quota": used_quota,
        "total_available_quota": available_quota,
        "total_granted_usd": granted_usd,
        "total_used_usd": used_usd,
        "total_available_usd": available_usd,
    }


def _session_cookie_header(value: str) -> str:
    """Keep only NewAPI's session cookie so unrelated browser cookies are never forwarded."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 4096 or "\r" in raw or "\n" in raw:
        raise ChannelInspectionError("Session Cookie 格式不正确")

    parsed = SimpleCookie()
    try:
        parsed.load(raw)
    except Exception:
        parsed = SimpleCookie()
    if "session" in parsed:
        session = parsed["session"].value.strip()
    elif ";" not in raw:
        session = raw.removeprefix("session=").strip()
    else:
        session = ""
    if not session:
        raise ChannelInspectionError("没有找到 session Cookie")
    return f"session={session}"


def normalize_console_credential(kind: str, secret: str) -> tuple[str, str]:
    normalized_kind = str(kind or "").strip().lower()
    raw = str(secret or "").strip()
    if normalized_kind == "access_token":
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not 8 <= len(raw) <= 4096 or any(char.isspace() for char in raw):
            raise ChannelInspectionError("控制台访问令牌格式不正确")
        return normalized_kind, raw
    if normalized_kind == "session_cookie":
        return normalized_kind, _session_cookie_header(raw)
    raise ChannelInspectionError("不支持的控制台凭据类型")


def query_channel_account_balance(
    channel: dict,
    *,
    credential_kind: str,
    credential_secret: str,
    user_id: str,
    request_json: Callable = _request_json,
) -> dict:
    """Query NewAPI account quota without serializing console credentials.

    The caller owns credential storage. This function only forwards the normalized secret for
    one upstream request and never includes it in the result or an exception message.
    """
    api_url = str(channel.get("base_url") or "").strip()
    source = f"{origin_from_url(api_url)}/api/user/self"
    normalized_user_id = str(user_id or "").strip()
    if not re.fullmatch(r"[1-9]\d{0,18}", normalized_user_id):
        raise ChannelInspectionError("New-Api-User 必须是数字用户 ID")
    normalized_kind, normalized_secret = normalize_console_credential(
        credential_kind,
        credential_secret,
    )
    headers = {"New-Api-User": normalized_user_id}
    if normalized_kind == "access_token":
        headers["Authorization"] = normalized_secret
    else:
        headers["Cookie"] = normalized_secret

    data, error = request_json(
        "GET",
        source,
        headers=headers,
        timeout=10,
    )
    if error:
        status = error.get("status")
        if status in (401, 403):
            reason = "unauthorized"
        elif status == 404:
            reason = "unsupported"
        else:
            reason = "unavailable"
        return {"supported": False, "error": reason, "source": source}

    if isinstance(data, dict) and data.get("success") is False:
        return {"supported": False, "error": "unauthorized", "source": source}

    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return {"supported": False, "error": "invalid_response", "source": source}

    remaining_quota, remaining_usd = _quota_amount(payload.get("quota"))
    used_quota, used_usd = _quota_amount(payload.get("used_quota"))
    if remaining_quota is None:
        return {"supported": False, "error": "invalid_response", "source": source}
    total_quota = remaining_quota + (used_quota or 0)
    total_usd = remaining_usd + (used_usd or 0)
    return {
        "supported": True,
        "error": None,
        "source": source,
        "credential_kind": normalized_kind,
        "remaining_quota": remaining_quota,
        "used_quota": used_quota,
        "total_quota": total_quota,
        "remaining_usd": remaining_usd,
        "used_usd": used_usd,
        "total_usd": total_usd,
    }


def inspect_channel(
    channel: dict,
    *,
    include_status: bool = True,
    force: bool = False,
    request_json: Callable = _request_json,
) -> dict:
    """Return safe model, price and status metadata for one saved relay."""
    api_url = str(channel.get("base_url") or "").strip()
    api_key = str(channel.get("api_key") or "")
    origin = origin_from_url(api_url)
    cache_key = _cache_key(channel, include_status)
    if not force:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and time.monotonic() - cached[0] < _CACHE_TTL:
                result = dict(cached[1])
                result["cached"] = True
                return result

    pricing_url = f"{origin}/api/pricing"
    pricing_data, pricing_error = request_json("GET", pricing_url, timeout=5)
    pricing_rows = _pricing_rows(channel, pricing_data)
    pricing_requires_auth = bool(pricing_error and pricing_error.get("auth_required"))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    models_url = models_url_from_api_url(api_url)
    model_data, model_error = request_json("GET", models_url, headers=headers, timeout=15)
    if model_error:
        detail = model_error.get("status") or model_error.get("error") or "unknown error"
        raise ChannelInspectionError(f"模型接口不可用：{detail}")
    options = _model_options(model_data)
    if not options:
        raise ChannelInspectionError("模型接口没有返回可识别的模型")

    statuses, status_source = _status_summary(channel, origin, request_json) if include_status else ([], None)
    for option in options:
        pricing = _pick_pricing(channel, option["id"], pricing_rows)
        if pricing:
            for key in ("route", "groups", "group", "group_ratio", "base_price", "price"):
                value = pricing.get(key)
                if value not in (None, "", []):
                    option[key] = value
        status = _pick_status(channel, option["id"], pricing, statuses)
        if status:
            option["status"] = status

    result = {
        "ok": True,
        "models": [option["id"] for option in options],
        "model_options": options,
        "status_summary": statuses,
        "pricing_requires_auth": pricing_requires_auth,
        "pricing_source": pricing_url if pricing_data is not None or pricing_requires_auth else None,
        "status_source": status_source,
        "cached": False,
        "source_project": SOURCE_URL,
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic(), result)
    return dict(result)
