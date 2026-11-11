"""The two chat_messages indexes that replace full-table scans.

Both read paths are asserted by query plan on the SQL the code really executes
(captured with a trace callback), and their results are asserted identical to a
database without the indexes, including after inserts, updates and deletes.
"""
from __future__ import annotations

import ast
import json
import random
import re
import sqlite3
from pathlib import Path

import pytest

from chat import daily_continuity_shadow as shadow
from chat import daily_schema
from tools import memory_surface as ms

CONTEXT_ID = 7
CONTEXT_EPOCH = 3
APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

COLUMNS = (
    "id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, "
    "created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, "
    "cache_info TEXT, source_kind TEXT NOT NULL DEFAULT 'chat', attachments TEXT, "
    "image_url TEXT, file_url TEXT, file_name TEXT"
)


def new_db(indexed):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE chat_messages ({COLUMNS})")
    conn.execute(
        "CREATE TABLE daily_message_contexts (message_id INTEGER, context_id INTEGER, "
        "context_epoch INTEGER, resident_generation INTEGER, role TEXT)"
    )
    conn.execute(
        "CREATE TABLE wake_log (id INTEGER PRIMARY KEY, wake_run_id TEXT, "
        "context_id INTEGER, context_epoch INTEGER, chat_id TEXT)"
    )
    if indexed:
        assert daily_schema.ensure_chat_messages_scan_indexes(conn) is True
    return conn


AUTHORS = ["hayana", "Hayana", "HAYANA", "haya", "user", "USER", "assistant", "Fyodor",
           "claude", "tool", "system", "", None, "İstanbul", "ÀNGEL"]


def insert(conn, rng, message_id=None):
    kind = rng.choice(["chat", "chat", "chat", "wake", "wake", "system", "tool"])
    run_id = f"run-{rng.randint(1, 6)}"
    info = {"wake_run_id": run_id, "usage": {"in": rng.randint(1, 99)}}
    if kind == "wake":
        info.update(wake_mode="normal", canonical_chat_history=True, source="wake")
    cols = ("author, content, thinking, created_at, tool_calls, branches, branch_idx, "
            "cache_info, source_kind, attachments, image_url, file_url, file_name")
    values = (rng.choice(AUTHORS), f"text {rng.randint(0, 99)}", "", "2026-09-08 10:00:00",
              "[]", "[]", 0, json.dumps(info), kind, "[]", "", "", "")
    if message_id is None:
        cur = conn.execute(f"INSERT INTO chat_messages ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    else:
        cur = conn.execute(
            f"INSERT INTO chat_messages (id, {cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, *values))
    mid = cur.lastrowid
    if kind != "wake":
        conn.execute("INSERT INTO daily_message_contexts VALUES (?,?,?,1,'user')",
                     (mid, CONTEXT_ID, CONTEXT_EPOCH))
    return mid


def populate(conn, rng, rows=300):
    for _ in range(rows):
        insert(conn, rng)
    for index in range(1, 5):
        conn.execute("INSERT INTO wake_log (wake_run_id, context_id, context_epoch, chat_id) "
                     "VALUES (?,?,?,'default')", (f"run-{index}", CONTEXT_ID, CONTEXT_EPOCH))
    conn.commit()


def capture(conn, call):
    """Run ``call`` and return (result, the SELECT statements it executed)."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result = call()
    finally:
        conn.set_trace_callback(None)
    return result, [s for s in statements if s.lstrip().upper().startswith("SELECT")]


def plan(conn, statement):
    return " ; ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + statement))


def wake_statement(statements):
    matches = [s for s in statements if "source_kind='wake'" in s and "FROM chat_messages" in s]
    assert len(matches) == 1, statements
    return matches[0]


def turn_statement(statements):
    matches = [s for s in statements if "lower(author)" in s]
    assert len(matches) == 1, statements
    return matches[0]


# ---- the migration helper ------------------------------------------------------

def test_creates_both_indexes_with_the_intended_definitions():
    conn = new_db(indexed=True)
    rows = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='chat_messages' "
        "AND name LIKE 'idx_chat_messages_%'").fetchall())
    assert set(rows) == {"idx_chat_messages_wake", "idx_chat_messages_author_lower"}
    normalized = {k: re.sub(r"\s+", " ", v) for k, v in rows.items()}
    assert normalized["idx_chat_messages_wake"].endswith(
        "ON chat_messages(id) WHERE source_kind='wake'")
    assert normalized["idx_chat_messages_author_lower"].endswith(
        "ON chat_messages(lower(author))")


def test_is_idempotent_and_does_not_touch_existing_rows():
    conn = new_db(indexed=False)
    populate(conn, random.Random(1), rows=50)
    before = conn.execute("SELECT * FROM chat_messages ORDER BY id").fetchall()
    for _ in range(3):
        assert daily_schema.ensure_chat_messages_scan_indexes(conn) is True
    assert conn.execute("SELECT * FROM chat_messages ORDER BY id").fetchall() == before
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_chat_messages_%'"
    ).fetchone()[0] == 2


def test_returns_false_without_raising_until_the_columns_exist():
    empty = sqlite3.connect(":memory:")
    assert daily_schema.ensure_chat_messages_scan_indexes(empty) is False
    old = sqlite3.connect(":memory:")
    old.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT)")
    assert daily_schema.ensure_chat_messages_scan_indexes(old) is False
    assert not old.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    old.execute("ALTER TABLE chat_messages ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'chat'")
    assert daily_schema.ensure_chat_messages_scan_indexes(old) is True


def test_integrity_check_is_clean_after_building_on_existing_rows():
    conn = new_db(indexed=False)
    populate(conn, random.Random(2), rows=200)
    daily_schema.ensure_chat_messages_scan_indexes(conn)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_app_runs_the_index_migration_after_source_kind_exists():
    tree = ast.parse(APP_PATH.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_migrate_chat_columns")
    calls = [n.func.id for n in ast.walk(function)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    source_kind = calls.index("ensure_chat_messages_source_kind_logged")
    indexes = calls.index("ensure_chat_messages_scan_indexes")
    assert source_kind < indexes
    text = ast.get_source_segment(APP_PATH.read_text(), function)
    assert "except Exception" in text  # index failure must never block startup


# ---- query plans of the SQL the code really runs ----------------------------------

def run_scope_read(conn):
    return capture(conn, lambda: shadow.read_canonical_scope_rows(
        conn, context_id=CONTEXT_ID, context_epoch=CONTEXT_EPOCH))


def test_wake_query_scans_the_table_without_the_index_and_uses_it_with_it():
    plain = new_db(indexed=False)
    populate(plain, random.Random(3))
    _rows, statements = run_scope_read(plain)
    without = plan(plain, wake_statement(statements))
    assert "SCAN chat_messages" in without and "idx_chat_messages_wake" not in without

    indexed = new_db(indexed=True)
    populate(indexed, random.Random(3))
    _rows, statements = run_scope_read(indexed)
    with_index = plan(indexed, wake_statement(statements))
    assert "idx_chat_messages_wake" in with_index, with_index
    assert "SCAN chat_messages" not in with_index.replace(
        "SCAN chat_messages USING INDEX idx_chat_messages_wake", "")


def test_wake_query_with_a_cutoff_also_uses_the_index():
    conn = new_db(indexed=True)
    populate(conn, random.Random(4))
    _rows, statements = capture(conn, lambda: shadow.read_canonical_scope_rows(
        conn, context_id=CONTEXT_ID, context_epoch=CONTEXT_EPOCH, before_message_id=150))
    assert "idx_chat_messages_wake" in plan(conn, wake_statement(statements))


def test_turn_number_query_uses_the_expression_index_only_when_present():
    plain = new_db(indexed=False)
    populate(plain, random.Random(5))
    _n, statements = capture(plain, lambda: ms._turn_number(plain, 200))
    without = plan(plain, turn_statement(statements))
    assert "idx_chat_messages_author_lower" not in without

    indexed = new_db(indexed=True)
    populate(indexed, random.Random(5))
    _n, statements = capture(indexed, lambda: ms._turn_number(indexed, 200))
    with_index = plan(indexed, turn_statement(statements))
    assert "USING COVERING INDEX idx_chat_messages_author_lower" in with_index, with_index
    assert "SCAN chat_messages" not in with_index


# ---- results are identical with and without the indexes -----------------------------

def same_scope(a, b):
    ra, _ = run_scope_read(a)
    rb, _ = run_scope_read(b)
    assert ra == rb


def test_results_identical_on_a_populated_database():
    rng_a, rng_b = random.Random(6), random.Random(6)
    plain, indexed = new_db(False), new_db(True)
    populate(plain, rng_a)
    populate(indexed, rng_b)
    same_scope(plain, indexed)
    ids = [r[0] for r in plain.execute("SELECT id FROM chat_messages ORDER BY id")]
    for message_id in [0, 1, ids[len(ids) // 2], ids[-1], ids[-1] + 50]:
        assert ms._turn_number(plain, message_id) == ms._turn_number(indexed, message_id)


def test_results_identical_when_the_index_is_added_to_an_existing_database():
    conn = new_db(False)
    populate(conn, random.Random(7))
    before_scope, _ = run_scope_read(conn)
    before_turns = [ms._turn_number(conn, i) for i in range(0, 320, 7)]
    daily_schema.ensure_chat_messages_scan_indexes(conn)
    after_scope, _ = run_scope_read(conn)
    assert after_scope == before_scope
    assert [ms._turn_number(conn, i) for i in range(0, 320, 7)] == before_turns


def test_results_stay_identical_through_inserts_updates_and_deletes():
    plain, indexed = new_db(False), new_db(True)
    ra, rb = random.Random(8), random.Random(8)
    populate(plain, ra, rows=120)
    populate(indexed, rb, rows=120)
    steps = random.Random(9)
    for _ in range(150):
        action = steps.choice(["insert", "insert", "author", "kind", "delete", "truncate", "reinsert"])
        target = steps.randint(1, 140)
        for conn, rng_state in ((plain, ra), (indexed, rb)):
            if action == "insert":
                insert(conn, rng_state)
            elif action == "author":
                conn.execute("UPDATE chat_messages SET author=? WHERE id=?",
                             (AUTHORS[target % len(AUTHORS)], target))
            elif action == "kind":
                conn.execute("UPDATE chat_messages SET source_kind=? WHERE id=?",
                             (["chat", "wake", "system"][target % 3], target))
            elif action == "delete":
                conn.execute("DELETE FROM chat_messages WHERE id=?", (target,))
            elif action == "truncate":
                conn.execute("DELETE FROM chat_messages WHERE id>=?", (130 + target % 10,))
            else:  # id reuse after deletion
                conn.execute("DELETE FROM chat_messages WHERE id=?", (target,))
                insert(conn, rng_state, message_id=target)
            conn.commit()
        same_scope(plain, indexed)
        probe = steps.randint(0, 200)
        assert ms._turn_number(plain, probe) == ms._turn_number(indexed, probe)
    assert indexed.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_author_case_and_non_ascii_semantics_are_unchanged():
    # lower() in SQLite is ASCII-only; the expression index must agree with it.
    for indexed in (False, True):
        conn = new_db(indexed)
        for author in ("Hayana", "HAYA", "uSeR", "İstanbul", "ÀNGEL", None, ""):
            conn.execute("INSERT INTO chat_messages (author, content) VALUES (?, 'x')", (author,))
        conn.commit()
        assert ms._turn_number(conn, 100) == 3
