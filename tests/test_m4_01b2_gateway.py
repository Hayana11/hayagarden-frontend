"""Executable M4-01B2 API gateway confirmation seam contracts."""
from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from tools import capability_state
from tools.confirmation_store import PendingActionStore
from tools.execution_fence import evaluate_tool_call
from tools.lease_signer import issue_turn_lease
from tools.todo_write_execution import execute_todo_write


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "gateway.py"


def _gateway_functions(*names):
    tree = ast.parse(GATEWAY_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in selected} != set(names):
        raise AssertionError(f"gateway functions missing: {names}")
    namespace = {
        "json": json,
        "SSE_END": "\\n\\n",
        "get_db": None,
        "run_tool": lambda name, args: None,
        "_call_todo_execution": None,
        "_complete_api_todo_confirmation": None,
        "_api_confirmation_error": lambda exc: (
            str(getattr(exc, "code", "") or "CONFIRMATION_FAILED"),
            str(exc),
        ),
    }
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(GATEWAY_PATH), "exec"), namespace)
    return namespace


@pytest.fixture()
def gateway_fixture(tmp_path, monkeypatch):
    runtime_path = tmp_path / "runtime.sqlite"
    runtime = sqlite3.connect(runtime_path)
    runtime.execute(
        "CREATE TABLE runtime_config ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT)"
    )
    runtime.commit()
    runtime.close()
    monkeypatch.setattr(capability_state, "DB_PATH", str(runtime_path))

    todo_path = tmp_path / "todos.sqlite"
    seed = sqlite3.connect(todo_path)
    seed.row_factory = sqlite3.Row
    PendingActionStore(seed)
    seed.execute(
        "CREATE TABLE todos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "done INTEGER DEFAULT 0, due_date TEXT, author TEXT, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    seed.commit()
    seed.close()

    def get_db():
        conn = sqlite3.connect(todo_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    lease = issue_turn_lease(
        turn_id="api-turn-gateway",
        turn_mode="chat",
        issued_from="explicit_user_intent",
        requested_capabilities=("todo.write",),
    )
    return get_db, lease, todo_path


def _make_pending(helpers, get_db, lease):
    helpers["get_db"] = get_db
    conn = get_db()
    try:
        action = PendingActionStore(conn).create_pending_action(
            capability_id="todo.write",
            tool_name="add_todo",
            tool_input={"content": "gateway seam test", "due_date": None},
            owner_id="api-chat",
            turn_id=lease["turn_id"],
            tool_use_id="toolu-gateway-1",
        )
    finally:
        conn.close()
    return {
        "deferred_tool_use": True,
        "pending_action_id": action.pending_action_id,
        "approval_id": action.approval_id,
        "tool_use_id": action.tool_use_id,
    }


def test_dispatch_allows_direct_todo_write(gateway_fixture):
    get_db, lease, todo_path = gateway_fixture
    helpers = _gateway_functions("_dispatch_api_chat_tool")
    calls = []
    helpers["run_tool"] = lambda name, args: calls.append((name, args)) or "direct todo result"
    result = helpers["_dispatch_api_chat_tool"](
        "add_todo",
        {"content": "gateway seam test", "due_date": None},
        lease,
    )
    assert result == "direct todo result"
    assert calls == [("add_todo", {"content": "gateway seam test", "due_date": None})]
    check = sqlite3.connect(todo_path)
    assert check.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0
    check.close()


def test_normal_confirmation_executes_and_replays_through_gateway_helper(
    gateway_fixture,
):
    get_db, lease, todo_path = gateway_fixture
    helpers = _gateway_functions(
        "_dispatch_api_chat_tool",
        "_complete_api_todo_confirmation",
    )
    deferred = _make_pending(helpers, get_db, lease)
    calls = []

    def internal_execution(payload):
        calls.append(dict(payload))
        conn = get_db()
        try:
            return json.dumps(execute_todo_write(conn, payload), ensure_ascii=False)
        finally:
            conn.close()

    helpers["_call_todo_execution"] = internal_execution
    request = {
        "pending_action_id": deferred["pending_action_id"],
        "approval_id": deferred["approval_id"],
        "confirmation_decision": "approve",
    }
    action, result, text = helpers["_complete_api_todo_confirmation"](request)
    assert result["ok"] is True
    assert "已添加待办" in text
    assert action.state == "approved"
    assert len(calls) == 1

    replay_action, replay, replay_text = helpers[
        "_complete_api_todo_confirmation"
    ](request)
    assert replay == result
    assert replay_text == text
    assert replay_action.state == "completed"
    assert len(calls) == 2

    check = sqlite3.connect(todo_path)
    assert check.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1
    assert check.execute(
        "SELECT state FROM confirmation_pending_actions WHERE pending_action_id=?",
        (deferred["pending_action_id"],),
    ).fetchone()[0] == "completed"
    check.close()


def test_normal_reject_does_not_reach_internal_execution(gateway_fixture):
    get_db, lease, _todo_path = gateway_fixture
    helpers = _gateway_functions(
        "_dispatch_api_chat_tool",
        "_complete_api_todo_confirmation",
    )
    deferred = _make_pending(helpers, get_db, lease)
    calls = []
    helpers["_call_todo_execution"] = lambda payload: calls.append(payload)
    action, result, text = helpers["_complete_api_todo_confirmation"](
        {
            "pending_action_id": deferred["pending_action_id"],
            "approval_id": deferred["approval_id"],
            "confirmation_decision": "reject",
        }
    )
    assert result is None
    assert text == "已取消"
    check = get_db()
    try:
        assert PendingActionStore(check).get(deferred["pending_action_id"]).state == "rejected"
    finally:
        check.close()
    assert calls == []


def test_stream_confirmation_emits_result_and_done(gateway_fixture):
    get_db, lease, _todo_path = gateway_fixture
    helpers = _gateway_functions(
        "_dispatch_api_chat_tool",
        "_complete_api_todo_confirmation",
        "_stream_api_confirmation",
    )
    deferred = _make_pending(helpers, get_db, lease)
    helpers["_call_todo_execution"] = lambda payload: json.dumps(
        {"ok": True, "id": 42},
        ensure_ascii=False,
    )
    events = list(
        helpers["_stream_api_confirmation"](
            {
                "pending_action_id": deferred["pending_action_id"],
                "approval_id": deferred["approval_id"],
                "confirmation_decision": "approve",
            }
        )
    )
    joined = "".join(events)
    assert '"t": "tool_use"' in joined
    assert '"t": "tool_result"' in joined
    assert '"t": "done"' in joined
    assert '"ok": true' in joined


def test_runtime_off_never_reaches_internal_execution(gateway_fixture):
    get_db, lease, _todo_path = gateway_fixture
    helpers = _gateway_functions(
        "_dispatch_api_chat_tool",
        "_complete_api_todo_confirmation",
    )
    deferred = _make_pending(helpers, get_db, lease)
    capability_state.set_capability_state("todo.write", enabled=False)
    calls = []
    helpers["_call_todo_execution"] = lambda payload: calls.append(payload)

    with pytest.raises(Exception) as exc:
        helpers["_complete_api_todo_confirmation"](
            {
                "pending_action_id": deferred["pending_action_id"],
                "approval_id": deferred["approval_id"],
                "confirmation_decision": "approve",
            }
        )
    assert getattr(exc.value, "code", None) == "DENIED_CAPABILITY"
    assert calls == []
