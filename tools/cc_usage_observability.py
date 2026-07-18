"""Claude Code Usage 行李透视（阶段 1A）：成功回复的上下文组成观测。

口径与官方额度窗口（context_usage_collector）分开：
本模块只描述单 turn 可见上下文与缓存指纹，不改生成链路，不建失败生命周期表。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

OBSERVATION_VERSION = 1
CONTEXT_LAYOUT_VERSION = 1
ESTIMATION_METHOD = "heuristic_cjk1_ascii4_v1"
TZ_NAME = "Asia/Shanghai"
TZ_OFFSET = timezone(timedelta(hours=8))
CACHE_EXPIRY_IDLE_SECONDS = 3600

LIMITATIONS = [
    "只观测成功落库的 assistant 回复；失败 / 超时 / 客户端断开不进入本报表。",
    "token 字段均为本地启发式估算（CJK≈1、其他≈4 字符/token，向上取整），非官方 tokenizer。",
    "unattributed_bootstrap 可能包含 Claude Code 内部提示、工具协议开销、tokenizer 估算误差、"
    "序列化开销、未观测非文本 block、以及 provider usage 口径差异；不得直接解释为 MCP 工具大小。",
    "suspected_cache_expiry=true 仅表示疑似缓存过期或供应端驱逐，不宣称严格 TTL 到期。",
    "tool schema 仅在获得 mcp_list_tools 或真实 static_registry 时估算；否则为 null/unavailable。",
    "confidence 默认 low。",
]

# 进程级网关实例 ID：每个网关进程生成一次
_GATEWAY_INSTANCE_ID = uuid.uuid4().hex


def gateway_instance_id() -> str:
    return _GATEWAY_INSTANCE_ID


def estimate_tokens_heuristic_cjk1_ascii4_v1(text: Optional[str]) -> int:
    """确定性本地估算：CJK 字符约 1 token，其他文本约 4 字符/token，向上取整。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in str(text):
        o = ord(ch)
        if (
            0x4E00 <= o <= 0x9FFF
            or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF
            or 0x2E80 <= o <= 0x2EFF
            or 0x3000 <= o <= 0x303F
            or 0xFF00 <= o <= 0xFFEF
            or 0x3040 <= o <= 0x30FF
            or 0xAC00 <= o <= 0xD7AF
        ):
            cjk += 1
        else:
            other += 1
    return int(cjk + math.ceil(other / 4) if other else cjk)


def sha256_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def sha256_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def content_to_text(content: Any) -> tuple[str, int]:
    """把 resident 最终 content 压成文本副本；返回 (text, non_text_block_count)。"""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        texts: list[str] = []
        non_text = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
                else:
                    non_text += 1
            elif isinstance(block, str):
                texts.append(block)
            else:
                non_text += 1
        return "".join(texts), non_text
    return str(content), 0


def measure_static_parts(
    *,
    persona: str = "",
    stable_note: str = "",
    save_instr: str = "",
    full_system: Optional[str] = None,
) -> dict[str, Any]:
    """只读测量静态 system 各段；不得修改入参。"""
    persona_s = str(persona or "")
    note_s = str(stable_note or "")
    save_s = str(save_instr or "")
    if full_system is None:
        parts = [p for p in (persona_s, note_s, save_s) if p and p.strip()]
        full = "\n\n".join(parts)
    else:
        full = str(full_system)
    return {
        "full_system": full,
        "persona_tokens_estimate": estimate_tokens_heuristic_cjk1_ascii4_v1(persona_s),
        "stable_note_tokens_estimate": estimate_tokens_heuristic_cjk1_ascii4_v1(note_s),
        "save_instr_tokens_estimate": estimate_tokens_heuristic_cjk1_ascii4_v1(save_s),
        "static_system_tokens_estimate": estimate_tokens_heuristic_cjk1_ascii4_v1(full),
    }


def resolve_tool_schema(
    *,
    tool_schema_text: Optional[str] = None,
    tool_schema_source: Optional[str] = None,
    tool_count: Optional[int] = None,
    allowed_tool_count: Optional[int] = None,
) -> dict[str, Any]:
    """只有真实 mcp_list_tools / static_registry schema 才能估算。"""
    source = (tool_schema_source or "").strip() or None
    allowed = int(allowed_tool_count) if allowed_tool_count is not None else None
    if tool_schema_text is None or not str(tool_schema_text).strip():
        status = "unavailable"
        if source in ("mcp_list_tools", "static_registry") and tool_count is not None:
            status = "partial"
        return {
            "tool_schema_tokens_estimate": None,
            "tool_schema_measurement_status": status,
            "tool_schema_source": source,
            "tool_count": int(tool_count) if tool_count is not None else None,
            "allowed_tool_count": allowed,
            "tool_schema_sha256": None,
        }
    if source not in ("mcp_list_tools", "static_registry"):
        return {
            "tool_schema_tokens_estimate": None,
            "tool_schema_measurement_status": "unavailable",
            "tool_schema_source": source,
            "tool_count": int(tool_count) if tool_count is not None else None,
            "allowed_tool_count": allowed,
            "tool_schema_sha256": None,
        }
    text = str(tool_schema_text)
    return {
        "tool_schema_tokens_estimate": estimate_tokens_heuristic_cjk1_ascii4_v1(text),
        "tool_schema_measurement_status": "available",
        "tool_schema_source": source,
        "tool_count": int(tool_count) if tool_count is not None else None,
        "allowed_tool_count": allowed,
        "tool_schema_sha256": sha256_text(text),
    }


def build_context_breakdown(
    *,
    persona: str = "",
    stable_note: str = "",
    save_instr: str = "",
    full_system: str = "",
    cold_once_text: str = "",
    history_bootstrap_text: str = "",
    rolling_summary_text: str = "",
    rolling_summary_in_prompt: bool = False,
    state_text: str = "",
    state_mode: str = "none",
    memory_recall_text: str = "",
    group_delta_text: str = "",
    one_shot_text: str = "",
    user_text: str = "",
    final_content: Any = None,
    tool_result_text: str = "",
    tool_schema_text: Optional[str] = None,
    tool_schema_source: Optional[str] = None,
    tool_count: Optional[int] = None,
    allowed_tool_count: Optional[int] = None,
    is_cold: bool = False,
    first_round_context_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """在组装现场测量：只读取字符串副本，不修改、不重排、不重新拼装 prompt。"""
    static = measure_static_parts(
        persona=persona,
        stable_note=stable_note,
        save_instr=save_instr,
        full_system=full_system,
    )
    # 回归契约：测量不得改变传入 system / content 字节
    _ = static["full_system"].encode("utf-8")
    content_text, non_text = content_to_text(final_content)

    cold_est = estimate_tokens_heuristic_cjk1_ascii4_v1(cold_once_text)
    hist_est = estimate_tokens_heuristic_cjk1_ascii4_v1(history_bootstrap_text)
    if rolling_summary_in_prompt and rolling_summary_text:
        roll_est = estimate_tokens_heuristic_cjk1_ascii4_v1(rolling_summary_text)
        roll_present = True
    else:
        roll_est = 0
        roll_present = False
    state_est = estimate_tokens_heuristic_cjk1_ascii4_v1(state_text)
    mem_est = estimate_tokens_heuristic_cjk1_ascii4_v1(memory_recall_text)
    group_est = estimate_tokens_heuristic_cjk1_ascii4_v1(group_delta_text)
    one_est = estimate_tokens_heuristic_cjk1_ascii4_v1(one_shot_text)
    user_est = estimate_tokens_heuristic_cjk1_ascii4_v1(user_text)
    tool_result_est = estimate_tokens_heuristic_cjk1_ascii4_v1(tool_result_text)
    visible_payload = estimate_tokens_heuristic_cjk1_ascii4_v1(content_text)

    mode = state_mode if state_mode in ("snapshot", "delta", "none") else "none"
    if not state_text:
        mode = "none"

    known_visible = (
        int(static["static_system_tokens_estimate"])
        + cold_est
        + hist_est
        + roll_est
        + state_est
        + mem_est
        + group_est
        + one_est
        + user_est
        + tool_result_est
    )

    tools = resolve_tool_schema(
        tool_schema_text=tool_schema_text,
        tool_schema_source=tool_schema_source,
        tool_count=tool_count,
        allowed_tool_count=allowed_tool_count,
    )

    unattributed = None
    if is_cold and first_round_context_tokens is not None:
        deduct = known_visible
        if tools["tool_schema_measurement_status"] == "available" and tools["tool_schema_tokens_estimate"] is not None:
            deduct += int(tools["tool_schema_tokens_estimate"])
        unattributed = max(0, int(first_round_context_tokens) - deduct)

    return {
        "static_system_tokens_estimate": int(static["static_system_tokens_estimate"]),
        "persona_tokens_estimate": int(static["persona_tokens_estimate"]),
        "stable_note_tokens_estimate": int(static["stable_note_tokens_estimate"]),
        "save_instr_tokens_estimate": int(static["save_instr_tokens_estimate"]),
        "cold_once_tokens_estimate": cold_est,
        "history_bootstrap_tokens_estimate": hist_est,
        "rolling_summary_tokens_estimate": roll_est,
        "rolling_summary_source_present": roll_present,
        "state_tokens_estimate": state_est,
        "state_mode": mode,
        "memory_recall_tokens_estimate": mem_est,
        "group_delta_tokens_estimate": group_est,
        "one_shot_tokens_estimate": one_est,
        "user_tokens_estimate": user_est,
        "visible_payload_tokens_estimate": visible_payload,
        "known_visible_context_tokens_estimate": known_visible,
        "tool_schema_tokens_estimate": tools["tool_schema_tokens_estimate"],
        "tool_schema_measurement_status": tools["tool_schema_measurement_status"],
        "tool_schema_source": tools["tool_schema_source"],
        "tool_count": tools["tool_count"],
        "allowed_tool_count": tools["allowed_tool_count"],
        "tool_result_tokens_estimate": tool_result_est,
        "unattributed_bootstrap_tokens_estimate": unattributed,
        "estimation_method": ESTIMATION_METHOD,
        "confidence": "low",
        "non_text_block_count": non_text,
    }


def build_runtime(
    *,
    resident_generation: int,
    resident_pid: Optional[int],
    resident_turn_count: int,
    respawn_reason: Optional[str],
    idle_seconds_before_turn: Optional[float],
    is_cold: bool,
    static_system: str,
    mcp_config_path: Optional[str] = None,
    mcp_config_text: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    tool_schema_sha256: Optional[str] = None,
    claude_session_id: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    claude_code_version: Optional[str] = None,
    context_layout_version: int = CONTEXT_LAYOUT_VERSION,
    instance_id: Optional[str] = None,
) -> dict[str, Any]:
    if mcp_config_text is not None:
        mcp_sha = sha256_text(mcp_config_text)
    else:
        mcp_sha = sha256_file(mcp_config_path)
    idle = None
    if idle_seconds_before_turn is not None:
        idle = float(idle_seconds_before_turn)
        if idle < 0:
            idle = 0.0
    return {
        "gateway_instance_id": instance_id or gateway_instance_id(),
        "resident_generation": int(resident_generation),
        "resident_pid": int(resident_pid) if resident_pid is not None else None,
        "resident_turn_count": int(resident_turn_count),
        "respawn_reason": respawn_reason,
        "idle_seconds_before_turn": idle,
        "is_cold": bool(is_cold),
        "context_layout_version": int(context_layout_version),
        "static_system_sha256": sha256_text(static_system or ""),
        "mcp_config_sha256": mcp_sha,
        "allowed_tools_sha256": sha256_text(allowed_tools if allowed_tools is not None else ""),
        "tool_schema_sha256": tool_schema_sha256,
        "claude_session_id_sha256": sha256_text(claude_session_id) if claude_session_id else None,
        "model": model,
        "effort": effort,
        "claude_code_version": claude_code_version,
    }


def attach_observation(
    usage: Mapping[str, Any],
    *,
    context_breakdown: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """给成功回复的 Usage v2 增加可选观测字段；旧字段语义不变。"""
    out = dict(usage or {})
    out["v"] = 2
    out.setdefault("provider", "claude_code")
    out["observation_version"] = OBSERVATION_VERSION
    out["context_breakdown"] = dict(context_breakdown or {})
    out["runtime"] = dict(runtime or {})
    return out


def finalize_breakdown_with_usage(
    breakdown: Mapping[str, Any],
    usage: Mapping[str, Any],
    *,
    is_cold: bool,
) -> dict[str, Any]:
    """在拿到真实 rounds 后回填 unattributed（仍不重跑 builder）。"""
    out = dict(breakdown or {})
    rounds = list((usage or {}).get("rounds") or [])
    first = rounds[0] if rounds else None
    if not (is_cold and first):
        out["unattributed_bootstrap_tokens_estimate"] = (
            None if not is_cold else out.get("unattributed_bootstrap_tokens_estimate")
        )
        if not is_cold:
            out["unattributed_bootstrap_tokens_estimate"] = None
        return out
    ctx = int(first.get("context_tokens") or 0)
    known = int(out.get("known_visible_context_tokens_estimate") or 0)
    deduct = known
    if (
        out.get("tool_schema_measurement_status") == "available"
        and out.get("tool_schema_tokens_estimate") is not None
    ):
        deduct += int(out["tool_schema_tokens_estimate"])
    out["unattributed_bootstrap_tokens_estimate"] = max(0, ctx - deduct)
    return out


def first_round(usage: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    rounds = list((usage or {}).get("rounds") or [])
    if not rounds:
        return None
    r = rounds[0]
    return r if isinstance(r, dict) else None


def round_context_tokens(round_row: Mapping[str, Any]) -> int:
    if round_row.get("context_tokens") is not None:
        return int(round_row.get("context_tokens") or 0)
    return (
        int(round_row.get("input_tokens") or 0)
        + int(round_row.get("cache_read") or 0)
        + int(round_row.get("cache_creation") or 0)
    )


def is_cold_start(usage: Mapping[str, Any], runtime: Optional[Mapping[str, Any]] = None) -> bool:
    runtime = runtime or (usage or {}).get("runtime") or {}
    reason = usage.get("respawn_reason")
    if reason is None:
        reason = runtime.get("respawn_reason")
    turn = usage.get("resident_turn_count")
    if turn is None:
        turn = runtime.get("resident_turn_count")
    if reason not in (None, ""):
        return True
    try:
        return int(turn or 0) == 1
    except (TypeError, ValueError):
        return False


def is_no_respawn_cache_miss(usage: Mapping[str, Any], runtime: Optional[Mapping[str, Any]] = None) -> bool:
    if is_cold_start(usage, runtime):
        return False
    fr = first_round(usage)
    if not fr:
        return False
    return int(fr.get("cache_read") or 0) == 0 and int(fr.get("cache_creation") or 0) > 0


_FINGERPRINT_KEYS = (
    "gateway_instance_id",
    "resident_generation",
    "static_system_sha256",
    "mcp_config_sha256",
    "allowed_tools_sha256",
    "model",
    "effort",
)


def _fingerprint_complete(runtime: Mapping[str, Any]) -> bool:
    # model/effort 允许显式 null，但仍需键存在；缺失键 → incomplete
    for key in _FINGERPRINT_KEYS:
        if key not in runtime:
            return False
    # 这些哈希是硬要求；null 视为缺失
    for key in (
        "gateway_instance_id",
        "resident_generation",
        "static_system_sha256",
        "mcp_config_sha256",
        "allowed_tools_sha256",
    ):
        if runtime.get(key) is None:
            return False
    return True


def _fingerprints_equal(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    for key in _FINGERPRINT_KEYS:
        if a.get(key) != b.get(key):
            return False
    return True


def classify_suspected_cache_expiry(
    usage: Mapping[str, Any],
    *,
    prev_runtime: Optional[Mapping[str, Any]] = None,
) -> Optional[bool]:
    """三态：True / False / None(unknown)。"""
    runtime = (usage or {}).get("runtime") or {}
    if not isinstance(runtime, dict):
        runtime = {}
    if not is_no_respawn_cache_miss(usage, runtime):
        return False
    idle = runtime.get("idle_seconds_before_turn")
    try:
        idle_ok = idle is not None and float(idle) >= CACHE_EXPIRY_IDLE_SECONDS
    except (TypeError, ValueError):
        idle_ok = False
    if not idle_ok:
        return False
    if prev_runtime is None or not isinstance(prev_runtime, dict):
        return None
    if not _fingerprint_complete(runtime) or not _fingerprint_complete(prev_runtime):
        return None
    if not _fingerprints_equal(runtime, prev_runtime):
        return False
    return True


def parse_cache_info_row(raw: Any) -> tuple[str, Optional[dict[str, Any]]]:
    """返回 (kind, parsed)。kind: valid_v2 / legacy / invalid_json / empty。"""
    if raw is None:
        return "empty", None
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw).strip()
        if not text:
            return "empty", None
        try:
            data = json.loads(text)
        except Exception:
            return "invalid_json", None
        if not isinstance(data, dict):
            return "invalid_json", None
    version = data.get("v")
    try:
        version_i = int(version) if version is not None else 1
    except (TypeError, ValueError):
        version_i = 1
    if version_i >= 2 and data.get("provider") == "claude_code":
        return "valid_v2", data
    if version_i >= 2:
        return "valid_v2", data
    return "legacy", data


def _percentile(sorted_vals: Sequence[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return float(d0 + d1)


def _median(vals: Sequence[float]) -> Optional[float]:
    if not vals:
        return None
    return float(statistics.median(vals))


def _avg_with_count(vals: Sequence[float]) -> dict[str, Any]:
    clean = [float(v) for v in vals if v is not None]
    if not clean:
        return {"value": None, "sample_count": 0}
    return {
        "value": float(sum(clean) / len(clean)),
        "sample_count": len(clean),
    }


BREAKDOWN_AVG_KEYS = (
    "static_system_tokens_estimate",
    "persona_tokens_estimate",
    "stable_note_tokens_estimate",
    "save_instr_tokens_estimate",
    "cold_once_tokens_estimate",
    "history_bootstrap_tokens_estimate",
    "rolling_summary_tokens_estimate",
    "state_tokens_estimate",
    "memory_recall_tokens_estimate",
    "group_delta_tokens_estimate",
    "one_shot_tokens_estimate",
    "user_tokens_estimate",
    "visible_payload_tokens_estimate",
    "known_visible_context_tokens_estimate",
    "tool_schema_tokens_estimate",
    "tool_result_tokens_estimate",
    "unattributed_bootstrap_tokens_estimate",
)


def aggregate_cc_observability(
    rows: Sequence[Mapping[str, Any]],
    *,
    days: int = 14,
    now: Optional[datetime] = None,
    timezone_name: str = TZ_NAME,
) -> dict[str, Any]:
    """纯函数聚合。rows 每项至少含 created_at / cache_info。"""
    days = max(1, int(days))
    now_cn = now or datetime.now(TZ_OFFSET)
    if now_cn.tzinfo is None:
        now_cn = now_cn.replace(tzinfo=TZ_OFFSET)
    else:
        now_cn = now_cn.astimezone(TZ_OFFSET)
    end_date = now_cn.strftime("%Y-%m-%d")
    start_dt = (now_cn - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_date = start_dt.strftime("%Y-%m-%d")

    def _day_key(offset: int) -> str:
        return (start_dt + timedelta(days=offset)).strftime("%Y-%m-%d")

    daily_map: dict[str, dict[str, Any]] = {}
    for i in range(days):
        d = _day_key(i)
        daily_map[d] = {
            "date": d,
            "total_user_turns": 0,
            "total_model_rounds": 0,
            "input_tokens": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "output_tokens": 0,
            "cold_start_count": 0,
            "no_respawn_cache_miss_count": 0,
            "suspected_cache_expiry_count": 0,
            "suspected_cache_expiry_unknown_count": 0,
        }

    coverage = {
        "total_candidate_rows": 0,
        "valid_v2_rows": 0,
        "legacy_rows": 0,
        "invalid_json_rows": 0,
        "rows_with_breakdown": 0,
        "rows_missing_breakdown": 0,
        "breakdown_coverage_pct": None,
    }

    summary = {
        "total_user_turns": 0,
        "total_model_rounds": 0,
        "input_tokens": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "output_tokens": 0,
        "cold_start_count": 0,
        "no_respawn_cache_miss_count": 0,
        "suspected_cache_expiry_count": 0,
        "suspected_cache_expiry_unknown_count": 0,
        "median_last_round_context": None,
        "p90_last_round_context": None,
        "median_model_rounds": None,
        "cold_start_creation_share": None,
    }

    last_contexts: list[float] = []
    model_rounds_list: list[float] = []
    cold_first_creation_sum = 0
    all_creation_sum = 0
    breakdown_buckets: dict[str, list[float]] = {k: [] for k in BREAKDOWN_AVG_KEYS}

    prev_runtime: Optional[dict[str, Any]] = None
    ordered = sorted(
        rows,
        key=lambda r: (
            str(r.get("created_at") or ""),
            int(r.get("id") or 0),
        ),
    )

    for row in ordered:
        created = str(row.get("created_at") or "")
        day = created[:10]
        if day < start_date or day > end_date:
            continue
        coverage["total_candidate_rows"] += 1
        kind, data = parse_cache_info_row(row.get("cache_info"))
        if kind == "invalid_json":
            coverage["invalid_json_rows"] += 1
            continue
        if kind == "empty":
            continue
        if kind == "legacy":
            coverage["legacy_rows"] += 1
            # 兼容：旧字段计入 token 成本，分类字段保持 unknown/不造假
            bucket = daily_map.get(day)
            if bucket is None:
                continue
            inp = int(data.get("input_tokens") or 0)
            cr = int(data.get("cache_read") or 0)
            cc = int(data.get("cache_creation") or 0)
            outp = int(data.get("output_tokens") or 0)
            for target in (summary, bucket):
                target["total_user_turns"] += 1
                target["total_model_rounds"] += 1
                target["input_tokens"] += inp
                target["cache_read"] += cr
                target["cache_creation"] += cc
                target["output_tokens"] += outp
            all_creation_sum += cc
            coverage["rows_missing_breakdown"] += 1
            summary["suspected_cache_expiry_unknown_count"] += 1
            bucket["suspected_cache_expiry_unknown_count"] += 1
            prev_runtime = None
            continue

        coverage["valid_v2_rows"] += 1
        usage = data
        runtime = usage.get("runtime") if isinstance(usage.get("runtime"), dict) else {}
        breakdown = (
            usage.get("context_breakdown")
            if isinstance(usage.get("context_breakdown"), dict)
            else None
        )
        if breakdown:
            coverage["rows_with_breakdown"] += 1
            for key in BREAKDOWN_AVG_KEYS:
                if key in breakdown and breakdown[key] is not None:
                    try:
                        breakdown_buckets[key].append(float(breakdown[key]))
                    except (TypeError, ValueError):
                        pass
        else:
            coverage["rows_missing_breakdown"] += 1

        rounds = list(usage.get("rounds") or [])
        num_rounds = int(usage.get("num_rounds") or len(rounds) or 0)
        inp = int(usage.get("input_tokens") or 0)
        cr = int(usage.get("cache_read") or 0)
        cc = int(usage.get("cache_creation") or 0)
        outp = int(usage.get("output_tokens") or 0)
        last_ctx = usage.get("last_round_context")
        if last_ctx is None and rounds:
            last_ctx = round_context_tokens(rounds[-1])

        bucket = daily_map[day]
        for target in (summary, bucket):
            target["total_user_turns"] += 1
            target["total_model_rounds"] += num_rounds
            target["input_tokens"] += inp
            target["cache_read"] += cr
            target["cache_creation"] += cc
            target["output_tokens"] += outp

        model_rounds_list.append(float(num_rounds))
        if last_ctx is not None:
            last_contexts.append(float(last_ctx))

        # creation 加权：所有 model rounds
        if rounds:
            for r in rounds:
                if isinstance(r, dict):
                    all_creation_sum += int(r.get("cache_creation") or 0)
        else:
            all_creation_sum += cc

        cold = is_cold_start(usage, runtime)
        miss = is_no_respawn_cache_miss(usage, runtime)
        expiry = classify_suspected_cache_expiry(usage, prev_runtime=prev_runtime)

        if cold:
            summary["cold_start_count"] += 1
            bucket["cold_start_count"] += 1
            fr = first_round(usage)
            if fr:
                cold_first_creation_sum += int(fr.get("cache_creation") or 0)
            elif cc:
                cold_first_creation_sum += cc
        if miss:
            summary["no_respawn_cache_miss_count"] += 1
            bucket["no_respawn_cache_miss_count"] += 1
        if expiry is True:
            summary["suspected_cache_expiry_count"] += 1
            bucket["suspected_cache_expiry_count"] += 1
        elif expiry is None:
            summary["suspected_cache_expiry_unknown_count"] += 1
            bucket["suspected_cache_expiry_unknown_count"] += 1

        prev_runtime = runtime or None

    if coverage["valid_v2_rows"]:
        coverage["breakdown_coverage_pct"] = round(
            100.0 * coverage["rows_with_breakdown"] / coverage["valid_v2_rows"], 2
        )
    elif coverage["total_candidate_rows"] == 0:
        coverage["breakdown_coverage_pct"] = None
    else:
        coverage["breakdown_coverage_pct"] = 0.0

    last_contexts.sort()
    summary["median_last_round_context"] = _median(last_contexts)
    summary["p90_last_round_context"] = _percentile(last_contexts, 0.9)
    summary["median_model_rounds"] = _median(model_rounds_list)
    if all_creation_sum > 0:
        summary["cold_start_creation_share"] = float(cold_first_creation_sum) / float(all_creation_sum)
    else:
        summary["cold_start_creation_share"] = None

    breakdown_averages = {k: _avg_with_count(breakdown_buckets[k]) for k in BREAKDOWN_AVG_KEYS}

    return {
        "ok": True,
        "timezone": timezone_name,
        "start_date": start_date,
        "end_date": end_date,
        "summary": summary,
        "daily": [daily_map[_day_key(i)] for i in range(days)],
        "breakdown_averages": breakdown_averages,
        "coverage": coverage,
        "limitations": list(LIMITATIONS),
    }


def load_cache_info_rows(
    db_path: str,
    *,
    days: int = 14,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """SQLite read-only 读取候选 assistant 行。"""
    days = max(1, int(days))
    now_cn = now or datetime.now(TZ_OFFSET)
    if now_cn.tzinfo is None:
        now_cn = now_cn.replace(tzinfo=TZ_OFFSET)
    else:
        now_cn = now_cn.astimezone(TZ_OFFSET)
    start = (now_cn - timedelta(days=days - 1)).strftime("%Y-%m-%d") + " 00:00:00"
    uri = "file:%s?mode=ro" % db_path
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, created_at, cache_info FROM chat_messages "
            "WHERE author='assistant' AND created_at >= ? "
            "AND cache_info IS NOT NULL AND cache_info != '' "
            "ORDER BY created_at ASC, id ASC",
            (start,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def build_report_from_db(
    db_path: str,
    *,
    days: int = 14,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    rows = load_cache_info_rows(db_path, days=days, now=now)
    return aggregate_cc_observability(rows, days=days, now=now)


def format_report_text(report: Mapping[str, Any]) -> str:
    s = report.get("summary") or {}
    cov = report.get("coverage") or {}
    lines = [
        "Claude Code Usage 观测日报",
        "timezone: %s" % report.get("timezone"),
        "range: %s .. %s" % (report.get("start_date"), report.get("end_date")),
        "",
        "summary:",
        "  user_turns=%s model_rounds=%s" % (s.get("total_user_turns"), s.get("total_model_rounds")),
        "  input=%s cache_read=%s cache_creation=%s output=%s"
        % (s.get("input_tokens"), s.get("cache_read"), s.get("cache_creation"), s.get("output_tokens")),
        "  cold_start=%s no_respawn_cache_miss=%s"
        % (s.get("cold_start_count"), s.get("no_respawn_cache_miss_count")),
        "  suspected_cache_expiry=%s unknown=%s"
        % (s.get("suspected_cache_expiry_count"), s.get("suspected_cache_expiry_unknown_count")),
        "  median_last_round_context=%s p90=%s median_rounds=%s"
        % (s.get("median_last_round_context"), s.get("p90_last_round_context"), s.get("median_model_rounds")),
        "  cold_start_creation_share=%s" % s.get("cold_start_creation_share"),
        "",
        "coverage:",
        "  candidates=%s valid_v2=%s legacy=%s invalid_json=%s"
        % (
            cov.get("total_candidate_rows"),
            cov.get("valid_v2_rows"),
            cov.get("legacy_rows"),
            cov.get("invalid_json_rows"),
        ),
        "  with_breakdown=%s missing=%s coverage_pct=%s"
        % (
            cov.get("rows_with_breakdown"),
            cov.get("rows_missing_breakdown"),
            cov.get("breakdown_coverage_pct"),
        ),
        "",
        "breakdown_averages:",
    ]
    avgs = report.get("breakdown_averages") or {}
    for key in BREAKDOWN_AVG_KEYS:
        item = avgs.get(key) or {}
        lines.append("  %s: value=%s sample_count=%s" % (key, item.get("value"), item.get("sample_count")))
    lines.append("")
    lines.append("limitations:")
    for item in report.get("limitations") or []:
        lines.append("  - %s" % item)
    return "\n".join(lines) + "\n"


def detect_claude_code_version(
    runner: Optional[Callable[[], str]] = None,
) -> Optional[str]:
    """只记录，不作为开工门槛。默认不调用网络。"""
    if runner is not None:
        try:
            text = (runner() or "").strip()
            return text or None
        except Exception:
            return None
    # 观测路径默认不 spawn 外部进程；由调用方注入版本字符串
    return os.environ.get("CLAUDE_CODE_VERSION") or None
