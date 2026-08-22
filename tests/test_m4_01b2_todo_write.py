"""M4-01B2 Todo write adapter and idempotent execution contracts."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from tools.capability_state import CAPABILITY_STATE_KEY
from tools.confirmation_store import PendingActionStore
from tools.execution_fence import evaluate_tool_call
from tools.lease_signer import issue_turn_lease
from tools.todo_write_adapter import API_OWNER_ID, execution_payload, format_result
from tools.todo_write_execution import execute_todo_write


def _state_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.commit()
    conn.close()


def _todo_db(path):
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "done INTEGER DEFAULT 0, due_date TEXT, author TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    return conn


def _approved_action(tmp_path, monkeypatch, *, content="明天寄快递", due_date=None):
    state_path = tmp_path / "runtime.sqlite"
    _state_db(state_path)
    import tools.capability_state as capability_state
    monkeypatch.setattr(capability_state, "DB_PATH", str(state_path))
    db_path = tmp_path / "todos.sqlite"
    conn = _todo_db(db_path)
    default_lease = issue_turn_lease(
        turn_id="api-turn-1", turn_mode="chat", issued_from="default_policy",
    )
    decision = evaluate_tool_call(
        "add_todo", {"content": content, "due_date": due_date}, default_lease,
    )
    assert decision["lease_decision"] == "CAPABILITY_ASK_REQUIRED"
    store = PendingActionStore(
        conn, lease_issuer=issue_turn_lease, runtime_evaluator=evaluate_tool_call,
    )
    action = store.create_pending_action(
        capability_id="todo.write", tool_name="add_todo",
        tool_input={"content": content, "due_date": due_date},
        owner_id=API_OWNER_ID, turn_id=default_lease["turn_id"], tool_use_id="toolu-1",
    )
    request = {
        "pending_action_id": action.pending_action_id,
        "approval_id": action.approval_id,
        "confirmation_decision": "approve",
    }
    context = store.confirm(request, owner_id=API_OWNER_ID)
    assert context.evaluation["lease_decision"] == "ALLOW"
    assert context.turn_lease["issued_from"] == "user_confirmation"
    return conn, action, request


def test_default_policy_asks_and_does_not_insert(tmp_path, monkeypatch):
    conn, action, _request = _approved_action(tmp_path, monkeypatch)
    assert action.state == "approved"
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0


def test_execution_replays_exact_result_after_response_loss(tmp_path, monkeypatch):
    conn, action, _request = _approved_action(tmp_path, monkeypatch)
    payload = execution_payload(action, owner_id=API_OWNER_ID)
    first = execute_todo_write(conn, payload)
    assert execute_todo_write(conn, payload) == first
    assert first["ok"] is True
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1
    assert conn.execute(
        "SELECT state FROM confirmation_pending_actions WHERE pending_action_id=?",
        (action.pending_action_id,),
    ).fetchone()[0] == "completed"


def test_execution_survives_process_restart(tmp_path, monkeypatch):
    conn, action, _request = _approved_action(tmp_path, monkeypatch)
    payload = execution_payload(action, owner_id=API_OWNER_ID)
    first = execute_todo_write(conn, payload)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    restarted = sqlite3.connect(db_path, timeout=5)
    restarted.row_factory = sqlite3.Row
    assert execute_todo_write(restarted, payload) == first
    assert restarted.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1
    restarted.close()


def test_concurrent_retries_create_one_todo(tmp_path, monkeypatch):
    conn, action, _request = _approved_action(tmp_path, monkeypatch)
    payload = execution_payload(action, owner_id=API_OWNER_ID)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    results, errors = [], []

    def worker():
        db = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            results.append(execute_todo_write(db, payload))
        except Exception as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    check = sqlite3.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1
    check.close()


def test_pending_or_mismatched_identity_fails_without_insert(tmp_path, monkeypatch):
    conn, action, request = _approved_action(tmp_path, monkeypatch)
    bad = dict(request)
    bad["approval_id"] = "action_sha256:not-the-same"
    with pytest.raises(Exception):
        PendingActionStore(conn).resume_approved(bad, owner_id=API_OWNER_ID)
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0
    pending = PendingActionStore(
        conn, lease_issuer=issue_turn_lease, runtime_evaluator=evaluate_tool_call,
    ).create_pending_action(
        capability_id="todo.write", tool_name="add_todo",
        tool_input={"content": "未确认"}, owner_id=API_OWNER_ID, turn_id="api-turn-pending",
    )
    with pytest.raises(Exception):
        execute_todo_write(conn, execution_payload(pending, owner_id=API_OWNER_ID))
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0


def test_permanent_failure_is_durable_and_replayed(tmp_path, monkeypatch):
    conn, action, _request = _approved_action(tmp_path, monkeypatch, content="")
    payload = execution_payload(action, owner_id=API_OWNER_ID)
    first = execute_todo_write(conn, payload)
    assert execute_todo_write(conn, payload) == first
    assert first["ok"] is False
    assert first["code"] == "PERMANENT_INPUT_ERROR"
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0
    assert format_result(first, action.tool_input).startswith("工具执行失败")


def test_runtime_off_is_denied_before_execution(tmp_path, monkeypatch):
    state_path = tmp_path / "runtime.sqlite"
    _state_db(state_path)
    import tools.capability_state as capability_state
    from tools.capability_state import RUNTIME_STATE_OFF
    monkeypatch.setattr(capability_state, "DB_PATH", str(state_path))
    document = {"schema_version": 1, "states": {"todo.write": RUNTIME_STATE_OFF}}
    conn = sqlite3.connect(state_path)
    conn.execute(
        "INSERT INTO runtime_config(key,value,updated_at) VALUES(?,?,?)",
        (CAPABILITY_STATE_KEY, json.dumps(document), "now"),
    )
    conn.commit()
    conn.close()
    lease = issue_turn_lease(turn_id="api-off", turn_mode="chat", issued_from="default_policy")
    decision = evaluate_tool_call("add_todo", {"content": "不应写入"}, lease)
    assert decision["lease_decision"] == "DENIED_CAPABILITY"
