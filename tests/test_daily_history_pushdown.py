"""_fetch_current_day_history: id bounds pushed into SQL, results unchanged.

The reference below is the function exactly as it was before the pushdown (both
queries fetched the whole window and filtered by id in Python).
"""
from __future__ import annotations

import itertools
import random
import sqlite3
from typing import Any, Optional

import pytest

from chat import daily_history as dh
from chat.daily_schema import META_SOURCE_KIND_CUTOVER, ensure_daily_meta_table, set_meta_int

CTX, EPOCH = 7, 3
LOCAL_DAY = "2026-09-08"
START, NEXT_START = dh.chat_day_window(LOCAL_DAY)[1], dh.chat_day_window(LOCAL_DAY)[3]

COLUMNS = (
    "id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, "
    "created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, "
    "cache_info TEXT, source_kind TEXT NOT NULL DEFAULT 'chat', attachments TEXT, "
    "image_url TEXT, file_url TEXT, file_name TEXT"
)


def reference_fetch(
    *, boundary_message_id, local_day, db_path=None, after_message_id=None,
    exclude_message_id=None, up_to_message_id=None, context_id=None,
    context_epoch=None, include_assistant_wake=False,
) -> list[dict[str, Any]]:
    """Verbatim copy of the implementation before this change."""
    conn = dh._connect(db_path)
    try:
        cols = dh._table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url', 'file_url', 'file_name', 'attachments', 'cache_info'):
            if optional in cols:
                select_cols.append(optional)
        wake_contents = dh._wake_content_set(conn)
        cutover = dh.get_meta_int(conn, META_SOURCE_KIND_CUTOVER)

        def _to_item(r):
            if not dh.is_canonical_conversation_message(
                r, include_assistant_wake=include_assistant_wake,
                wake_contents=wake_contents, cutover_id=cutover,
            ):
                return None
            mid = int(r['id'])
            role = 'user' if str(r['author']).lower() in dh._USER_AUTHORS else 'assistant'
            return {
                'message_id': mid, 'role': role, 'author': str(r['author']),
                'content': dh._message_display_content(r),
                'image_url': str(r['image_url'] or '') if 'image_url' in r.keys() else '',
                'file_url': str(r['file_url'] or '') if 'file_url' in r.keys() else '',
                'file_name': str(r['file_name'] or '') if 'file_name' in r.keys() else '',
                'attachments': r['attachments'] if 'attachments' in r.keys() else '',
                'created_at': str(r['created_at'] or ''),
            }

        mapped_items = {}
        if context_id is not None and context_epoch is not None:
            for r in conn.execute(
                'SELECT %s FROM chat_messages m '
                'INNER JOIN daily_message_contexts dmc ON dmc.message_id = m.id '
                'WHERE dmc.context_id=? AND dmc.context_epoch=? '
                'ORDER BY m.id ASC' % ', '.join('m.' + c for c in select_cols),
                (int(context_id), int(context_epoch)),
            ).fetchall():
                mid = int(r['id'])
                if exclude_message_id is not None and mid == int(exclude_message_id):
                    continue
                if after_message_id is not None and mid <= int(after_message_id):
                    continue
                item = _to_item(r)
                if item is not None:
                    mapped_items[mid] = item

        _d, start_at, _end, next_start = dh.chat_day_window(local_day)
        other_mapped = frozenset()
        if context_id is not None:
            other_rows = conn.execute(
                'SELECT message_id FROM daily_message_contexts WHERE context_id != ?',
                (int(context_id),),
            ).fetchall()
            other_mapped = frozenset(int(r[0]) for r in other_rows)

        legacy_items = {}
        for r in conn.execute(
            'SELECT %s FROM chat_messages '
            'WHERE created_at >= ? AND created_at < ? AND id > ? '
            'ORDER BY id ASC' % ', '.join(select_cols),
            (start_at, next_start, int(boundary_message_id or 0)),
        ).fetchall():
            mid = int(r['id'])
            if mid in mapped_items or mid in other_mapped:
                continue
            if exclude_message_id is not None and mid == int(exclude_message_id):
                continue
            if after_message_id is not None and mid <= int(after_message_id):
                continue
            if up_to_message_id is not None and mid > int(up_to_message_id):
                continue
            item = _to_item(r)
            if item is not None:
                legacy_items[mid] = item

        rows_by_id = dict(mapped_items)
        for mid, item in legacy_items.items():
            rows_by_id.setdefault(mid, item)
        return [rows_by_id[k] for k in sorted(rows_by_id.keys())]
    finally:
        conn.close()


TIMES = [
    "2026-09-07 03:59:59",   # previous chat day
    "2026-09-07 12:00:00",
    START,                   # exactly the window start (included)
    "2026-09-08 09:30:00",
    "2026-09-08 23:59:59",
    "2026-09-09 03:59:59",   # last second of the window
    NEXT_START,              # exactly the next start (excluded)
    "2026-09-09 10:00:00",
]
AUTHORS = ["hayana", "user", "Hayana", "assistant", "fyodor", "claude", "system", "tool"]
KINDS = ["chat", "chat", "chat", "chat", "wake", "system", "workspace_job"]


def build_db(path, seed, rows=90):
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE chat_messages ({COLUMNS})")
    conn.execute(
        "CREATE TABLE daily_message_contexts (message_id INTEGER PRIMARY KEY, "
        "context_id INTEGER NOT NULL, context_epoch INTEGER NOT NULL, "
        "resident_generation INTEGER NOT NULL, role TEXT NOT NULL, "
        "created_at DATETIME NOT NULL DEFAULT (datetime('now','+8 hours')))"
    )
    conn.execute(
        "CREATE INDEX idx_daily_message_contexts_ctx ON daily_message_contexts(context_id, context_epoch)"
    )
    conn.execute("CREATE TABLE wake_log (id INTEGER PRIMARY KEY, content TEXT)")
    for index in range(1, rows + 1):
        kind = rng.choice(KINDS)
        content = rng.choice([f"message {index}", "", "  ", f"wake text {index % 5}",
                              f"has [[SAVE:note]] {index}", f"plain {index}"])
        conn.execute(
            "INSERT INTO chat_messages (id, author, content, thinking, created_at, tool_calls, "
            "branches, branch_idx, cache_info, source_kind, attachments, image_url, file_url, file_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (index, rng.choice(AUTHORS), content, "", rng.choice(TIMES), "[]", "[]", 0,
             "{}", kind, "[]", rng.choice(["", "/static/uploads/a.png"]), "", ""),
        )
        placement = rng.random()
        if placement < 0.45:
            conn.execute("INSERT INTO daily_message_contexts (message_id, context_id, context_epoch, "
                         "resident_generation, role) VALUES (?,?,?,1,'user')", (index, CTX, EPOCH))
        elif placement < 0.55:   # same context, another epoch
            conn.execute("INSERT INTO daily_message_contexts (message_id, context_id, context_epoch, "
                         "resident_generation, role) VALUES (?,?,?,1,'user')", (index, CTX, EPOCH - 1))
        elif placement < 0.70:   # another context entirely
            conn.execute("INSERT INTO daily_message_contexts (message_id, context_id, context_epoch, "
                         "resident_generation, role) VALUES (?,?,?,1,'user')", (index, CTX + 1, EPOCH))
    for index in range(5):
        conn.execute("INSERT INTO wake_log (content) VALUES (?)", (f"wake text {index}",))
    ensure_daily_meta_table(conn)
    set_meta_int(conn, META_SOURCE_KIND_CUTOVER, rng.choice([0, 20, 45]))
    conn.commit()
    conn.close()
    return path


def values_around(rng, rows=90):
    """None, the edges, ids of real rows, ids past the end."""
    picks = [None, 0, 1, rows, rows + 5]
    picks += [rng.randint(2, rows - 1) for _ in range(5)]
    return picks


def call(fn, db, **kwargs):
    return fn(local_day=LOCAL_DAY, db_path=db, **kwargs)


# ---- identical results -------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_new_implementation_equals_the_original_over_a_parameter_grid(tmp_path, seed):
    db = build_db(str(tmp_path / "chat.db"), seed)
    rng = random.Random(seed * 101)
    compared = non_empty = 0
    contexts = [(CTX, EPOCH), (None, None), (CTX, None), (None, EPOCH), (CTX + 1, EPOCH), (99, 1)]
    for _ in range(700):
        kwargs = dict(
            boundary_message_id=rng.choice(values_around(rng) + [None]),
            after_message_id=rng.choice(values_around(rng)),
            exclude_message_id=rng.choice(values_around(rng)),
            up_to_message_id=rng.choice(values_around(rng)),
            include_assistant_wake=rng.choice([False, True]),
        )
        kwargs["context_id"], kwargs["context_epoch"] = rng.choice(contexts)
        expected = call(reference_fetch, db, **kwargs)
        actual = call(dh._fetch_current_day_history, db, **kwargs)
        assert actual == expected, kwargs
        compared += 1
        non_empty += bool(expected)
    assert compared == 700
    assert non_empty > 200, "grid did not produce enough non-empty results"


def test_every_bound_exactly_on_a_row_id_and_at_the_edges(tmp_path):
    db = build_db(str(tmp_path / "chat.db"), 11)
    ids = [r[0] for r in sqlite3.connect(db).execute("SELECT id FROM chat_messages ORDER BY id")]
    bounds = [None, 0, 1, 2, 44, 45, 46, ids[-1] - 1, ids[-1], ids[-1] + 1]
    checked = 0
    for boundary, after, up_to, exclude in itertools.product(bounds, bounds, bounds, [None, 45, 46]):
        kwargs = dict(boundary_message_id=boundary if boundary is not None else 0,
                      after_message_id=after, up_to_message_id=up_to, exclude_message_id=exclude,
                      context_id=CTX, context_epoch=EPOCH)
        assert call(dh._fetch_current_day_history, db, **kwargs) == call(reference_fetch, db, **kwargs)
        checked += 1
    assert checked == len(bounds) ** 3 * 3


def test_mapped_rows_beyond_up_to_are_still_returned_like_the_original(tmp_path):
    """The mapped query never had an up_to bound: pushing one would drop these."""
    db = build_db(str(tmp_path / "chat.db"), 12)
    kwargs = dict(boundary_message_id=0, after_message_id=None, up_to_message_id=10,
                  exclude_message_id=None, context_id=CTX, context_epoch=EPOCH,
                  include_assistant_wake=True)
    expected = call(reference_fetch, db, **kwargs)
    assert any(item["message_id"] > 10 for item in expected), "fixture has no mapped row past up_to"
    assert call(dh._fetch_current_day_history, db, **kwargs) == expected


def test_window_edges_and_other_days_are_unchanged(tmp_path):
    db = build_db(str(tmp_path / "chat.db"), 13)
    for day in ("2026-09-07", "2026-09-08", "2026-09-09", "2026-01-01"):
        for after in (None, 30):
            kwargs = dict(boundary_message_id=0, after_message_id=after, up_to_message_id=80,
                          exclude_message_id=None, context_id=CTX, context_epoch=EPOCH)
            assert dh._fetch_current_day_history(local_day=day, db_path=db, **kwargs) == \
                reference_fetch(local_day=day, db_path=db, **kwargs)


def test_hot_turn_shape_nothing_after_the_cursor_returns_nothing(tmp_path):
    db = build_db(str(tmp_path / "chat.db"), 14)
    kwargs = dict(boundary_message_id=0, after_message_id=90, up_to_message_id=90,
                  exclude_message_id=90, context_id=CTX, context_epoch=EPOCH)
    assert call(dh._fetch_current_day_history, db, **kwargs) == call(reference_fetch, db, **kwargs) == []


# ---- the SQL that actually runs ------------------------------------------------------

class Recorder:
    """Patches dh._connect so the executed SELECTs can be captured."""

    def __init__(self, monkeypatch):
        self.statements: list[str] = []
        real = dh._connect

        def traced(db_path=None):
            conn = real(db_path)
            conn.set_trace_callback(self.statements.append)
            return conn

        monkeypatch.setattr(dh, "_connect", traced)

    def history_queries(self):
        mapped = [s for s in self.statements if "INNER JOIN daily_message_contexts" in s]
        legacy = [s for s in self.statements
                  if "FROM chat_messages WHERE created_at >=" in s]
        assert len(mapped) == 1 and len(legacy) == 1, self.statements
        return mapped[0], legacy[0]


def plan(db, statement):
    conn = sqlite3.connect(db)
    try:
        return " ; ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + statement))
    finally:
        conn.close()


def fetched_rows(db, statement):
    conn = sqlite3.connect(db)
    try:
        return len(conn.execute(statement).fetchall())
    finally:
        conn.close()


def test_new_queries_are_primary_key_ranges_not_full_reads(tmp_path, monkeypatch):
    db = build_db(str(tmp_path / "chat.db"), 15)
    recorder = Recorder(monkeypatch)
    dh._fetch_current_day_history(
        boundary_message_id=0, local_day=LOCAL_DAY, db_path=db, after_message_id=60,
        exclude_message_id=88, up_to_message_id=88, context_id=CTX, context_epoch=EPOCH)
    mapped, legacy = recorder.history_queries()
    mapped_plan, legacy_plan = plan(db, mapped), plan(db, legacy)
    assert "rowid>?" in mapped_plan and "SCAN" not in mapped_plan, mapped_plan
    assert "SEARCH chat_messages USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)" in legacy_plan, legacy_plan
    assert "id > 60" in mapped and "id > 60" in legacy and "id <= 88" in legacy


def test_without_after_the_mapped_query_keeps_its_original_shape(tmp_path, monkeypatch):
    db = build_db(str(tmp_path / "chat.db"), 16)
    recorder = Recorder(monkeypatch)
    dh._fetch_current_day_history(
        boundary_message_id=5, local_day=LOCAL_DAY, db_path=db, after_message_id=None,
        up_to_message_id=None, context_id=CTX, context_epoch=EPOCH)
    mapped, legacy = recorder.history_queries()
    assert "m.id >" not in mapped and "id <=" not in legacy
    assert "id > 5" in legacy
    assert "SEARCH chat_messages USING INTEGER PRIMARY KEY (rowid>?)" in plan(db, legacy)


def test_legacy_lower_bound_is_the_stricter_of_boundary_and_after(tmp_path, monkeypatch):
    db = build_db(str(tmp_path / "chat.db"), 17)
    for boundary, after, expected in ((10, 50, 50), (70, 20, 70), (30, 30, 30), (0, None, 0), (25, None, 25)):
        recorder = Recorder(monkeypatch)
        dh._fetch_current_day_history(
            boundary_message_id=boundary, local_day=LOCAL_DAY, db_path=db,
            after_message_id=after, context_id=CTX, context_epoch=EPOCH)
        _mapped, legacy = recorder.history_queries()
        assert f"AND id > {expected}" in legacy, (boundary, after, legacy)


def test_hot_turn_queries_read_far_fewer_rows_than_the_original(tmp_path, monkeypatch):
    db = build_db(str(tmp_path / "chat.db"), 18, rows=400)
    kwargs = dict(boundary_message_id=0, local_day=LOCAL_DAY, db_path=db, after_message_id=395,
                  exclude_message_id=400, up_to_message_id=400, context_id=CTX, context_epoch=EPOCH)
    new = Recorder(monkeypatch)
    dh._fetch_current_day_history(**kwargs)
    new_mapped, new_legacy = new.history_queries()
    monkeypatch.undo()

    old = Recorder(monkeypatch)
    reference_fetch(**kwargs)
    old_mapped, old_legacy = old.history_queries()
    assert fetched_rows(db, old_mapped) > 100 and fetched_rows(db, old_legacy) > 100
    assert fetched_rows(db, new_mapped) <= 5 and fetched_rows(db, new_legacy) <= 5
