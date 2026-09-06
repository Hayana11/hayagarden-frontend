"""Regression contract for CC resident invalidation after history rewrites."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-cc-history-rewrite-config.db'),
)

from chat.cc_history_rewrite import (
    current_history_rewrite_epoch,
    guard_cc_generation,
    history_rewrite_epoch_reason,
    note_durable_history_rewrite,
    serialize_history_rewrite,
)


def _chat_schema(conn):
    conn.executescript(
        """
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT,
            content TEXT,
            thinking TEXT,
            tool_calls TEXT,
            branches TEXT,
            branch_idx INTEGER,
            image_url TEXT DEFAULT '',
            file_url TEXT DEFAULT '',
            file_name TEXT DEFAULT '',
            choices TEXT DEFAULT '',
            display_segments TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT DEFAULT (datetime('now','+8 hours'))
        );
        CREATE TABLE chat_edit_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fork_msg_id INTEGER,
            original_content TEXT,
            messages_json TEXT
        );
        """
    )


def _make_get_db(path):
    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    return get_db


class _FakeProc:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code or 0


def _warm_resident(*, epoch=None):
    from cc_resident import ResidentSession

    resident = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
    resident._proc = _FakeProc()
    resident._system_text = 'system'
    resident._session_id = 'old-world-session'
    resident._cold = False
    resident._last_used = time.time()
    if epoch is None:
        epoch = current_history_rewrite_epoch()
    resident._history_rewrite_epoch = epoch
    return resident


def _authoritative_history(get_db):
    conn = get_db()
    try:
        return '\n'.join(
            row['content']
            for row in conn.execute('SELECT content FROM chat_messages ORDER BY id')
        )
    finally:
        conn.close()


def _assert_next_generation_cold(testcase, resident, get_db):
    spawn_reasons = []

    def fake_spawn(system_text, env, *, reason='process_dead', tool_profile='legacy'):
        spawn_reasons.append(reason)
        resident._proc = _FakeProc()
        resident._system_text = system_text
        resident._session_id = 'new-world-session'
        resident._cold = True
        resident._generation += 1
        resident._next_spawn_reason = None
        resident._history_rewrite_epoch = current_history_rewrite_epoch()
        resident._reset_session_meta(respawn_reason=reason)

    with mock.patch.object(resident, '_spawn', side_effect=fake_spawn):
        testcase.assertTrue(resident.ensure_alive('system', {}))
    testcase.assertEqual(len(spawn_reasons), 1)
    testcase.assertEqual(spawn_reasons[0], 'history_rewrite')
    return _authoritative_history(get_db)


def _assert_hot_reuse(testcase, resident):
    # After a cold spawn, ResidentSession stays `_cold=True` until a turn
    # commits; simulate that warm state so ensure_alive's return value is
    # meaningful while still asserting no respawn.
    resident._cold = False
    old_proc = resident._proc
    old_generation = resident._generation
    old_epoch = resident._history_rewrite_epoch
    testcase.assertIsNone(resident.peek_respawn_reason('system'))
    testcase.assertFalse(resident.ensure_alive('system', {}))
    testcase.assertIs(resident._proc, old_proc)
    testcase.assertEqual(resident._generation, old_generation)
    testcase.assertEqual(resident._history_rewrite_epoch, old_epoch)


def _ensure_app_importable():
    root = Path('/opt/frontend')
    root.mkdir(parents=True, exist_ok=True)
    env_path = root / '.env'
    if not env_path.exists():
        try:
            env_path.touch()
        except OSError:
            pass
    conn = sqlite3.connect(str(root / 'memories.db'))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT,
                content TEXT,
                thinking TEXT,
                tool_calls TEXT,
                branches TEXT,
                branch_idx INTEGER,
                image_url TEXT DEFAULT '',
                file_url TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                choices TEXT DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'chat',
                created_at TEXT DEFAULT (datetime('now','+8 hours'))
            );
            CREATE TABLE IF NOT EXISTS chat_edit_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fork_msg_id INTEGER,
                original_content TEXT,
                messages_json TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _import_gateway():
    if 'gateway' in sys.modules:
        return sys.modules['gateway']
    stubbed = []
    if 'tools.workspace_registry' not in sys.modules:
        reg = mock.MagicMock()
        reg.TOOLS_NOTE = ''
        reg.build_resident_tool_defs.return_value = []
        reg.load_registry.return_value = []
        sys.modules['tools.workspace_registry'] = reg
        stubbed.append('tools.workspace_registry')
    if 'tools.workspace_agent' not in sys.modules:
        wa = mock.MagicMock()
        wa.get_workspace_tool_defs.return_value = []
        sys.modules['tools.workspace_agent'] = wa
        stubbed.append('tools.workspace_agent')
    import gateway
    for name in stubbed:
        sys.modules.pop(name, None)
    return gateway


def _reset_epoch_file():
    path = (
        os.environ.get('CC_HISTORY_REWRITE_EPOCH_PATH')
        or os.environ.get('CC_HISTORY_REWRITE_BARRIER_PATH')
    )
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


_ensure_app_importable()
sys.modules.setdefault('moments_cover', types.ModuleType('moments_cover'))
if 'account_balance_routes' not in sys.modules:
    from flask import Blueprint

    account_routes = types.ModuleType('account_balance_routes')
    account_routes.create_relay_account_blueprint = lambda **_kwargs: Blueprint(
        'cc_history_rewrite_account_stub', __name__,
    )
    sys.modules['account_balance_routes'] = account_routes
import app as app_module  # noqa: E402


class HistoryRewriteRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'chat.db')
        self.get_db = _make_get_db(self.db_path)
        conn = self.get_db()
        _chat_schema(conn)
        conn.commit()
        conn.close()
        self.epoch_path = str(Path(self.tmp.name) / 'rewrite.epoch')
        self.patches = [
            mock.patch.object(app_module, 'DB_PATH', self.db_path),
            mock.patch.object(app_module, 'get_db', self.get_db),
            mock.patch.dict(os.environ, {
                'CC_HISTORY_REWRITE_LOCK_PATH': str(Path(self.tmp.name) / 'rewrite.lock'),
                'CC_HISTORY_REWRITE_EPOCH_PATH': self.epoch_path,
                'INTERNAL_STATE_V3_SHADOW_ENABLED': '0',
                'INTERNAL_STATE_V3_SCORE_PROOF_ENABLED': '0',
                'INTERNAL_STATE_V3_USER_EVENTS_ENABLED': '0',
            }, clear=False),
        ]
        for patcher in self.patches:
            patcher.start()
        _reset_epoch_file()
        self.resident = _warm_resident()
        self.invalidation_calls = []
        self.history_at_invalidation = []

        def bridge(method, path, body=None, timeout=5):
            self.assertEqual(method, 'POST')
            self.assertEqual(path, '/internal/cc-resident/history-rewrite')
            self.invalidation_calls.append(body['reason'])
            self.history_at_invalidation.append(_authoritative_history(self.get_db))
            # Eager kill only — durable epoch must remain for other workers.
            self.resident.invalidate_for_history_rewrite(body['reason'])
            return {
                'ok': True,
                'invalidated': True,
                'epoch': current_history_rewrite_epoch(),
            }

        self.bridge_patch = mock.patch.object(
            app_module, '_gw_json_request', side_effect=bridge,
        )
        self.bridge_patch.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.bridge_patch.stop()
        for patcher in reversed(self.patches):
            patcher.stop()
        _reset_epoch_file()
        self.tmp.cleanup()

    def _insert(self, author, content, **extra):
        conn = self.get_db()
        cur = conn.execute(
            'INSERT INTO chat_messages (author, content, thinking, tool_calls, branches, branch_idx) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (
                author, content, extra.get('thinking', ''), extra.get('tool_calls', ''),
                extra.get('branches', ''), extra.get('branch_idx', 0),
            ),
        )
        conn.commit()
        row_id = int(cur.lastrowid)
        conn.close()
        return row_id

    def test_edit_invalidates_then_cold_bootstraps_only_edited_history(self):
        from chat import rewrite_staging as _rw
        edit_id = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        self._insert('hayana', 'U2')
        self._insert('assistant', 'A2')
        # Prepare alone must not rewrite active history / invalidate.
        prep = self.client.post('/api/chat/edit', json={'msg_id': edit_id, 'content': "U1'"})
        self.assertEqual(prep.status_code, 200)
        self.assertEqual(self.invalidation_calls, [])
        rewrite_id = prep.get_json()['rewrite_id']
        conn = self.get_db()
        _rw.store_candidate(conn, rewrite_id, content='NEW_A1')
        conn.commit()
        conn.close()
        response = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rewrite_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.invalidation_calls, ['edit'])
        self.assertEqual(self.history_at_invalidation, ["U1'\nNEW_A1"])
        epoch = current_history_rewrite_epoch()
        self.assertTrue(epoch)
        history = _assert_next_generation_cold(self, self.resident, self.get_db)
        self.assertEqual(history.split('\n'), ["U1'", 'NEW_A1'])
        self.assertEqual(current_history_rewrite_epoch(), epoch)
        self.assertEqual(self.resident._history_rewrite_epoch, epoch)

    def test_regenerate_invalidates_before_new_generation(self):
        from chat import rewrite_staging as _rw
        self._insert('hayana', 'U1')
        assistant_id = self._insert('assistant', 'A1')
        # Prepare is non-destructive: active A1 remains; no invalidate yet.
        response = self.client.post('/api/chat/regen/prepare', json={'msg_id': assistant_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.invalidation_calls, [])
        rewrite_id = response.get_json()['rewrite_id']
        conn = self.get_db()
        rows = [dict(r) for r in conn.execute(
            'SELECT id, author, content FROM chat_messages ORDER BY id'
        ).fetchall()]
        overlay = _rw.apply_history_overlay(
            rows, _rw.load(conn, rewrite_id),
        )
        conn.close()
        self.assertEqual([r['content'] for r in overlay], ['U1'])
        # Activation of the new variant is the durable rewrite boundary.
        conn = self.get_db()
        _rw.store_candidate(conn, rewrite_id, content='NEW_A1')
        conn.commit()
        conn.close()
        fin = self.client.post('/api/chat/regen/finalize', json={'rewrite_id': rewrite_id})
        self.assertEqual(fin.status_code, 200)
        self.assertEqual(self.invalidation_calls, ['regen_finalize'])
        history = _assert_next_generation_cold(self, self.resident, self.get_db)
        self.assertEqual(history.split('\n'), ['U1', 'NEW_A1'])

    def test_branch_switch_invalidates_and_cold_uses_selected_answer(self):
        self._insert('hayana', 'U1')
        assistant_id = self._insert(
            'assistant', "A1'",
            branches=json.dumps([{'content': 'A1'}, {'content': "A1'"}]),
            branch_idx=1,
        )
        response = self.client.post(
            '/api/chat/branch/switch', json={'msg_id': assistant_id, 'direction': -1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.invalidation_calls, ['branch_switch'])
        history = _assert_next_generation_cold(self, self.resident, self.get_db)
        self.assertIn('A1', history)
        self.assertNotIn("A1'", history)

    def test_delete_invalidates_then_cold_excludes_deleted(self):
        self._insert('hayana', 'U1')
        assistant_id = self._insert('assistant', 'A1')
        response = self.client.post('/api/chat/delete', json={'msg_id': assistant_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})
        self.assertEqual(self.invalidation_calls, ['delete'])
        self.assertEqual(self.history_at_invalidation, ['U1'])
        history = _assert_next_generation_cold(self, self.resident, self.get_db)
        self.assertIn('U1', history)
        self.assertNotIn('A1', history)

    def test_missing_delete_does_not_invalidate(self):
        self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        response = self.client.post('/api/chat/delete', json={'msg_id': 999})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})
        self.assertEqual(self.invalidation_calls, [])
        self.assertEqual(current_history_rewrite_epoch(), '')
        _assert_hot_reuse(self, self.resident)

    def test_failed_edit_does_not_invalidate_hot_resident(self):
        response = self.client.post('/api/chat/edit', json={'msg_id': 999, 'content': 'missing'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.invalidation_calls, [])
        self.assertEqual(current_history_rewrite_epoch(), '')
        _assert_hot_reuse(self, self.resident)

    def test_branch_noop_does_not_invalidate_hot_resident(self):
        self._insert('hayana', 'U1')
        assistant_id = self._insert(
            'assistant', 'A1',
            branches=json.dumps([{'content': 'A1'}, {'content': "A1'"}]),
            branch_idx=0,
        )
        response = self.client.post(
            '/api/chat/branch/switch', json={'msg_id': assistant_id, 'direction': -1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.invalidation_calls, [])
        self.assertEqual(current_history_rewrite_epoch(), '')
        _assert_hot_reuse(self, self.resident)

    def test_bridge_failure_after_durable_rewrite_fail_closed(self):
        from chat import rewrite_staging as _rw
        edit_id = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': edit_id, 'content': "U1'"},
        )
        self.assertEqual(prep.status_code, 200)
        rewrite_id = prep.get_json()['rewrite_id']
        conn = self.get_db()
        _rw.store_candidate(conn, rewrite_id, content='NEW_A1')
        conn.commit()
        conn.close()
        with mock.patch.object(
            app_module, '_gw_json_request', return_value={'error': 'bridge refused'},
        ):
            response = self.client.post(
                '/api/chat/edit/finalize', json={'rewrite_id': rewrite_id},
            )
        # Bridge is acceleration-only: committed rewrite stays API success.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get('ok'), True)
        self.assertEqual(self.invalidation_calls, [])
        epoch = current_history_rewrite_epoch()
        self.assertTrue(epoch)
        self.assertEqual(history_rewrite_epoch_reason(), 'edit')
        self.assertTrue(self.resident._alive())
        self.assertEqual(self.resident._session_id, 'old-world-session')
        history = _assert_next_generation_cold(self, self.resident, self.get_db)
        self.assertEqual(history.split('\n'), ["U1'", 'NEW_A1'])
        # Epoch is durable: cold catch-up must not clear it.
        self.assertEqual(current_history_rewrite_epoch(), epoch)
        self.assertEqual(self.resident._history_rewrite_epoch, epoch)
        _assert_hot_reuse(self, self.resident)

    def test_bridge_failure_after_durable_delete_fail_closed(self):
        self._insert('hayana', 'U1')
        assistant_id = self._insert('assistant', 'A1')
        with mock.patch.object(
            app_module, '_gw_json_request', return_value={'error': 'bridge refused'},
        ):
            response = self.client.post('/api/chat/delete', json={'msg_id': assistant_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})
        self.assertEqual(self.invalidation_calls, [])
        epoch = current_history_rewrite_epoch()
        self.assertTrue(epoch)
        self.assertEqual(history_rewrite_epoch_reason(), 'delete')
        history = _assert_next_generation_cold(self, self.resident, self.get_db)
        self.assertIn('U1', history)
        self.assertNotIn('A1', history)
        self.assertEqual(current_history_rewrite_epoch(), epoch)

    def test_unrelated_normal_turn_still_reuses_hot_resident(self):
        _assert_hot_reuse(self, self.resident)
        self.assertEqual(self.invalidation_calls, [])
        self.assertEqual(current_history_rewrite_epoch(), '')


class HistoryRewriteMultiWorkerEpochTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.epoch_path = str(Path(self.tmp.name) / 'rewrite.epoch')
        self.env_patch = mock.patch.dict(os.environ, {
            'CC_HISTORY_REWRITE_EPOCH_PATH': self.epoch_path,
        }, clear=False)
        self.env_patch.start()
        _reset_epoch_file()

    def tearDown(self):
        self.env_patch.stop()
        _reset_epoch_file()
        self.tmp.cleanup()

    def test_multi_worker_lazy_invalidation_and_unique_epochs(self):
        def _cold(resident):
            spawn_reasons = []

            def fake_spawn(system_text, env, *, reason='process_dead', tool_profile='legacy'):
                spawn_reasons.append(reason)
                resident._proc = _FakeProc()
                resident._system_text = system_text
                resident._session_id = 'new-world-session'
                resident._cold = True
                resident._generation += 1
                resident._next_spawn_reason = None
                resident._history_rewrite_epoch = current_history_rewrite_epoch()
                resident._reset_session_meta(respawn_reason=reason)

            with mock.patch.object(resident, '_spawn', side_effect=fake_spawn):
                self.assertTrue(resident.ensure_alive('system', {}))
            self.assertEqual(spawn_reasons, ['history_rewrite'])
            return spawn_reasons

        pre_epoch = current_history_rewrite_epoch()
        worker_a = _warm_resident(epoch=pre_epoch)
        worker_b = _warm_resident(epoch=pre_epoch)
        worker_c = _warm_resident(epoch=pre_epoch)

        epoch1 = note_durable_history_rewrite('edit')
        self.assertNotEqual(epoch1, pre_epoch)
        worker_a.invalidate_for_history_rewrite('edit')
        self.assertFalse(worker_a._alive())
        self.assertTrue(worker_b._alive())
        self.assertEqual(worker_b._history_rewrite_epoch, pre_epoch)
        self.assertEqual(current_history_rewrite_epoch(), epoch1)

        _cold(worker_b)
        self.assertEqual(worker_b._history_rewrite_epoch, epoch1)
        self.assertEqual(current_history_rewrite_epoch(), epoch1)
        # B catching up must not clear epoch for still-stale C.
        self.assertTrue(worker_c._alive())
        self.assertEqual(worker_c._history_rewrite_epoch, pre_epoch)
        _assert_hot_reuse(self, worker_b)

        _cold(worker_c)
        self.assertEqual(worker_c._history_rewrite_epoch, epoch1)
        _assert_hot_reuse(self, worker_c)

        # Same reason twice still advances a new unique epoch.
        epoch2 = note_durable_history_rewrite('edit')
        self.assertNotEqual(epoch2, epoch1)
        self.assertEqual(history_rewrite_epoch_reason(), 'edit')
        self.assertTrue(worker_b._alive())
        _cold(worker_b)
        self.assertEqual(worker_b._history_rewrite_epoch, epoch2)
        self.assertEqual(current_history_rewrite_epoch(), epoch2)

        # A second still-stale reconstruction also colds on epoch2.
        stale_again = _warm_resident(epoch=epoch1)
        self.assertTrue(stale_again._alive())
        _cold(stale_again)
        self.assertEqual(stale_again._history_rewrite_epoch, epoch2)

    def test_failed_spawn_does_not_bind_new_epoch(self):
        pre_epoch = current_history_rewrite_epoch()
        resident = _warm_resident(epoch=pre_epoch)
        epoch1 = note_durable_history_rewrite('delete')

        def boom(*_a, **_k):
            raise RuntimeError('spawn failed')

        with mock.patch.object(resident, '_spawn', side_effect=boom):
            with self.assertRaises(RuntimeError):
                resident.ensure_alive('system', {})
        self.assertEqual(resident._history_rewrite_epoch, pre_epoch)
        self.assertEqual(current_history_rewrite_epoch(), epoch1)
        self.assertNotEqual(resident._history_rewrite_epoch, epoch1)


class HistoryRewriteLockTests(unittest.TestCase):
    def test_generation_and_rewrite_cannot_cross(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {'CC_HISTORY_REWRITE_LOCK_PATH': str(Path(tmp) / 'guard.lock')},
            clear=False,
        ):
            generation_entered = threading.Event()
            release_generation = threading.Event()
            rewrite_entered = threading.Event()

            def events():
                generation_entered.set()
                self.assertTrue(release_generation.wait(2))
                yield 'done'

            @serialize_history_rewrite
            def rewrite():
                rewrite_entered.set()

            generation = threading.Thread(target=lambda: list(guard_cc_generation(events())))
            generation.start()
            self.assertTrue(generation_entered.wait(1))
            writer = threading.Thread(target=rewrite)
            writer.start()
            self.assertFalse(rewrite_entered.wait(0.1))
            release_generation.set()
            generation.join(2)
            writer.join(2)
            self.assertTrue(rewrite_entered.is_set())

    def test_delete_and_generation_cannot_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'chat.db')
            get_db = _make_get_db(db_path)
            conn = get_db()
            _chat_schema(conn)
            cur = conn.execute(
                "INSERT INTO chat_messages (author, content) VALUES ('assistant', 'A1')"
            )
            msg_id = int(cur.lastrowid)
            conn.commit()
            conn.close()

            generation_entered = threading.Event()
            release_generation = threading.Event()
            delete_entered = threading.Event()

            def events():
                generation_entered.set()
                self.assertTrue(release_generation.wait(2))
                yield 'done'

            with mock.patch.object(app_module, 'DB_PATH', db_path), \
                 mock.patch.object(app_module, 'get_db', get_db), \
                 mock.patch.object(
                     app_module, 'invalidate_cc_resident_for_history_rewrite',
                 ), \
                 mock.patch.dict(os.environ, {
                     'CC_HISTORY_REWRITE_LOCK_PATH': str(Path(tmp) / 'guard.lock'),
                     'CC_HISTORY_REWRITE_EPOCH_PATH': str(Path(tmp) / 'rewrite.epoch'),
                 }, clear=False):
                client = app_module.app.test_client()

                def do_delete():
                    delete_entered.set()
                    client.post('/api/chat/delete', json={'msg_id': msg_id})

                generation = threading.Thread(
                    target=lambda: list(guard_cc_generation(events())),
                )
                generation.start()
                self.assertTrue(generation_entered.wait(1))
                writer = threading.Thread(target=do_delete)
                writer.start()
                self.assertTrue(delete_entered.wait(1))
                time.sleep(0.1)
                conn = get_db()
                still = conn.execute(
                    'SELECT content FROM chat_messages WHERE id=?', (msg_id,),
                ).fetchone()
                conn.close()
                self.assertIsNotNone(still)
                release_generation.set()
                generation.join(2)
                writer.join(2)
                conn = get_db()
                gone = conn.execute(
                    'SELECT content FROM chat_messages WHERE id=?', (msg_id,),
                ).fetchone()
                conn.close()
                self.assertIsNone(gone)


class HistoryRewriteEpochTests(unittest.TestCase):
    def test_note_produces_unique_epochs_for_same_reason(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {'CC_HISTORY_REWRITE_EPOCH_PATH': str(Path(tmp) / 'e.epoch')},
            clear=False,
        ):
            self.assertEqual(current_history_rewrite_epoch(), '')
            self.assertIsNone(history_rewrite_epoch_reason())
            e1 = note_durable_history_rewrite('edit')
            e2 = note_durable_history_rewrite('edit')
            self.assertTrue(e1)
            self.assertTrue(e2)
            self.assertNotEqual(e1, e2)
            self.assertEqual(current_history_rewrite_epoch(), e2)
            self.assertEqual(history_rewrite_epoch_reason(), 'edit')


class HistoryRewriteSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = _import_gateway()

    def test_history_rewrite_endpoint_allows_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            epoch_path = str(Path(tmp) / 'sec.epoch')
            with mock.patch.dict(os.environ, {
                'CC_HISTORY_REWRITE_EPOCH_PATH': epoch_path,
            }, clear=False):
                _reset_epoch_file()
                resident = _warm_resident()
                holder = self.gateway._CC_RESIDENT
                previous = holder.swap(resident)
                try:
                    epoch = note_durable_history_rewrite('sec')
                    client = self.gateway.app.test_client()
                    response = client.post(
                        '/internal/cc-resident/history-rewrite',
                        json={'reason': 'sec'},
                        environ_base={'REMOTE_ADDR': '127.0.0.1'},
                    )
                    self.assertEqual(response.status_code, 200)
                    body = response.get_json() or {}
                    self.assertEqual(body.get('ok'), True)
                    self.assertEqual(body.get('epoch'), epoch)
                    self.assertFalse(resident._alive())
                    # Eager invalidate must not consume the durable epoch.
                    self.assertEqual(current_history_rewrite_epoch(), epoch)
                finally:
                    holder.swap(previous)
                    _reset_epoch_file()

    def test_history_rewrite_endpoint_rejects_non_loopback(self):
        resident = _warm_resident()
        old_proc = resident._proc
        holder = self.gateway._CC_RESIDENT
        previous = holder.swap(resident)
        try:
            client = self.gateway.app.test_client()
            response = client.post(
                '/internal/cc-resident/history-rewrite',
                json={'reason': 'sec'},
                environ_base={'REMOTE_ADDR': '203.0.113.9'},
            )
            self.assertEqual(response.status_code, 403)
            body = response.get_json() or {}
            self.assertEqual(body.get('error'), 'loopback only')
            self.assertIs(resident._proc, old_proc)
            self.assertTrue(resident._alive())
        finally:
            holder.swap(previous)


if __name__ == '__main__':
    unittest.main()
