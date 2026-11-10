"""Exact raw-column lookup key for the source-row derivation cache.

The cache key is (message id, every raw column the derivation reads) compared
exactly, so a lookup does no JSON parsing, canonical serialization or hashing.
These tests pin that every column participates, that a hit does no derivation
work, that derive_completed_turns / derive_autonomous_events can share one
derivation, and that results are identical to the pre-cache algorithm.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import sqlite3

import pytest

from continuity import sources
from continuity.contracts import AutonomousEvent, CanonicalTurn

WAKE_CACHE_INFO = json.dumps({
    'wake_mode': 'normal', 'canonical_chat_history': True,
    'unified_chat_resident': True, 'b3_authority': True,
    'source': 'wake', 'provider': 'claude_code',
})


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.delenv('CONTINUITY_STRICT_REVALIDATE', raising=False)
    sources.clear_source_derivation_cache()
    yield
    sources.clear_source_derivation_cache()


def _row(message_id, author, content, **overrides):
    row = {
        'id': message_id, 'author': author, 'content': content,
        'thinking': '', 'source_kind': 'chat',
        'created_at': '2026-09-08 10:%02d:00' % message_id,
        'tool_calls': '[]', 'branches': '[]', 'branch_idx': 0,
        'cache_info': '{}', 'attachments': '[]', 'image_url': '',
        'file_url': '', 'file_name': '',
    }
    row.update(overrides)
    return row


def _transcript():
    """Rows that exercise every branch of the derivation."""
    return [
        _row(1, 'hayana', 'first question'),
        _row(2, 'assistant', 'first answer', tool_calls=json.dumps(
            [{'name': 'lookup', 'args': {'q': 'a'}, 'result': 'ok',
              'success': True}])),
        _row(3, 'assistant', 'wake note', source_kind='wake',
             cache_info=WAKE_CACHE_INFO),
        _row(4, 'hayana', 'second question',
             attachments=json.dumps([
                 {'type': 'image', 'url': '/static/uploads/a.png', 'name': 'a'},
             ])),
        _row(5, 'assistant', 'second answer', branches=json.dumps([
            {'content': 'alt', 'tool_calls': '[]', 'thinking': 'hidden'},
            {'content': 'alt two', 'tool_calls': [{'name': 'x', 'args': {}}]},
        ]), branch_idx=1),
        _row(6, 'hayana', 'third question'),
        _row(7, 'assistant', 'partial', cache_info=json.dumps(
            {'partial_rescue': True})),
        _row(8, 'hayana', 'fourth question'),
        _row(9, 'assistant', 'one'),
        _row(10, 'assistant', 'two answers in a row fail closed'),
        _row(11, 'hayana', 'legacy workspace row follows'),
        _row(12, 'assistant', 'legacy job', tool_calls=json.dumps(
            [{'name': 'ws_job', 'args': {'action': 'status'},
              'job': {'id': 1}}])),
        _row(13, 'hayana', 'file question', file_url='/chat-files/x.txt',
             file_name='x.txt'),
        _row(14, 'assistant', 'file answer', image_url='/static/uploads/b.png'),
        _row(15, 'system', 'excluded author'),
        _row(16, 'hayana', 'save marker [[SAVE:note]]'),
        _row(17, 'assistant', 'unicode 答复 ✓'),
    ]


def _sqlite_rows(rows):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    columns = list(rows[0])
    conn.execute('CREATE TABLE m (%s)' % ', '.join(columns))
    for row in rows:
        conn.execute(
            'INSERT INTO m VALUES (%s)' % ', '.join('?' * len(columns)),
            [row[c] for c in columns],
        )
    return conn.execute('SELECT * FROM m ORDER BY id').fetchall()


# --------------------------------------------------------------------------
# Pre-cache reference: the derivation exactly as it was before any caching
# (e1225bd), built only from the row-level primitives, no cache involved.
# --------------------------------------------------------------------------

def _legacy_evidence_ref(row):
    mid = int(sources._value(row, 'id', 0) or 0)
    payload = sources._row_payload_json(row)
    digest = sources._sha256_text(payload)
    return sources.EvidenceRef(
        source_ref=f'message:{mid}',
        source_revision=digest,
        content_hash=digest,
        logical_size=int(
            sources.estimate_tokens_heuristic_cjk1_ascii4_v1(payload)
        ),
    )


def _legacy_turns(rows):
    ordered = sorted(rows, key=lambda r: int(sources._value(r, 'id', 0) or 0))
    completed = []
    state = {'user': None, 'assistants': []}

    def flush():
        user, assistants = state['user'], state['assistants']
        if (
            user is not None and len(assistants) == 1
            and not sources._explicitly_incomplete(assistants[0])
        ):
            assistant = assistants[0]
            uid = int(sources._value(user, 'id', 0) or 0)
            aid = int(sources._value(assistant, 'id', 0) or 0)
            branch_id = sources._active_branch_identity(assistant)
            user_ref = _legacy_evidence_ref(user)
            assistant_ref = _legacy_evidence_ref(assistant)
            tool_refs = sources._tool_outcome_refs(assistant)
            completed.append(CanonicalTurn(
                turn_id=f'turn:{uid}:{aid}',
                identity_id=sources.IDENTITY_ID,
                chat_id=sources.CHAT_ID,
                branch_id=branch_id,
                user_input_ref=user_ref,
                assistant_committed_output_refs=(assistant_ref,),
                tool_outcome_refs=tool_refs,
                started_at=str(sources._value(user, 'created_at', '') or ''),
                committed_at=str(
                    sources._value(assistant, 'created_at', '') or ''
                ),
                finality_status='completed',
                source_revision=sources._turn_revision(
                    user, assistant, user_ref=user_ref,
                    assistant_ref=assistant_ref, branch_id=branch_id,
                    tool_refs=tool_refs,
                ),
            ))
        state['user'], state['assistants'] = None, []

    for row in ordered:
        if sources.is_formal_user_source_row(row):
            flush()
            state['user'] = row
        elif sources._is_formal_assistant(row) and state['user'] is not None:
            state['assistants'].append(row)
    flush()
    return tuple(completed)


def _legacy_events(rows):
    events = []
    for row in sorted(rows, key=lambda r: int(sources._value(r, 'id', 0) or 0)):
        if not sources._canonical_normal_wake(row):
            continue
        mid = int(sources._value(row, 'id', 0) or 0)
        events.append(AutonomousEvent(
            event_id=f'wake:{mid}',
            identity_id=sources.IDENTITY_ID,
            chat_id=sources.CHAT_ID,
            branch_id='active-transcript',
            event_kind='wake',
            committed_content_ref=_legacy_evidence_ref(row),
            created_at=str(sources._value(row, 'created_at', '') or ''),
            finality_status='completed',
            source_revision=sources.row_revision(row),
        ))
    return tuple(events)


def _fields(items):
    """Field-by-field view (recursing into EvidenceRef) of derived objects."""
    return [dataclasses.asdict(item) for item in items]


def _assert_identical(actual_turns, actual_events, rows):
    expected_turns, expected_events = _legacy_turns(rows), _legacy_events(rows)
    assert len(actual_turns) == len(expected_turns)
    assert len(actual_events) == len(expected_events)
    for got, want in zip(_fields(actual_turns), _fields(expected_turns)):
        assert got == want
    for got, want in zip(_fields(actual_events), _fields(expected_events)):
        assert got == want
    assert (
        sources.build_source_members(actual_turns, actual_events)
        == sources.build_source_members(expected_turns, expected_events)
    )
    # Byte-level check on a canonical, object-identity-independent encoding
    # (pickle would differ whenever the cache shares one string object).
    def canonical(turns, events):
        return json.dumps(
            [_fields(turns), _fields(events)],
            ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        ).encode('utf-8')

    assert canonical(actual_turns, actual_events) == canonical(
        expected_turns, expected_events,
    )


@pytest.mark.parametrize('as_sqlite_row', [False, True])
def test_new_path_matches_pre_cache_algorithm_field_by_field(
    monkeypatch, as_sqlite_row,
):
    rows = _transcript()
    if as_sqlite_row:
        rows = _sqlite_rows(rows)
    expected_turn_ids = [t.turn_id for t in _legacy_turns(rows)]
    # The fixture must really exercise turns, wakes and the fail-closed cases.
    assert 'turn:1:2' in expected_turn_ids and 'turn:4:5' in expected_turn_ids
    assert len(_legacy_events(rows)) == 1
    assert not any(t in expected_turn_ids for t in ('turn:8:9', 'turn:8:10'))

    cold = (
        sources.derive_completed_turns(rows),
        sources.derive_autonomous_events(rows),
    )
    _assert_identical(*cold, rows)
    warm = (
        sources.derive_completed_turns(rows),
        sources.derive_autonomous_events(rows),
    )
    _assert_identical(*warm, rows)
    _assert_identical(*sources.derive_turns_and_events(rows), rows)

    sources.clear_source_derivation_cache()
    _assert_identical(*sources.derive_turns_and_events(rows), rows)

    monkeypatch.setenv('CONTINUITY_STRICT_REVALIDATE', '1')
    _assert_identical(*sources.derive_turns_and_events(rows), rows)


def test_new_path_matches_pre_cache_algorithm_after_row_edits():
    rows = _transcript()
    sources.derive_turns_and_events(rows)  # populate the cache
    edited = copy.deepcopy(rows)
    edited[1]['content'] = 'edited answer'
    edited[4]['branch_idx'] = 0
    edited[12]['file_url'] = ''
    edited.append(_row(18, 'hayana', 'late question'))
    edited.append(_row(19, 'assistant', 'late answer'))
    _assert_identical(*sources.derive_turns_and_events(edited), edited)


# --------------------------------------------------------------------------
# Lookup key: every column read by the derivation must produce a miss.
# --------------------------------------------------------------------------

_COLUMN_MUTATIONS = {
    'id': 101,
    'author': 'claude',
    'content': 'changed content',
    'source_kind': 'wake',
    'tool_calls': '[{"name":"other","args":{},"result":"no"}]',
    'branches': '[{"content":"b","tool_calls":"[]"}]',
    'branch_idx': 3,
    'cache_info': '{"turn_incomplete": true}',
    'attachments': '[{"type":"image","url":"/static/uploads/z.png"}]',
    'file_url': '/chat-files/changed.txt',
    'file_name': 'changed.txt',
    'image_url': '/static/uploads/changed.png',
    'created_at': '2030-01-01 00:00:00',
}


def test_mutation_table_covers_every_key_column():
    assert set(_COLUMN_MUTATIONS) == set(sources._SOURCE_ROW_KEY_COLUMNS)
    assert len(sources._SOURCE_ROW_KEY_COLUMNS) == len(
        set(sources._SOURCE_ROW_KEY_COLUMNS)
    )


@pytest.mark.parametrize('column', sources._SOURCE_ROW_KEY_COLUMNS)
def test_any_key_column_change_misses(monkeypatch, column):
    base = _row(2, 'assistant', 'answer', tool_calls='[]')
    sources._source_row_derivation(base)
    computed = []
    original = sources._compute_source_row_facts

    def counted(row, **kwargs):
        computed.append(sources._value(row, 'id'))
        return original(row, **kwargs)

    monkeypatch.setattr(sources, '_compute_source_row_facts', counted)

    sources._source_row_derivation(dict(base))
    assert computed == [], 'identical values must hit'

    changed = dict(base)
    changed[column] = _COLUMN_MUTATIONS[column]
    assert changed[column] != base[column]
    derived = sources._source_row_derivation(changed)
    assert computed == [changed['id']], f'changing {column} must miss'
    assert derived.cache_key != sources._source_row_derivation(base).cache_key

    # The miss result is the true derivation of the changed row.
    expected_hash = sources._sha256_text(sources._row_payload_json(changed))
    assert derived.content_hash == expected_hash


def test_column_absent_is_distinct_from_empty_value():
    full = _row(2, 'assistant', 'answer')
    missing = {k: v for k, v in full.items() if k != 'image_url'}
    assert (
        sources._source_row_cache_key(full)
        != sources._source_row_cache_key(missing)
    )


@pytest.mark.parametrize('first,second', [(1, True), (0, False), (1, 1.0)])
def test_values_equal_in_python_but_different_types_do_not_share_an_entry(
    first, second,
):
    one = _row(2, 'assistant', 'answer', branch_idx=first)
    two = _row(2, 'assistant', 'answer', branch_idx=second)
    assert one['branch_idx'] == two['branch_idx']
    assert (
        sources._source_row_cache_key(one) != sources._source_row_cache_key(two)
    )


def test_unhashable_column_value_is_derived_uncached_not_rejected():
    row = _row(2, 'assistant', 'answer', tool_calls=[
        {'name': 'lookup', 'args': {}, 'result': 'ok'},
    ])
    assert sources._source_row_cache_key(row) is None
    derived = sources._source_row_derivation(row)
    assert derived.content_hash == sources._sha256_text(
        sources._row_payload_json(row)
    )
    assert len(sources._SOURCE_DERIVATION_CACHE) == 0


def test_key_covers_every_column_the_derivation_reads(monkeypatch):
    """Guard: no reader may use a column that is missing from the key."""
    read = set()

    class Recording(dict):
        def __getitem__(self, key):
            read.add(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            read.add(key)
            return super().get(key, default)

    # Force the uncached path so only the readers (not the key function)
    # touch the row.
    monkeypatch.setattr(sources, '_source_row_cache_key', lambda row: None)
    rows = [Recording(row) for row in _transcript()]
    sources.derive_turns_and_events(rows)
    for row in rows:
        sources.row_content_hash(row)
        sources.row_logical_size(row)
        sources._tool_outcome_refs(row)
        sources._active_branch_identity(row)

    assert read, 'recording row was never read'
    assert read <= set(sources._SOURCE_ROW_KEY_COLUMNS), (
        'derivation reads columns missing from _SOURCE_ROW_KEY_COLUMNS: %s'
        % sorted(read - set(sources._SOURCE_ROW_KEY_COLUMNS))
    )
    # thinking is deliberately not provider-visible evidence.
    assert 'thinking' not in read


def test_thinking_is_not_part_of_the_key():
    row = _row(2, 'assistant', 'answer')
    other = dict(row, thinking='different hidden reasoning')
    assert sources._source_row_cache_key(row) == sources._source_row_cache_key(other)


# --------------------------------------------------------------------------
# A hit does no JSON / serialization / hashing work; strict bypasses.
# --------------------------------------------------------------------------

def test_hit_path_skips_payload_json_hash_and_facts(monkeypatch):
    rows = _transcript()
    sources.derive_turns_and_events(rows)
    fresh = copy.deepcopy(rows)  # equal values, different objects
    calls = {'payload': 0, 'facts': 0, 'json': 0}
    originals = (
        sources._row_payload_json, sources._compute_source_row_facts,
        sources._json_value,
    )

    def spy(index, name):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return originals[index](*args, **kwargs)
        return wrapper

    monkeypatch.setattr(sources, '_row_payload_json', spy(0, 'payload'))
    monkeypatch.setattr(sources, '_compute_source_row_facts', spy(1, 'facts'))
    monkeypatch.setattr(sources, '_json_value', spy(2, 'json'))
    for row in fresh:
        sources._source_row_derivation(row)
    assert calls == {'payload': 0, 'facts': 0, 'json': 0}


def test_strict_revalidation_bypasses_and_never_populates_the_cache(monkeypatch):
    monkeypatch.setenv('CONTINUITY_STRICT_REVALIDATE', '1')
    rows = _transcript()
    computed = []
    original = sources._compute_source_row_facts
    monkeypatch.setattr(
        sources, '_compute_source_row_facts',
        lambda row, **kw: computed.append(row['id']) or original(row, **kw),
    )
    sources.derive_turns_and_events(rows)
    sources.derive_turns_and_events(rows)
    assert len(computed) == 2 * len(rows)
    assert len(sources._SOURCE_DERIVATION_CACHE) == 0


# --------------------------------------------------------------------------
# One derivation per row shared by turns and events.
# --------------------------------------------------------------------------

def test_turns_and_events_share_one_derivation_pass(monkeypatch):
    rows = _transcript()
    calls = []
    original = sources._source_row_derivation
    monkeypatch.setattr(
        sources, '_source_row_derivation',
        lambda row: calls.append(row['id']) or original(row),
    )
    turns, events = sources.derive_turns_and_events(rows)
    assert sorted(calls) == sorted(row['id'] for row in rows)

    calls.clear()
    separate = (
        sources.derive_completed_turns(rows),
        sources.derive_autonomous_events(rows),
    )
    assert len(calls) == 2 * len(rows)  # standalone callers still work
    assert (turns, events) == separate


def test_shared_derivation_in_strict_mode_computes_each_row_once(monkeypatch):
    monkeypatch.setenv('CONTINUITY_STRICT_REVALIDATE', '1')
    rows = _transcript()
    computed = []
    original = sources._compute_source_row_facts
    monkeypatch.setattr(
        sources, '_compute_source_row_facts',
        lambda row, **kw: computed.append(row['id']) or original(row, **kw),
    )
    sources.derive_turns_and_events(rows)
    assert sorted(computed) == sorted(row['id'] for row in rows)
