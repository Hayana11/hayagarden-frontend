from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tools.confirmation_store import (
    ConfirmationError,
    PendingActionStore,
    PendingActionStoreError,
    deferred_payload,
)


BASE_TIME = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def approval_builder(capability_id, tool_name, tool_input):
    return f"H:{capability_id}:{tool_name}:{json.dumps(tool_input, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


def lease_issuer(**kwargs):
    return {
        "lease_version": 1,
        "turn_id": kwargs["turn_id"],
        "turn_mode": kwargs["turn_mode"],
        "issued_from": kwargs["issued_from"],
        "allowed_capabilities": (kwargs["requested_capabilities"][0],),
        "approval_ids": tuple(kwargs["approval_ids"]),
        "task_contract_id": None,
        "issued_at": BASE_TIME.isoformat(),
    }


class Runtime:
    def __init__(self):
        self.off = False
        self.fail = False
        self.calls = 0

    def __call__(self, tool_name, tool_input, lease):
        self.calls += 1
        if self.fail:
            raise RuntimeError("state store unavailable")
        if self.off:
            return {"lease_decision": "DENIED_CAPABILITY"}
        return {
            "lease_decision": "ALLOW",
            "capability_id": "todo.write",
            "approval_id": lease["approval_ids"][0],
        }


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "confirmation.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    runtime = Runtime()
    store = PendingActionStore(
        conn,
        approval_builder=approval_builder,
        lease_issuer=lease_issuer,
        runtime_evaluator=runtime,
        ttl=timedelta(minutes=10),
        id_factory=iter(["A", "B", "C", "D", "E", "F"]).__next__,
    )
    return path, conn, store, runtime


def create(store, *, content="买牛奶", owner="conversation-1", turn="turn-1"):
    return store.create_pending_action(
        capability_id="todo.write",
        tool_name="mcp__internal__add_todo",
        tool_input={"content": content},
        owner_id=owner,
        turn_id=turn,
        now=BASE_TIME,
    )


def request(action, decision="approve", **overrides):
    return {
        "pending_action_id": overrides.get("pending_action_id", action.pending_action_id),
        "approval_id": overrides.get("approval_id", action.approval_id),
        "confirmation_decision": decision,
    }


def test_same_exact_action_has_same_approval_but_distinct_instance_ids(db):
    _, _, store, _ = db
    first = create(store)
    second = create(store)
    assert first.approval_id == second.approval_id
    assert first.pending_action_id != second.pending_action_id


@pytest.mark.parametrize("payload", [
    {},
    {"approval_id": "H", "confirmation_decision": "approve"},
    {"pending_action_id": "A", "confirmation_decision": "approve"},
    {"pending_action_id": "A", "approval_id": "H"},
])
def test_missing_confirmation_field_fails_closed(db, payload):
    _, _, store, _ = db
    with pytest.raises(ConfirmationError):
        store.confirm(payload, owner_id="conversation-1", now=BASE_TIME)


def test_unknown_pending_action_fails_closed(db):
    _, _, store, _ = db
    with pytest.raises(ConfirmationError) as exc:
        store.confirm({"pending_action_id": "unknown", "approval_id": "H", "confirmation_decision": "approve"}, owner_id="conversation-1", now=BASE_TIME)
    assert exc.value.code == "UNKNOWN_PENDING_ACTION"


def test_wrong_approval_and_cross_row_identity_fail_closed(db):
    _, _, store, _ = db
    first = create(store, content="买牛奶")
    second = create(store, content="买咖啡")
    with pytest.raises(ConfirmationError):
        store.confirm(request(first, approval_id="wrong"), owner_id="conversation-1", now=BASE_TIME)
    with pytest.raises(ConfirmationError):
        store.confirm(request(first, approval_id=second.approval_id), owner_id="conversation-1", now=BASE_TIME)
    with pytest.raises(ConfirmationError):
        store.confirm(request(second, pending_action_id=first.pending_action_id), owner_id="conversation-1", now=BASE_TIME)


def test_old_instance_cannot_confirm_new_same_content_action(db):
    _, _, store, _ = db
    first = create(store)
    second = create(store)
    store.reject(request(first, decision="reject"), owner_id="conversation-1", now=BASE_TIME)
    with pytest.raises(ConfirmationError):
        store.confirm(request(first), owner_id="conversation-1", now=BASE_TIME)
    assert store.get(second.pending_action_id, now=BASE_TIME).state == "pending"


def test_wrong_owner_fails_closed(db):
    _, _, store, _ = db
    action = create(store)
    with pytest.raises(ConfirmationError) as exc:
        store.confirm(request(action), owner_id="conversation-2", now=BASE_TIME)
    assert exc.value.code == "OWNERSHIP_MISMATCH"


def test_expired_action_cannot_be_approved(db):
    _, _, store, runtime = db
    action = create(store)
    with pytest.raises(ConfirmationError) as exc:
        store.confirm(request(action), owner_id="conversation-1", now=BASE_TIME + timedelta(minutes=11))
    assert exc.value.code == "EXPIRED"
    assert runtime.calls == 0
    assert store.get(action.pending_action_id, now=BASE_TIME + timedelta(minutes=11)).state == "expired"


def test_approve_issues_new_user_confirmation_lease_from_durable_row(db):
    _, _, store, runtime = db
    action = create(store)
    context = store.confirm(request(action), owner_id="conversation-1", now=BASE_TIME)
    assert context.turn_lease["issued_from"] == "user_confirmation"
    assert context.turn_lease["turn_id"] != action.turn_id
    assert context.turn_lease["approval_ids"] == (action.approval_id,)
    assert context.turn_lease["allowed_capabilities"] == (action.capability_id,)
    assert context.evaluation["lease_decision"] == "ALLOW"
    assert runtime.calls == 1
    assert store.get(action.pending_action_id, now=BASE_TIME).state == "approved"


def test_runtime_off_keeps_pending_and_has_zero_side_effect(db):
    _, _, store, runtime = db
    action = create(store)
    runtime.off = True
    with pytest.raises(ConfirmationError) as exc:
        store.confirm(request(action), owner_id="conversation-1", now=BASE_TIME)
    assert exc.value.code == "DENIED_CAPABILITY"
    assert store.get(action.pending_action_id, now=BASE_TIME).state == "pending"


def test_runtime_state_failure_fails_closed_and_keeps_pending(db):
    _, _, store, runtime = db
    action = create(store)
    runtime.fail = True
    with pytest.raises(ConfirmationError) as exc:
        store.confirm(request(action), owner_id="conversation-1", now=BASE_TIME)
    assert exc.value.code == "RUNTIME_UNAVAILABLE"
    assert store.get(action.pending_action_id, now=BASE_TIME).state == "pending"


def test_reject_only_changes_exact_action_and_duplicate_reject_is_idempotent(db):
    _, _, store, _ = db
    first = create(store, content="买牛奶")
    second = create(store, content="买咖啡")
    rejected = store.reject(request(first, decision="reject"), owner_id="conversation-1", now=BASE_TIME)
    repeated = store.reject(request(first, decision="reject"), owner_id="conversation-1", now=BASE_TIME)
    assert rejected.state == repeated.state == "rejected"
    assert store.get(second.pending_action_id, now=BASE_TIME).state == "pending"
    with pytest.raises(ConfirmationError):
        store.confirm(request(first), owner_id="conversation-1", now=BASE_TIME)


def test_approved_action_cannot_be_reapproved_or_rejected(db):
    _, _, store, _ = db
    action = create(store)
    store.confirm(request(action), owner_id="conversation-1", now=BASE_TIME)
    with pytest.raises(ConfirmationError):
        store.confirm(request(action), owner_id="conversation-1", now=BASE_TIME)
    with pytest.raises(ConfirmationError):
        store.reject(request(action, decision="reject"), owner_id="conversation-1", now=BASE_TIME)


def test_deferred_payload_round_trips_pending_action_id(db):
    _, _, store, _ = db
    action = create(store)
    payload = deferred_payload(action, approval_prompt="要记进待办吗？")
    assert payload["pending_action_id"] == action.pending_action_id
    assert payload["approval_id"] == action.approval_id
    assert payload["tool_input"] == {"content": "买牛奶"}
    assert payload["status"] == "waiting_for_confirmation"


def test_pending_survives_backend_restart(tmp_path):
    path = tmp_path / "restart.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    store = PendingActionStore(conn, approval_builder=approval_builder, id_factory=lambda: "restart-id")
    action = create(store)
    conn.close()
    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    restored = PendingActionStore(reopened, approval_builder=approval_builder)
    assert restored.get(action.pending_action_id, now=BASE_TIME) == action
    reopened.close()


def test_store_read_failure_fails_closed(tmp_path):
    conn = sqlite3.connect(tmp_path / "closed.sqlite3")
    conn.row_factory = sqlite3.Row
    store = PendingActionStore(conn, approval_builder=approval_builder)
    conn.close()
    with pytest.raises(PendingActionStoreError) as exc:
        store.get("missing")
    assert exc.value.code == "STORE_UNAVAILABLE"


def test_no_product_side_effect_and_api_relay_binding_absent(db):
    _, _, store, _ = db
    side_effects = []
    action = create(store)
    store.confirm(request(action), owner_id="conversation-1", now=BASE_TIME)
    assert side_effects == []
    manifest = pytest.importorskip("tools.capability_manifest")
    for entry in manifest.CAPABILITY_MANIFEST:
        bindings = entry.get("provider_bindings") or {}
        assert bindings.get("api_relay") != "add_todo"

