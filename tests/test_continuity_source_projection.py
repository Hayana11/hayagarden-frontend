"""SQL-side projection of the continuity source scope read.

read_canonical_scope_rows must return rows whose derived facts, payload JSON
and content hashes are byte-identical to reading every column in full.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from chat import daily_continuity_shadow as shadow
from continuity import sources

CONTEXT_ID = 7
CONTEXT_EPOCH = 3
FULL_COLUMNS = (
    'id, author, content, thinking, created_at, tool_calls, branches, '
    'branch_idx, cache_info, source_kind, attachments, image_url, file_url, '
    'file_name'
)
FULL_COLUMN_NAMES = tuple(item.strip() for item in FULL_COLUMNS.split(','))


def make_db(rows, *, wake_run_ids=(), context_ids=None):
    """In-memory source DB. ``rows`` are dicts; all are mapped into the scope
    unless ``context_ids`` lists the ids that are."""
    conn = sqlite3.connect(':memory:')
    conn.execute(
        'CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, '
        'content TEXT, thinking TEXT, created_at TEXT, tool_calls TEXT, '
        'branches TEXT, branch_idx INTEGER, cache_info TEXT, source_kind TEXT, '
        'attachments TEXT, image_url TEXT, file_url TEXT, file_name TEXT)'
    )
    conn.execute(
        'CREATE TABLE daily_message_contexts (message_id INTEGER, '
        'context_id INTEGER, context_epoch INTEGER, resident_generation INTEGER, '
        'role TEXT)'
    )
    conn.execute(
        'CREATE TABLE wake_log (id INTEGER PRIMARY KEY, wake_run_id TEXT, '
        'context_id INTEGER, context_epoch INTEGER, chat_id TEXT)'
    )
    for row in rows:
        full = {
            'thinking': '', 'created_at': '2026-09-08 10:00:00',
            'tool_calls': '[]', 'branches': '[]', 'branch_idx': 0,
            'cache_info': '{}', 'source_kind': 'chat', 'attachments': '[]',
            'image_url': '', 'file_url': '', 'file_name': '', **row,
        }
        conn.execute(
            'INSERT INTO chat_messages (%s) VALUES (%s)' % (
                FULL_COLUMNS, ', '.join('?' * len(FULL_COLUMN_NAMES)),
            ),
            [full[name] for name in FULL_COLUMN_NAMES],
        )
    mapped = (
        [r['id'] for r in rows if r.get('source_kind', 'chat') != 'wake']
        if context_ids is None else list(context_ids)
    )
    for message_id in mapped:
        conn.execute(
            'INSERT INTO daily_message_contexts VALUES (?, ?, ?, 1, ?)',
            (message_id, CONTEXT_ID, CONTEXT_EPOCH, 'user'),
        )
    for run_id in wake_run_ids:
        conn.execute(
            'INSERT INTO wake_log (wake_run_id, context_id, context_epoch, '
            'chat_id) VALUES (?, ?, ?, ?)',
            (run_id, CONTEXT_ID, CONTEXT_EPOCH, 'default'),
        )
    return conn


def sample_rows():
    return [
        {'id': 1, 'author': 'hayana', 'content': 'question',
         'thinking': 'T' * 5000},
        {'id': 2, 'author': 'assistant', 'content': 'answer',
         'thinking': 'long hidden reasoning ' * 400,
         'cache_info': json.dumps({'usage': {'in': 10}})},
    ]


# ---- A1: thinking is not read ---------------------------------------------

def test_source_columns_do_not_select_thinking():
    names = {item.strip() for item in shadow._SOURCE_COLUMNS.split(',')}
    assert 'thinking' not in names
    assert names == set(FULL_COLUMN_NAMES) - {'thinking'}


def test_scope_rows_do_not_carry_thinking_and_derive_the_same_facts():
    conn = make_db(sample_rows())
    rows = shadow.read_canonical_scope_rows(
        conn, context_id=CONTEXT_ID, context_epoch=CONTEXT_EPOCH,
    )
    assert [row['id'] for row in rows] == [1, 2]
    assert all('thinking' not in row for row in rows)

    full = [
        dict(zip(FULL_COLUMN_NAMES, record)) for record in conn.execute(
            'SELECT %s FROM chat_messages ORDER BY id' % FULL_COLUMNS
        )
    ]
    for got, want in zip(rows, full):
        assert sources.row_content_hash(got) == sources.row_content_hash(want)
        assert sources._row_payload_json(got) == sources._row_payload_json(want)


def test_current_request_row_does_not_carry_thinking():
    conn = make_db(sample_rows() + [
        {'id': 3, 'author': 'hayana', 'content': 'now', 'thinking': 'x'},
    ])
    _history, current = shadow._source_rows(conn, 3)
    assert 'thinking' not in current and current['content'] == 'now'


def test_schema_contract_still_requires_thinking():
    conn = make_db(sample_rows())
    shadow._check_source_schema(conn)  # complete schema passes

    broken = sqlite3.connect(':memory:')
    broken.execute(
        'CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, '
        'content TEXT, created_at TEXT, tool_calls TEXT, branches TEXT, '
        'branch_idx INTEGER, cache_info TEXT, source_kind TEXT, '
        'attachments TEXT, image_url TEXT, file_url TEXT, file_name TEXT)'
    )
    with pytest.raises(sqlite3.OperationalError, match='thinking'):
        shadow._check_source_schema(broken)
