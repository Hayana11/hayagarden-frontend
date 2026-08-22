from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from tools import capability_state
from tools.capability_state import CAPABILITY_STATE_KEY, set_capability_state
from tools.confirmation_store import ConfirmationError, PendingActionStore
from tools.execution_fence import build_approval_id, capability_for_tool


BASE_TIME = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def canonical_store(tmp_path, monkeypatch):
    runtime_db = tmp_path / "runtime.sqlite3"
    runtime_conn = sqlite3.connect(runtime_db)
    runtime_conn.execute(
        "CREATE TABLE runtime_config ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at DATETIME DEFAULT (datetime('now')))"
    )
    runtime_conn.commit()
    runtime_conn.close()
    monkeypatch.setattr(capability_state, "DB_PATH", str(runtime_db))

    pending_conn = sqlite3.connect(tmp_path / "confirmation.sqlite3")
    store = PendingActionStore(
        pending_conn,
        id_factory=iter(["canonical-action"]).__next__,
    )
    yield tmp_path, pending_conn, store
    pending_conn.close()


def create_action(store):
    return store.create_pending_action(
        capability_id="todo.write",
        tool_name="mcp__internal__add_todo",
        tool_input={"content": "买牛奶"},
        owner_id="conversation-1",
        turn_id="turn-1",
        now=BASE_TIME,
    )


def confirm_request(action):
    return {
        "pending_action_id": action.pending_action_id,
        "approval_id": action.approval_id,
        "confirmation_decision": "approve",
    }


def test_real_canonical_signer_and_fence_allow_confirmation(canonical_store):
    _, _, store = canonical_store
    action = create_action(store)

    assert action.approval_id == build_approval_id(
        "todo.write",
        "mcp__internal__add_todo",
        {"content": "买牛奶"},
    )

    context = store.confirm(
        confirm_request(action),
        owner_id="conversation-1",
        now=BASE_TIME,
    )

    assert context.turn_lease["issued_from"] == "user_confirmation"
    assert context.turn_lease["approval_ids"] == (action.approval_id,)
    assert "todo.write" in context.turn_lease["allowed_capabilities"]
    assert context.evaluation["lease_decision"] == "ALLOW"
    assert context.evaluation["approval_id"] == action.approval_id
    assert context.action.state == "approved"


def test_real_runtime_off_fails_closed_before_approval(canonical_store):
    _, _, store = canonical_store
    action = create_action(store)
    set_capability_state("todo.write", enabled=False)

    with pytest.raises(ConfirmationError) as exc:
        store.confirm(confirm_request(action), owner_id="conversation-1", now=BASE_TIME)

    assert exc.value.code == "DENIED_CAPABILITY"
    assert store.get(action.pending_action_id, now=BASE_TIME).state == "pending"


def test_real_runtime_state_malformed_fails_closed(canonical_store):
    _, pending_conn, store = canonical_store
    action = create_action(store)
    runtime_db = capability_state.DB_PATH
    conn = sqlite3.connect(runtime_db)
    conn.execute(
        "INSERT INTO runtime_config (key, value) VALUES (?, ?)",
        (CAPABILITY_STATE_KEY, "{not-json"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ConfirmationError) as exc:
        store.confirm(confirm_request(action), owner_id="conversation-1", now=BASE_TIME)

    assert exc.value.code == "DENIED_CAPABILITY"
    assert store.get(action.pending_action_id, now=BASE_TIME).state == "pending"
    assert pending_conn.execute(
        "SELECT state FROM confirmation_pending_actions WHERE pending_action_id=?",
        (action.pending_action_id,),
    ).fetchone()[0] == "pending"


def test_real_runtime_storage_failure_fails_closed(canonical_store, tmp_path, monkeypatch):
    _, _, store = canonical_store
    action = create_action(store)
    monkeypatch.setattr(
        capability_state,
        "DB_PATH",
        str(tmp_path / "missing-parent" / "runtime.sqlite3"),
    )

    with pytest.raises(ConfirmationError) as exc:
        store.confirm(confirm_request(action), owner_id="conversation-1", now=BASE_TIME)

    assert exc.value.code == "DENIED_CAPABILITY"
    assert store.get(action.pending_action_id, now=BASE_TIME).state == "pending"


def test_canonical_manifest_has_internal_write_binding_only():
    assert capability_for_tool("mcp__internal__add_todo") == "todo.write"
    assert capability_for_tool("add_todo") is None
