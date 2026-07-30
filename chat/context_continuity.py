"""Cross-turn context continuity helpers.

This module stays free of Flask/gateway imports so the ordering contract can be
unit-tested without importing the production application.
"""
import json

_AI_AUTHORS = ("fyodor", "claude", "assistant")
_LARGE_RETURN_TOOLS = {
    "web_search", "browse_github", "read_webpage", "pocket_html",
    "get_activity_summary", "codebase_describe_project",
    "codebase_read_file", "codebase_search_code",
    "codebase_find_references", "codebase_explain_history",
    "codebase_git_view", "mcp_load",
}


def capture_pending_wake_ids(get_db_fn):
    """Snapshot wake rows that must be visible to the current user turn."""
    from chat.window_identity import fetch_claimable_wake_ids
    return fetch_claimable_wake_ids(get_db_fn)


def consume_wake_ids(get_db_fn, wake_ids):
    """Consume exactly the snapshotted rows, and only after reply persistence."""
    ids = []
    for value in wake_ids or ():
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return 0
    conn = get_db_fn()
    try:
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            "UPDATE wake_log SET consumed=1 "
            "WHERE consumed=0 AND id IN (" + placeholders + ")",
            ids,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def build_system_with_wake_claim(
    build_system_fn, get_db_fn, *, user_turn=False, **build_kwargs
):
    """Build first, return a claim to commit only after a reply is stored.

    build_system() reads wake_log consumed=0. Capturing ids does not mutate the
    rows, so the builder always sees the same pending wake snapshot.
    """
    wake_ids = capture_pending_wake_ids(get_db_fn) if user_turn else []
    system = build_system_fn(**build_kwargs)
    return system, wake_ids


def is_pending_user_turn(get_db_fn, message_id):
    """Validate a frontend turn id and reject replay after an AI response."""
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        return False
    if message_id <= 0:
        return False

    conn = get_db_fn()
    try:
        row = conn.execute(
            "SELECT author FROM chat_messages WHERE id=?", (message_id,)
        ).fetchone()
        if not row:
            return False
        author = row["author"] if hasattr(row, "keys") else row[0]
        if author in _AI_AUTHORS:
            return False

        latest_user = conn.execute(
            "SELECT MAX(id) FROM chat_messages "
            "WHERE author NOT IN ('fyodor','claude','assistant')"
        ).fetchone()[0]
        if latest_user != message_id:
            return False

        answered = conn.execute(
            "SELECT 1 FROM chat_messages WHERE id>? "
            "AND author IN ('fyodor','claude','assistant') LIMIT 1",
            (message_id,),
        ).fetchone()
        return answered is None
    finally:
        conn.close()


def _tool_caps(cap_small, cap_large, cap_per_message=None):
    if cap_small is not None and cap_large is not None:
        small, large = max(0, int(cap_small)), max(0, int(cap_large))
    else:
        try:
            import config_store
            small = config_store.get_int("TOOL_INJECT_MAX", 2000)
            large = config_store.get_int("TOOL_INJECT_MAX_MCP", 8000)
        except Exception:
            small, large = 2000, 8000
        small, large = max(0, int(small)), max(0, int(large))
    if cap_per_message is not None:
        per_message = max(0, int(cap_per_message))
    else:
        try:
            import config_store
            per_message = config_store.get_int("TOOL_INJECT_PER_MESSAGE_MAX", 12000)
            if per_message <= 0:
                per_message = config_store.get_int("TOOL_INJECT_TOTAL_MAX", 12000)
        except Exception:
            per_message = 12000
    return small, large, per_message


def _trim_tool_history_lines(lines, *, total_budget, estimate_tokens):
    if total_budget <= 0 or len(lines) <= 1:
        return lines
    header = lines[0]
    body = lines[1:]
    while body:
        joined = '\n'.join([header] + body)
        if estimate_tokens(joined) <= total_budget:
            return [header] + body
        body.pop(0)
    return [header]


def format_tool_history(tool_calls_json, cap_small=None, cap_large=None, cap_per_message=None):
    """Render persisted tool calls as text for the next model turn."""
    from chat.context_budget import default_estimate_tokens
    small, large, per_message = _tool_caps(cap_small, cap_large, cap_per_message)
    try:
        calls = json.loads(tool_calls_json)
    except (TypeError, ValueError):
        return ""
    if not isinstance(calls, list) or not calls:
        return ""

    lines = ["[上一轮我调用的工具与结果]"]
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name", "tool")
        try:
            args_text = json.dumps(call.get("args") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            args_text = str(call.get("args") or "")
        if len(args_text) > 300:
            args_text = args_text[:300] + "…"

        cap = large if name in _LARGE_RETURN_TOOLS else small
        result = str(call.get("result") or "")
        if len(result) > cap:
            result = result[:cap] + "…(已截断)"
        failed = "" if call.get("success", True) else "（失败）"
        lines.append("· %s%s %s\n  → %s" % (name, failed, args_text, result))
    if len(lines) <= 1:
        return ""
    if cap_per_message is None:
        return "\n".join(lines)
    return "\n".join(
        _trim_tool_history_lines(
            lines, total_budget=per_message, estimate_tokens=default_estimate_tokens,
        )
    )


def format_tool_history_legacy(tool_calls_json, cap_small=None, cap_large=None):
    """Main-branch tool history: per-tool caps only, no per-message budget."""
    return format_tool_history(tool_calls_json, cap_small=cap_small, cap_large=cap_large, cap_per_message=None)
