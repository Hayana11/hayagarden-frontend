"""Step 7 cross-window lock — Cases 1–6 (temp DB / temp jobs dir only)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import daily_context as dc
from chat import window_identity as wi
from wake import executor as wake_exec


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            cache_info TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.execute(
        '''CREATE TABLE wake_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            woke_at TIMESTAMP DEFAULT (datetime('now','+8 hours')),
            thoughts TEXT,
            action TEXT,
            content TEXT,
            consumed INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0,
            cache_info TEXT DEFAULT '',
            wake_run_id TEXT DEFAULT ''
        )'''
    )
    conn.execute(
        '''CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT, content TEXT, layer TEXT, author TEXT, processed INTEGER DEFAULT 0
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db)
    conn = sqlite3.connect(db)
    wi.ensure_wake_window_identity_columns(conn)
    conn.commit()
    conn.close()


def _open_ctx(db: str, *, epoch: int = 1, gen: int = 1, chat_id: str = 'default') -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    now_s = '2026-07-30 12:00:00'
    cur = conn.execute(
        '''INSERT INTO daily_contexts (
            chat_id, local_day, timezone, boundary_hour, context_epoch,
            boundary_message_id, status, carryover_count, is_backfill,
            resident_generation, version, created_at, updated_at,
            window_mode, opened_at
        ) VALUES (?, '2026-07-30', 'Asia/Shanghai', 4, ?, 0, 'PROVISIONAL', 0, 0,
                  ?, 1, ?, ?, 'manual', ?)''',
        (chat_id, epoch, gen, now_s, now_s, now_s),
    )
    cid = int(cur.lastrowid)
    conn.commit()
    row = dict(conn.execute('SELECT * FROM daily_contexts WHERE id=?', (cid,)).fetchone())
    conn.close()
    return row


def _switch_canonical(db: str, old_id: int, *, new_epoch: int, new_gen: int = 1) -> dict:
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE daily_contexts SET closed_at='2026-07-30 13:00:00', close_reason='manual' WHERE id=?",
        (int(old_id),),
    )
    conn.commit()
    conn.close()
    return _open_ctx(db, epoch=new_epoch, gen=new_gen)


def _identity_from_row(row: dict) -> dict:
    return {
        'chat_id': str(row.get('chat_id') or 'default'),
        'context_id': int(row['id']),
        'context_epoch': int(row['context_epoch']),
        'resident_generation': int(row.get('resident_generation') or 1),
    }


def _get_db_fn(db: str):
    def get_db():
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn
    return get_db


def _count_chat(db: str, *, source_kind: str | None = None) -> int:
    conn = sqlite3.connect(db)
    if source_kind is None:
        n = int(conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0])
    else:
        n = int(conn.execute(
            'SELECT COUNT(*) FROM chat_messages WHERE source_kind=?', (source_kind,),
        ).fetchone()[0])
    conn.close()
    return n


def _workspace_hook_like(event: dict, db: str) -> bool:
    """Mirror gateway._workspace_job_event_hook: atomic persist then queue (flag-on)."""
    import tools.workspace_jobs as wj

    if wi.soft_window_enabled():
        outcome = wi.persist_workspace_job_chat_if_current(event, db_path=db)
        if not outcome.get('persisted'):
            wi.log_stale_async_result(
                source='workspace_job',
                reason=outcome.get('reason') or wi.REASON_UNAVAILABLE,
                job_id=(event.get('meta') or {}).get('job_id'),
                captured_identity=(
                    outcome.get('captured_identity') or event.get('window_identity')
                ),
                current_identity=outcome.get('current_identity'),
            )
            return False
        wj.queue_event(event)
        return True

    # Flag-off legacy mirror
    wj.queue_event(event)
    meta = event.get('meta') or {}
    tc = [{
        'name': 'ws_job',
        'args': {'action': 'status', 'id': meta.get('job_id')},
        'result': '{}',
        'success': True,
        'job': meta,
    }]
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO chat_messages (author, content, tool_calls, source_kind) "
        "VALUES ('assistant', ?, ?, 'workspace_job')",
        (event.get('content', ''), json.dumps(tc)),
    )
    conn.commit()
    conn.close()
    return True


def _drain_deliverable(db: str) -> list[dict]:
    import tools.workspace_jobs as wj
    out = []
    for ev in wj.drain_pending_events():
        if ev.get('type') != 'job_finished':
            continue
        reason, _, _ = wi.evaluate_async_delivery(ev.get('window_identity'), db_path=db)
        if reason != wi.REASON_OK:
            continue
        out.append(ev)
    return out


class CrossWindowLockStep7Tests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat(self.db)
        self.jobs_dir = tempfile.TemporaryDirectory()
        os.environ['EXEC_CWD'] = self.jobs_dir.name
        os.environ['WORKSPACE_ROOT'] = self.jobs_dir.name
        os.environ['EXEC_ENABLED'] = '1'
        import importlib
        import tools.workspace_executor as ex
        import tools.workspace_jobs as wj
        self.ex = importlib.reload(ex)
        self.wj = importlib.reload(wj)
        # Avoid chown failures in sandbox.
        self.wj._harden_workspace_path = lambda *a, **k: None
        self._flag = mock.patch.object(wi, 'soft_window_enabled', return_value=True)
        self._flag.start()
        self._dc_flag = mock.patch.object(dc, 'enabled', return_value=True)
        self._dc_flag.start()

    def tearDown(self):
        self._flag.stop()
        self._dc_flag.stop()
        self.jobs_dir.cleanup()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    # ── Case 1 ──────────────────────────────────────────────
    def test_case1_current_window_workspace_job_ok(self):
        ctx = _open_ctx(self.db, epoch=4, gen=2)
        identity = _identity_from_row(ctx)
        fake_proc = mock.Mock()
        fake_proc.pid = 4242
        with mock.patch('subprocess.Popen', return_value=fake_proc), \
             mock.patch('threading.Thread'):
            result = json.loads(self.wj.ws_job(
                {'action': 'start', 'cmd': 'echo hi', 'name': 't1', 'notify': True},
                conversation_id='hayana-chat',
                window_identity=identity,
            ))
        self.assertTrue(result.get('ok'), result)
        job_id = result['id']
        meta_path = Path(self.jobs_dir.name) / '.jobs' / f'{job_id}.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        self.assertEqual(meta['window_identity'], identity)

        event = {
            'type': 'job_finished',
            'conversation_id': 'hayana-chat',
            'content': '[ws_job] done',
            'preview': 't1 succeeded',
            'log_tail': '',
            'meta': {'job_id': job_id, 'status': 'succeeded', 'exit_code': 0, 'name': 't1'},
            'window_identity': meta['window_identity'],
        }
        delivered = _workspace_hook_like(event, self.db)
        self.assertTrue(delivered)
        self.assertEqual(_count_chat(self.db, source_kind='workspace_job'), 1)
        pending = _drain_deliverable(self.db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['window_identity'], identity)

    # ── Case 2 ──────────────────────────────────────────────
    def test_case2_stale_window_workspace_job_blocked(self):
        ctx_a = _open_ctx(self.db, epoch=1, gen=1)
        identity_a = _identity_from_row(ctx_a)
        fake_proc = mock.Mock()
        fake_proc.pid = 4243
        with mock.patch('subprocess.Popen', return_value=fake_proc), \
             mock.patch('threading.Thread'):
            result = json.loads(self.wj.ws_job(
                {'action': 'start', 'cmd': 'echo stale', 'name': 'old', 'notify': True},
                window_identity=identity_a,
            ))
        self.assertTrue(result.get('ok'), result)
        job_id = result['id']
        meta_path = Path(self.jobs_dir.name) / '.jobs' / f'{job_id}.json'
        self.assertTrue(meta_path.exists())
        log_path = Path(self.jobs_dir.name) / '.jobs' / f'{job_id}.log'
        log_path.write_text('log kept\n', encoding='utf-8')

        _switch_canonical(self.db, int(ctx_a['id']), new_epoch=2, new_gen=3)

        event = {
            'type': 'job_finished',
            'content': 'should not land',
            'preview': 'old failed',
            'log_tail': 'log kept',
            'meta': {'job_id': job_id, 'status': 'failed', 'exit_code': 1, 'name': 'old'},
            'window_identity': identity_a,
        }
        with self.assertLogs('chat.window_identity', level='WARNING') as cm:
            delivered = _workspace_hook_like(event, self.db)
        self.assertFalse(delivered)
        self.assertEqual(_count_chat(self.db, source_kind='workspace_job'), 0)
        self.assertTrue(any('stale_window_result' in line for line in cm.output))
        self.assertTrue(meta_path.exists())
        self.assertTrue(log_path.exists())
        # Even if somehow queued, drain must drop
        self.wj.queue_event(event)
        self.assertEqual(_drain_deliverable(self.db), [])

    # ── Case 3 ──────────────────────────────────────────────
    def test_case3_current_window_wake_ok(self):
        ctx = _open_ctx(self.db, epoch=5, gen=1)
        identity = _identity_from_row(ctx)
        wake_exec.execute(
            'message', 'thinking', 'hello from wake',
            'normal', _get_db_fn(self.db),
            wake_run_id='wake-case3',
            window_identity=identity,
        )
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM wake_log ORDER BY id DESC LIMIT 1').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row['consumed']), 0)
        self.assertEqual(str(row['chat_id']), 'default')
        self.assertEqual(int(row['context_id']), identity['context_id'])
        self.assertEqual(int(row['context_epoch']), 5)
        self.assertEqual(int(row['resident_generation']), 1)
        self.assertEqual(_count_chat(self.db, source_kind='wake'), 1)
        ids = wi.fetch_claimable_wake_ids(_get_db_fn(self.db))
        self.assertEqual(ids, [int(row['id'])])
        injected = wi.fetch_unconsumed_wakes_for_injection(
            _get_db_fn(self.db), 'id, woke_at, action, content',
        )
        self.assertEqual(len(injected), 1)
        note_conn = _get_db_fn(self.db)()
        note = wi.fetch_pending_wake_notification_row(note_conn)
        note_conn.close()
        self.assertIsNotNone(note)
        self.assertEqual(int(note['id']), int(row['id']))
        conn.close()

    # ── Case 4 ──────────────────────────────────────────────
    def test_case4_stale_wake_blocked(self):
        ctx_a = _open_ctx(self.db, epoch=1, gen=1)
        identity_a = _identity_from_row(ctx_a)
        _switch_canonical(self.db, int(ctx_a['id']), new_epoch=9, new_gen=2)
        with self.assertLogs('chat.window_identity', level='WARNING') as cm:
            wake_exec.execute(
                'message', 't', 'should not show',
                'normal', _get_db_fn(self.db),
                wake_run_id='wake-case4',
                window_identity=identity_a,
            )
        self.assertTrue(any('stale_window_result' in line for line in cm.output))
        self.assertEqual(_count_chat(self.db, source_kind='wake'), 0)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM wake_log ORDER BY id DESC LIMIT 1').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row['consumed']), 1)
        self.assertEqual(int(row['notified']), 1)
        self.assertEqual(int(row['context_id']), identity_a['context_id'])
        self.assertEqual(int(row['context_epoch']), identity_a['context_epoch'])
        conn.close()
        self.assertEqual(wi.fetch_claimable_wake_ids(_get_db_fn(self.db)), [])
        self.assertEqual(
            wi.fetch_unconsumed_wakes_for_injection(
                _get_db_fn(self.db), 'id, woke_at, action, content',
            ),
            [],
        )
        note_conn = _get_db_fn(self.db)()
        self.assertIsNone(wi.fetch_pending_wake_notification_row(note_conn))
        note_conn.close()

    # ── Case 5 ──────────────────────────────────────────────
    def test_case5_missing_identity_fail_closed(self):
        _open_ctx(self.db, epoch=1, gen=1)
        # workspace job start without identity
        result = json.loads(self.wj.ws_job(
            {'action': 'start', 'cmd': 'echo x'},
            window_identity=None,
        ))
        self.assertEqual(result.get('error'), 'window_identity_unavailable')

        # finish event missing identity must not deliver / must not bind to current
        event = {
            'type': 'job_finished',
            'content': 'no id',
            'meta': {'job_id': 'job_deadbeefdead', 'status': 'succeeded'},
            'window_identity': None,
        }
        with self.assertLogs('chat.window_identity', level='WARNING'):
            self.assertFalse(_workspace_hook_like(event, self.db))
        self.assertEqual(_count_chat(self.db, source_kind='workspace_job'), 0)

        with self.assertLogs('chat.window_identity', level='WARNING'):
            wake_exec.execute(
                'message', 't', 'orphan wake',
                'normal', _get_db_fn(self.db),
                wake_run_id='wake-case5',
                window_identity=None,
            )
        self.assertEqual(_count_chat(self.db, source_kind='wake'), 0)
        self.assertEqual(wi.fetch_claimable_wake_ids(_get_db_fn(self.db)), [])
        note_conn = _get_db_fn(self.db)()
        self.assertIsNone(wi.fetch_pending_wake_notification_row(note_conn))
        note_conn.close()

    # ── Case 6 ──────────────────────────────────────────────
    def test_case6_flag_off_legacy_compatible(self):
        self._flag.stop()
        self._dc_flag.stop()
        self._flag = mock.patch.object(wi, 'soft_window_enabled', return_value=False)
        self._dc_flag = mock.patch.object(dc, 'enabled', return_value=False)
        self._flag.start()
        self._dc_flag.start()

        fake_proc = mock.Mock()
        fake_proc.pid = 4244
        with mock.patch('subprocess.Popen', return_value=fake_proc), \
             mock.patch('threading.Thread'):
            result = json.loads(self.wj.ws_job(
                {'action': 'start', 'cmd': 'echo legacy'},
                window_identity=None,
            ))
        self.assertTrue(result.get('ok'), result)
        job_id = result['id']
        meta = json.loads(
            (Path(self.jobs_dir.name) / '.jobs' / f'{job_id}.json').read_text(encoding='utf-8'),
        )
        self.assertNotIn('window_identity', meta)

        event = {
            'type': 'job_finished',
            'content': 'legacy job',
            'meta': {'job_id': job_id, 'status': 'succeeded'},
        }
        self.assertTrue(_workspace_hook_like(event, self.db))
        self.assertEqual(_count_chat(self.db, source_kind='workspace_job'), 1)

        wake_exec.execute(
            'message', 't', 'legacy wake',
            'normal', _get_db_fn(self.db),
            wake_run_id='wake-case6',
            window_identity=None,
        )
        self.assertEqual(_count_chat(self.db, source_kind='wake'), 1)
        conn = sqlite3.connect(self.db)
        n = int(conn.execute(
            'SELECT COUNT(*) FROM wake_log WHERE consumed=0 AND content=?',
            ('legacy wake',),
        ).fetchone()[0])
        conn.close()
        self.assertEqual(n, 1)
        ids = wi.fetch_claimable_wake_ids(_get_db_fn(self.db))
        self.assertTrue(ids)

    def test_atomic_job_persist_blocks_concurrent_canonical_switch(self):
        """Regression: canonical cannot change between gate and INSERT."""
        ctx_a = _open_ctx(self.db, epoch=1, gen=1)
        identity_a = _identity_from_row(ctx_a)
        order: list[str] = []
        order_lock = threading.Lock()

        def record(name: str) -> None:
            with order_lock:
                order.append(name)

        gate_passed = threading.Event()
        switch_trying = threading.Event()
        errors: list[BaseException] = []

        def switch_worker() -> None:
            try:
                self.assertTrue(gate_passed.wait(timeout=5), 'gate_passed timeout')
                record('switch_attempted')
                switch_trying.set()
                conn2 = sqlite3.connect(self.db, timeout=30)
                try:
                    conn2.execute('BEGIN IMMEDIATE')
                    record('switch_acquired')
                    conn2.execute(
                        "UPDATE daily_contexts SET closed_at='2026-07-30 13:00:00', "
                        "close_reason='manual' WHERE id=?",
                        (int(ctx_a['id']),),
                    )
                    now_s = '2026-07-30 13:00:01'
                    conn2.execute(
                        '''INSERT INTO daily_contexts (
                            chat_id, local_day, timezone, boundary_hour, context_epoch,
                            boundary_message_id, status, carryover_count, is_backfill,
                            resident_generation, version, created_at, updated_at,
                            window_mode, opened_at
                        ) VALUES ('default', '2026-07-30', 'Asia/Shanghai', 4, 2, 0,
                                  'PROVISIONAL', 0, 0, 3, 1, ?, ?, 'manual', ?)''',
                        (now_s, now_s, now_s),
                    )
                    conn2.commit()
                    record('switch_committed')
                finally:
                    conn2.close()
            except BaseException as exc:  # noqa: BLE001 — surface in main thread
                errors.append(exc)

        def after_gate(_conn) -> None:
            record('gate_passed')
            gate_passed.set()
            self.assertTrue(switch_trying.wait(timeout=5), 'switch_trying timeout')

        def after_insert(_conn) -> None:
            record('job_inserted')

        def after_commit() -> None:
            record('job_committed')

        event = {
            'type': 'job_finished',
            'content': 'atomic job row',
            'meta': {
                'job_id': 'job_atomic00001',
                'status': 'succeeded',
                'exit_code': 0,
                'name': 'atomic',
            },
            'window_identity': identity_a,
            'log_tail': '',
        }

        switch_thread = threading.Thread(target=switch_worker, name='canonical-switch')
        switch_thread.start()
        outcome = wi.persist_workspace_job_chat_if_current(
            event,
            db_path=self.db,
            after_gate_ok=after_gate,
            after_insert=after_insert,
            after_commit=after_commit,
        )
        switch_thread.join(timeout=10)
        self.assertFalse(switch_thread.is_alive(), 'switch thread hung')
        self.assertEqual(errors, [])
        self.assertTrue(outcome.get('persisted'), outcome)
        self.assertEqual(outcome.get('reason'), wi.REASON_OK)

        # Strong ordering: job commit before switch acquires the write lock.
        self.assertIn('job_committed', order)
        self.assertIn('switch_acquired', order)
        self.assertLess(
            order.index('job_committed'),
            order.index('switch_acquired'),
            order,
        )
        for required in (
            'gate_passed',
            'switch_attempted',
            'job_inserted',
            'job_committed',
            'switch_acquired',
            'switch_committed',
        ):
            self.assertIn(required, order, order)

        self.assertEqual(_count_chat(self.db, source_kind='workspace_job'), 1)
        # After switch to B, pending event with A identity must be dropped.
        import tools.workspace_jobs as wj
        wj.queue_event(event)
        self.assertEqual(_drain_deliverable(self.db), [])
        self.assertEqual(_count_chat(self.db, source_kind='workspace_job'), 1)


if __name__ == '__main__':
    unittest.main()
