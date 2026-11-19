"""Capability surface fingerprint: single state read + token-keyed catalog cache."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from cryptography.fernet import Fernet

import tools.capability_state as capability_state
import tools.cc_capability_adapter as adapter
import tools.external_mcp_production as production
from tools.capability_state import (
    CAPABILITY_STATE_KEY,
    CapabilityStateError,
    CapabilityStateReader,
    RUNTIME_STATE_DENY,
    RUNTIME_STATE_INHERIT,
    read_capability_state,
)

ALL_IDS = (
    adapter.HOME_MCP_CAPABILITY_IDS
    + adapter.INTERNAL_MCP_CAPABILITY_IDS
    + adapter.CAPABILITY_PROXY_CAPABILITY_IDS
    + adapter.NATIVE_FILE_CAPABILITY_IDS
    + adapter.EXTERNAL_READ_CAPABILITY_IDS
)


def _tool(name):
    return {
        'name': name, 'description': f'Tool {name}',
        'inputSchema': {'type': 'object', 'properties': {}},
    }


class World:
    """Config DB + external MCP DB/key under tmp_path, wired like production."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.config_db = str(tmp_path / 'config.db')
        conn = sqlite3.connect(self.config_db)
        conn.execute(
            'CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT, '
            'updated_at TEXT)'
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(capability_state, 'DB_PATH', self.config_db)

        self.key = tmp_path / 'external.key'
        self.key.write_bytes(Fernet.generate_key())
        os.chmod(self.key, 0o600)
        self.db = str(tmp_path / 'external-mcp.db')
        real_lstat = os.lstat

        def root_owned_lstat(path, *, code):
            try:
                metadata = real_lstat(path)
            except OSError as exc:
                raise production.ExternalMcpProductionInitializationError(
                    code
                ) from exc
            if path in {str(self.key), self.db}:
                return SimpleNamespace(
                    st_mode=metadata.st_mode, st_uid=0, st_gid=0,
                    st_nlink=metadata.st_nlink,
                )
            return metadata

        monkeypatch.setattr(production, 'EXTERNAL_MCP_DB_PATH', self.db)
        monkeypatch.setattr(production, 'EXTERNAL_MCP_KEY_FILE', str(self.key))
        monkeypatch.setattr(production, '_validate_db_parent', lambda: None)
        monkeypatch.setattr(production, '_lstat', root_owned_lstat)
        # Make timestamps trustworthy immediately; the racy guard has its own test.
        monkeypatch.setattr(adapter, '_EXTERNAL_CATALOG_RACY_NS', 0)
        adapter.clear_external_catalog_cache()
        self.monkeypatch = monkeypatch
        self.server_id = None

    def add_tools(self, *names):
        with production.open_external_mcp_production() as graph:
            if self.server_id is None:
                server = graph.server_registry.register(
                    display_name='Monopoly',
                    endpoint='https://monopoly.example/mcp',
                    provenance='test',
                )
                self.server_id = server.server_id
            current = graph.server_registry.get(self.server_id)
            connected = graph.server_registry.mark_connected(
                self.server_id, current.revision,
            )
            graph.candidate_registry.ingest({
                'status': 'SUCCESS', 'server_id': connected.server_id,
                'registry_revision': connected.revision,
                'catalog_complete': True,
                'tool_record_boundary': 'SDK_VISIBLE_RAW',
                'tools': [_tool(name) for name in names],
                'diagnostics': {'registry_changed_during_attempt': False},
            })
        time.sleep(0.02)  # distinct timestamps even on coarse clocks

    def catalog_names(self):
        catalog, visible = adapter._external_surface_catalog()
        return sorted(item['remote_tool_name'] for item in visible)

    def builds(self):
        """Count real catalog builds through the production listing function."""
        import tools.external_mcp_surface as surface
        counter = {'n': 0}
        original = surface.list_external_surface

        def counted(*args, **kwargs):
            counter['n'] += 1
            return original(*args, **kwargs)

        self.monkeypatch.setattr(surface, 'list_external_surface', counted)
        return counter


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


# ---- CapabilityStateReader: one read, same per-capability semantics ----------

@pytest.fixture
def config_db(tmp_path, monkeypatch):
    path = str(tmp_path / 'config.db')
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT, '
        'updated_at TEXT)'
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(capability_state, 'DB_PATH', path)
    return path


def _write_document(path, raw):
    conn = sqlite3.connect(path)
    conn.execute('DELETE FROM runtime_config')
    if raw is not None:
        conn.execute(
            'INSERT INTO runtime_config (key, value) VALUES (?, ?)',
            (CAPABILITY_STATE_KEY, raw),
        )
    conn.commit()
    conn.close()


def _count_connects(monkeypatch):
    counter = {'n': 0}
    original = capability_state._connect

    def counted():
        counter['n'] += 1
        return original()

    monkeypatch.setattr(capability_state, '_connect', counted)
    return counter


def _outcome(call):
    try:
        return ('ok', call())
    except Exception as exc:  # noqa: BLE001
        return ('error', type(exc).__name__, str(exc))


@pytest.mark.parametrize('raw', [
    None,
    json.dumps({'schema_version': 1, 'states': {}}),
    json.dumps({'schema_version': 1, 'states': {ALL_IDS[0]: 'OFF', ALL_IDS[3]: 'ON'}}),
    'not json',
    json.dumps({'schema_version': 2, 'states': {}}),
    json.dumps({'schema_version': 1, 'states': {'unknown.cap': 'ON'}}),
    json.dumps({'schema_version': 1, 'states': {ALL_IDS[0]: 'MAYBE'}}),
    json.dumps({'schema_version': 1}),
    json.dumps([]),
])
def test_reader_gives_the_same_states_or_errors_as_per_capability_reads(
    config_db, raw,
):
    _write_document(config_db, raw)
    old = _outcome(lambda: {cid: read_capability_state(cid) for cid in ALL_IDS})
    reader = CapabilityStateReader()
    new = _outcome(lambda: {
        cid: read_capability_state(cid, reader=reader) for cid in ALL_IDS
    })
    assert new == old


def test_reader_opens_one_connection_where_per_capability_reads_open_many(
    config_db, monkeypatch,
):
    _write_document(config_db, json.dumps({'schema_version': 1, 'states': {}}))
    counter = _count_connects(monkeypatch)
    {cid: read_capability_state(cid) for cid in ALL_IDS}
    per_capability = counter['n']
    counter['n'] = 0
    reader = CapabilityStateReader()
    {cid: read_capability_state(cid, reader=reader) for cid in ALL_IDS}
    assert per_capability == len(ALL_IDS)
    assert counter['n'] == 1


def test_statically_ineligible_capability_is_denied_without_touching_storage(
    monkeypatch,
):
    def boom():
        raise AssertionError('storage must not be read')

    monkeypatch.setattr(capability_state, '_connect', boom)
    reader = CapabilityStateReader()
    assert read_capability_state('no.such.capability', reader=reader) == RUNTIME_STATE_DENY
    assert read_capability_state('', reader=reader) == RUNTIME_STATE_DENY


def test_reader_never_serves_a_stale_document_to_a_new_reader(config_db):
    _write_document(config_db, json.dumps({'schema_version': 1, 'states': {}}))
    first = CapabilityStateReader()
    assert read_capability_state(ALL_IDS[0], reader=first) == RUNTIME_STATE_INHERIT
    _write_document(config_db, json.dumps(
        {'schema_version': 1, 'states': {ALL_IDS[0]: 'OFF'}}
    ))
    assert read_capability_state(ALL_IDS[0], reader=CapabilityStateReader()) == 'OFF'


def test_surface_snapshot_reads_capability_state_over_one_connection(
    world, monkeypatch,
):
    counter = _count_connects(monkeypatch)
    snapshot = adapter._surface_snapshot()
    assert snapshot['runtime_state_status'] == 'OK'
    assert counter['n'] == 1


def test_snapshot_still_honors_a_patched_per_capability_reader(monkeypatch):
    # Existing cutover tests replace read_capability_state wholesale.
    monkeypatch.setattr(
        adapter, 'read_capability_state', mock.Mock(return_value=RUNTIME_STATE_INHERIT),
    )
    monkeypatch.setattr(adapter, '_external_surface_catalog', lambda: ([], ()))
    assert adapter._surface_snapshot()['runtime_state_status'] == 'OK'


def test_capability_state_change_changes_the_fingerprint_immediately(world):
    before = adapter.physical_surface_fingerprint()
    assert adapter.physical_surface_fingerprint() == before
    capability_state.set_capability_state(adapter.HOME_MCP_CAPABILITY_IDS[0], enabled=False)
    changed = adapter.physical_surface_fingerprint()
    assert changed != before
    capability_state.set_capability_state(adapter.HOME_MCP_CAPABILITY_IDS[0], enabled=True)
    assert adapter.physical_surface_fingerprint() != changed


def test_unreadable_capability_state_still_fails_closed(world):
    _write_document(world.config_db, 'not json')
    snapshot = adapter._surface_snapshot()
    assert snapshot['runtime_state_status'] == 'FAIL_CLOSED'
    assert snapshot['home_mcp_tools'] == ()


# ---- External catalog cache ---------------------------------------------------

def test_catalog_is_built_once_while_every_input_is_unchanged(world):
    world.add_tools('roll')
    counter = world.builds()
    first = adapter._external_surface_catalog()
    second = adapter._external_surface_catalog()
    third = adapter._external_surface_catalog()
    assert counter['n'] == 1
    assert first == second == third
    assert world.catalog_names() == ['roll']


def test_cached_catalog_is_returned_as_an_independent_copy(world):
    world.add_tools('roll')
    catalog, visible = adapter._external_surface_catalog()
    catalog[0]['tools'].clear()
    catalog.append({'poison': True})
    again_catalog, again_visible = adapter._external_surface_catalog()
    assert [t['remote_tool_name'] for t in again_catalog[0]['tools']] == ['roll']
    assert {'poison': True} not in again_catalog
    assert len(again_visible) == 1


def test_a_database_write_invalidates_immediately(world):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    fingerprint = adapter.physical_surface_fingerprint()
    world.add_tools('roll', 'buy')
    assert world.catalog_names() == ['buy', 'roll']
    assert adapter.physical_surface_fingerprint() != fingerprint


def test_a_wal_mode_write_invalidates_immediately(world):
    world.add_tools('roll')
    conn = sqlite3.connect(world.db)
    assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    conn.close()
    assert world.catalog_names() == ['roll']
    fingerprint = adapter.physical_surface_fingerprint()
    world.add_tools('roll', 'buy')
    assert world.catalog_names() == ['buy', 'roll']
    assert adapter.physical_surface_fingerprint() != fingerprint


def test_replacing_the_database_file_invalidates(world, tmp_path):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    other = str(tmp_path / 'other.db')
    sqlite3.connect(world.db).backup(sqlite3.connect(other))
    # Same bytes, different file: the token must still change.
    time.sleep(0.02)
    os.replace(other, world.db)
    counter = world.builds()
    world.catalog_names()
    assert counter['n'] == 1


def test_key_file_content_change_invalidates(world):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    counter = world.builds()
    time.sleep(0.02)
    world.key.write_bytes(Fernet.generate_key())  # same size, new content
    os.chmod(world.key, 0o600)
    world.catalog_names()
    assert counter['n'] == 1


def test_key_file_permission_loosening_fails_closed_not_stale(world):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    os.chmod(world.key, 0o644)  # production rejects anything but 0600
    assert adapter._external_surface_catalog() == ([], ())
    os.chmod(world.key, 0o600)
    assert world.catalog_names() == ['roll']


def test_missing_database_or_key_fails_closed_and_recovers(world):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    saved = world.key.read_bytes()
    world.key.unlink()
    assert adapter._external_surface_catalog() == ([], ())
    time.sleep(0.02)
    world.key.write_bytes(saved)
    os.chmod(world.key, 0o600)
    assert world.catalog_names() == ['roll']


def test_pointing_at_a_different_database_invalidates(world, tmp_path, monkeypatch):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    second_dir = tmp_path / 'second'
    second_dir.mkdir()
    second_db = str(second_dir / 'external-mcp.db')
    monkeypatch.setattr(production, 'EXTERNAL_MCP_DB_PATH', second_db)
    real = production._lstat

    def lstat(path, *, code):
        result = real(path, code=code)
        if path == second_db:
            return SimpleNamespace(
                st_mode=result.st_mode, st_uid=0, st_gid=0,
                st_nlink=result.st_nlink,
            )
        return result

    monkeypatch.setattr(production, '_lstat', lstat)
    assert world.catalog_names() == []  # fresh empty DB, not the cached 'roll'


def test_empty_catalog_is_never_pinned(world):
    counter = world.builds()
    assert world.catalog_names() == []
    assert world.catalog_names() == []
    assert counter['n'] == 2


def test_a_failed_build_is_not_pinned_when_inputs_are_unchanged(world, monkeypatch):
    world.add_tools('roll')
    import tools.external_mcp_surface as surface
    original = surface.list_external_surface
    calls = {'n': 0}

    def flaky(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            return []  # list_external_surface fails closed to [] on errors
        return original(*args, **kwargs)

    monkeypatch.setattr(surface, 'list_external_surface', flaky)
    assert adapter._external_surface_catalog() == ([], ())
    assert world.catalog_names() == ['roll']  # same files, recovered at once
    assert world.catalog_names() == ['roll']
    assert calls['n'] == 2


def test_recently_modified_inputs_are_not_trusted_for_caching(world, monkeypatch):
    world.add_tools('roll')
    monkeypatch.setattr(adapter, '_EXTERNAL_CATALOG_RACY_NS', 3_600 * 10**9)
    adapter.clear_external_catalog_cache()
    counter = world.builds()
    assert world.catalog_names() == ['roll']
    assert world.catalog_names() == ['roll']
    assert counter['n'] == 2  # files were just written: rebuilt every time


def test_a_write_during_the_build_cannot_be_served_stale(world, monkeypatch):
    world.add_tools('roll')
    import tools.external_mcp_surface as surface
    original = surface.list_external_surface
    state = {'done': False}

    def write_midway(*args, **kwargs):
        result = original(*args, **kwargs)
        if not state['done']:
            state['done'] = True
            world.add_tools('roll', 'buy')  # lands after the token was taken
        return result

    monkeypatch.setattr(surface, 'list_external_surface', write_midway)
    assert world.catalog_names() == ['roll']  # result predates the write
    # ...but it was stored under the old token, so the next lookup must miss.
    assert world.catalog_names() == ['buy', 'roll']


def test_patched_catalog_seam_used_by_existing_tests_still_works(monkeypatch):
    monkeypatch.setattr(adapter, '_external_surface_catalog', lambda: ([], ()))
    assert adapter._surface_snapshot()['external_mcp_tools'] == ()


# ---- Each part of the token must be able to invalidate on its own -----------

def test_mutating_a_cache_hit_result_cannot_corrupt_the_cache(world):
    world.add_tools('roll')
    adapter._external_surface_catalog()  # miss, stores
    hit_catalog, hit_visible = adapter._external_surface_catalog()  # hit
    hit_catalog[0]['tools'].clear()
    hit_catalog.append({'poison': True})
    again_catalog, _ = adapter._external_surface_catalog()
    assert [t['remote_tool_name'] for t in again_catalog[0]['tools']] == ['roll']
    assert {'poison': True} not in again_catalog


def test_sqlite_header_change_counter_alone_invalidates(world, monkeypatch):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    # Blind the stat part of the token: only the file header can notice.
    monkeypatch.setattr(adapter, '_stat_signature', lambda path: ('blind',))
    adapter.clear_external_catalog_cache()
    assert world.catalog_names() == ['roll']  # cached under the blind token
    world.add_tools('roll', 'buy')
    assert world.catalog_names() == ['buy', 'roll']


def test_wal_file_growth_alone_invalidates_when_the_main_file_is_untouched(
    world, monkeypatch,
):
    world.add_tools('roll')
    keeper = sqlite3.connect(world.db)  # keeps the WAL from being checkpointed
    try:
        assert keeper.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute('SELECT count(*) FROM sqlite_master').fetchall()
        assert world.catalog_names() == ['roll']
        main_before = os.stat(world.db)
        header_before = open(world.db, 'rb').read(100)
        world.add_tools('roll', 'buy')
        main_after = os.stat(world.db)
        # The write sits in the -wal: the main file and its header did not move.
        assert (main_before.st_size, main_before.st_mtime_ns) == (
            main_after.st_size, main_after.st_mtime_ns,
        )
        assert open(world.db, 'rb').read(100) == header_before
        assert os.path.getsize(world.db + '-wal') > 0
        assert world.catalog_names() == ['buy', 'roll']
    finally:
        keeper.close()


class _FakeStat:
    def __init__(self, **overrides):
        fields = dict(
            st_dev=1, st_ino=2, st_mode=0o100600, st_uid=0, st_gid=0,
            st_nlink=1, st_size=10, st_mtime_ns=10**18, st_ctime_ns=10**18,
        )
        fields.update(overrides)
        self.__dict__.update(fields)


@pytest.mark.parametrize('field,value', [
    ('st_dev', 9), ('st_ino', 9), ('st_mode', 0o100644), ('st_uid', 1000),
    ('st_gid', 1000), ('st_nlink', 2), ('st_size', 11),
    ('st_mtime_ns', 10**18 + 1), ('st_ctime_ns', 10**18 + 1),
])
def test_every_stat_field_changes_the_signature(monkeypatch, field, value):
    base = _FakeStat()
    monkeypatch.setattr(os, 'lstat', lambda path: base)
    before = adapter._stat_signature('/x')
    monkeypatch.setattr(os, 'lstat', lambda path: _FakeStat(**{field: value}))
    assert adapter._stat_signature('/x') != before


def test_ctime_catches_a_rewrite_that_restores_size_and_mtime(world):
    world.add_tools('roll')
    assert world.catalog_names() == ['roll']
    stat = os.stat(world.key)
    counter = world.builds()
    time.sleep(0.02)
    world.key.write_bytes(Fernet.generate_key())  # same size
    os.chmod(world.key, 0o600)
    os.utime(world.key, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # forge mtime
    assert os.stat(world.key).st_mtime_ns == stat.st_mtime_ns
    assert world.catalog_names() == ['roll']
    assert counter['n'] == 1  # rebuilt: ctime cannot be forged


def test_unknowable_path_state_disables_caching(world, monkeypatch):
    world.add_tools('roll')
    real = os.lstat

    def denied(path):
        if str(path) == world.db:
            raise PermissionError('denied')
        return real(path)

    counter = world.builds()
    monkeypatch.setattr(os, 'lstat', denied)
    adapter._external_surface_catalog()
    adapter._external_surface_catalog()
    assert counter['n'] == 2


def test_token_watches_db_wal_journal_key_and_both_parent_directories(world):
    token_inputs = adapter._external_catalog_token()
    assert token_inputs is not None
    watched = {path for path, _sig in token_inputs[0][0]}
    assert watched == {
        world.db, world.db + '-wal', world.db + '-journal', str(world.key),
        os.path.dirname(world.db),
        # production validates this literal directory on every open
        '/var/lib/hayagarden',
    }
