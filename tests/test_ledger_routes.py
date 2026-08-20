"""Ledger API tests with import-time isolation from /opt/frontend production paths.

Tests redirect app import-time access to a temporary directory and never open,
create, or modify:
  /opt/frontend/memories.db
  /opt/frontend/.env
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROD_DB = '/opt/frontend/memories.db'
PROD_ENV = '/opt/frontend/.env'
PROD_GALLERY_DB = '/opt/frontend/gallery.db'
PROD_CLIENT_LOG = '/opt/frontend/client_errors.log'
PROD_PREFIX = '/opt/frontend/'

_ISOLATION_DIR = tempfile.mkdtemp(prefix='ledger-route-test-')
_ISOLATION_DB = str(Path(_ISOLATION_DIR) / 'memories.db')
_ISOLATION_ENV = str(Path(_ISOLATION_DIR) / '.env')
_ISOLATION_GALLERY_DB = str(Path(_ISOLATION_DIR) / 'gallery.db')
_ISOLATION_CLIENT_LOG = str(Path(_ISOLATION_DIR) / 'client_errors.log')
Path(_ISOLATION_ENV).write_text('', encoding='utf-8')
Path(_ISOLATION_DIR, 'gallery').mkdir(parents=True, exist_ok=True)

_ORIG_CONNECT = sqlite3.connect
_ORIG_OPEN = builtins.open
_ORIG_MAKEDIRS = os.makedirs


def _bootstrap_isolation_db() -> None:
    conn = _ORIG_CONNECT(_ISOLATION_DB)
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
        """
    )
    conn.commit()
    conn.close()


_bootstrap_isolation_db()

_PROD_CONNECT_TARGETS: list[str] = []


def _redirect_prod_path(path: str) -> str:
    if path == PROD_DB:
        return _ISOLATION_DB
    if path == PROD_GALLERY_DB:
        return _ISOLATION_GALLERY_DB
    if path.startswith(PROD_PREFIX):
        rel = path[len(PROD_PREFIX):]
        return str(Path(_ISOLATION_DIR) / rel)
    return path


_STRICT_GUARD = False


def _guarded_connect(database, *args, **kwargs):
    raw = str(database)
    if raw == PROD_DB:
        if _STRICT_GUARD:
            raise AssertionError('ledger tests must not connect to production database')
        _PROD_CONNECT_TARGETS.append(raw)
    database = _redirect_prod_path(raw)
    return _ORIG_CONNECT(database, *args, **kwargs)


def _guarded_open(file, *args, **kwargs):
    path = os.fsdecode(file) if isinstance(file, bytes) else str(file)
    if path == PROD_ENV:
        return _ORIG_OPEN(_ISOLATION_ENV, *args, **kwargs)
    if path == PROD_CLIENT_LOG:
        return _ORIG_OPEN(_ISOLATION_CLIENT_LOG, *args, **kwargs)
    return _ORIG_OPEN(file, *args, **kwargs)


def _guarded_makedirs(name, mode=0o777, exist_ok=False):
    path = _redirect_prod_path(str(name))
    return _ORIG_MAKEDIRS(path, mode, exist_ok=exist_ok)


def _file_fingerprint(path: str):
    try:
        st = os.stat(path)
        with open(path, 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        return (st.st_ino, st.st_size, st.st_mtime_ns, digest)
    except FileNotFoundError:
        return None


_PROD_FINGERPRINTS_BEFORE = {
    PROD_DB: _file_fingerprint(PROD_DB),
    PROD_ENV: _file_fingerprint(PROD_ENV),
}

_connect_patch = mock.patch('sqlite3.connect', _guarded_connect)
_open_patch = mock.patch('builtins.open', _guarded_open)
_makedirs_patch = mock.patch('os.makedirs', _guarded_makedirs)
_connect_patch.start()
_open_patch.start()
_makedirs_patch.start()

import app as app_module  # noqa: E402

_STRICT_GUARD = True


def _make_get_db(db_path: str):
    def get_db():
        if os.path.realpath(db_path) == os.path.realpath(PROD_DB):
            raise AssertionError('ledger tests must not connect to production database')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return get_db


def _init_ledger_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            category TEXT,
            note TEXT,
            date TEXT,
            author TEXT,
            meta TEXT,
            created_at DATETIME DEFAULT (datetime('now','+8 hours'))
        );
        CREATE TABLE IF NOT EXISTS ledger_budget (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT UNIQUE,
            amount REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


class LedgerRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prod_before = dict(_PROD_FINGERPRINTS_BEFORE)

    @classmethod
    def tearDownClass(cls):
        _connect_patch.stop()
        _open_patch.stop()
        _makedirs_patch.stop()
        shutil.rmtree(_ISOLATION_DIR, ignore_errors=True)
        for path, before in cls._prod_before.items():
            after = _file_fingerprint(path)
            if before is None:
                assert not os.path.exists(path), f'{path} must not be created by ledger tests'
            else:
                assert after == before, f'{path} was modified by ledger tests'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'ledger-test.db')
        _init_ledger_tables(self.db_path)
        self.get_db = _make_get_db(self.db_path)
        self.patcher = mock.patch.object(app_module, 'get_db', self.get_db)
        self.patcher.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_import_used_isolation_database_not_production(self):
        self.assertTrue(os.path.exists(_ISOLATION_DB))
        self.assertNotEqual(os.path.realpath(_ISOLATION_DB), os.path.realpath(PROD_DB))

    def test_strict_guard_blocks_sqlite_connect_production_db(self):
        with self.assertRaises(AssertionError):
            sqlite3.connect(PROD_DB)

    def _insert(self, amount=-12.0, date='2026-07-10', meta=None):
        conn = self.get_db()
        cur = conn.execute(
            'INSERT INTO ledger (amount, category, note, date, author, meta) VALUES (?,?,?,?,?,?)',
            (amount, '餐饮', '测试', date, 'both', meta),
        )
        conn.commit()
        lid = cur.lastrowid
        conn.close()
        return lid

    def test_patch_missing_returns_404(self):
        res = self.client.patch('/api/ledger/99999', json={'note': 'x'})
        self.assertEqual(res.status_code, 404)
        body = res.get_json()
        self.assertEqual(body.get('ok'), False)
        self.assertEqual(body.get('error'), 'ledger entry not found')

    def test_delete_missing_returns_404(self):
        res = self.client.delete('/api/ledger/99999')
        self.assertEqual(res.status_code, 404)
        body = res.get_json()
        self.assertEqual(body.get('ok'), False)
        self.assertEqual(body.get('error'), 'ledger entry not found')

    def test_patch_existing_ok(self):
        lid = self._insert()
        res = self.client.patch(f'/api/ledger/{lid}', json={'note': '改备注', 'amount': -20})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json().get('ok'), True)
        conn = self.get_db()
        row = conn.execute('SELECT note, amount FROM ledger WHERE id=?', (lid,)).fetchone()
        conn.close()
        self.assertEqual(row['note'], '改备注')
        self.assertEqual(row['amount'], -20)

    def test_delete_existing_ok(self):
        lid = self._insert()
        res = self.client.delete(f'/api/ledger/{lid}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json().get('ok'), True)
        conn = self.get_db()
        row = conn.execute('SELECT id FROM ledger WHERE id=?', (lid,)).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_post_rejects_zero_nan_inf(self):
        for bad in (0, 'NaN', 'Infinity', '-Infinity'):
            res = self.client.post('/api/ledger', json={
                'amount': bad, 'category': '餐饮', 'note': 'x', 'date': '2026-07-10',
            })
            self.assertEqual(res.status_code, 400, msg=f'expected 400 for {bad!r}')

    def test_budget_rejects_negative_nan_inf(self):
        for bad in (-1, 'NaN', 'Infinity'):
            res = self.client.post('/api/ledger/budget', json={'month': '2026-07', 'amount': bad})
            self.assertEqual(res.status_code, 400, msg=f'expected 400 for {bad!r}')

    def test_invalid_month_date_return_400(self):
        res = self.client.get('/api/ledger?month=2026-13')
        self.assertEqual(res.status_code, 400)
        res = self.client.get('/api/ledger/budget?month=202607')
        self.assertEqual(res.status_code, 400)
        res = self.client.post('/api/ledger', json={
            'amount': -10, 'date': '2026/07/10',
        })
        self.assertEqual(res.status_code, 400)
        res = self.client.post('/api/ledger/budget', json={'month': 'bad', 'amount': 100})
        self.assertEqual(res.status_code, 400)

    def test_valid_entry_and_budget_roundtrip(self):
        res = self.client.post('/api/ledger', json={
            'amount': -68,
            'category': '餐饮',
            'note': '小面',
            'date': '2026-07-10',
            'author': 'fy',
            'meta': {'who': 'fy', 'reason': '想吃', 'mem': '雨停以后'},
        })
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body.get('ok'))
        self.assertIsInstance(body.get('id'), int)
        lid = body['id']

        got = self.client.get('/api/ledger?month=2026-07')
        self.assertEqual(got.status_code, 200)
        records = got.get_json()['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['id'], lid)

        bset = self.client.post('/api/ledger/budget', json={'month': '2026-07', 'amount': 3000})
        self.assertEqual(bset.status_code, 200)
        self.assertTrue(bset.get_json().get('ok'))
        bget = self.client.get('/api/ledger/budget?month=2026-07')
        self.assertEqual(bget.status_code, 200)
        self.assertEqual(bget.get_json().get('amount'), 3000)

    def test_meta_whitelist_compatible(self):
        res = self.client.post('/api/ledger', json={
            'amount': -5,
            'date': '2026-07-11',
            'meta': {
                'who': 'both',
                'reason': '必需',
                'note': '细节',
                'mem': 'm',
                'read': 'r',
                'later': 'l',
                'evil': 'drop-me',
                'nested': {'x': 1},
            },
        })
        self.assertEqual(res.status_code, 200)
        lid = res.get_json()['id']
        conn = self.get_db()
        row = conn.execute('SELECT meta FROM ledger WHERE id=?', (lid,)).fetchone()
        conn.close()
        meta = json.loads(row['meta'])
        self.assertEqual(set(meta.keys()), {'who', 'reason', 'note', 'mem', 'read', 'later'})
        self.assertNotIn('evil', meta)


def _init_todo_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            due_date TEXT,
            author TEXT,
            created_at DATETIME DEFAULT (datetime('now','+8 hours'))
        )
        """
    )
    conn.commit()
    conn.close()


class TodoRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'todo-test.db')
        _init_todo_tables(self.db_path)
        self.get_db = _make_get_db(self.db_path)
        self.patcher = mock.patch.object(app_module, 'get_db', self.get_db)
        self.patcher.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _insert(self, content='待办', done=0, due_date=None, author='fyodor'):
        conn = self.get_db()
        cur = conn.execute(
            'INSERT INTO todos (content, done, due_date, author) VALUES (?,?,?,?)',
            (content, done, due_date, author),
        )
        conn.commit()
        todo_id = cur.lastrowid
        conn.close()
        return todo_id

    def test_get_post_roundtrip_and_order(self):
        response = self.client.post('/api/todos', json={
            'content': '有日期',
            'due_date': '2026-08-21',
            'author': 'fyodor',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})

        self._insert('无日期')
        listed = self.client.get('/api/todos')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [row['content'] for row in listed.get_json()['todos']],
            ['有日期', '无日期'],
        )

    def test_post_empty_returns_400_without_insert(self):
        response = self.client.post('/api/todos', json={'content': '  '})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'content required'})
        conn = self.get_db()
        count = conn.execute('SELECT COUNT(*) FROM todos').fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_patch_toggle_and_delete_persist(self):
        todo_id = self._insert()
        patched = self.client.patch(
            f'/api/todos/{todo_id}',
            json={'done': True},
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.get_json()['done'], 1)

        toggled = self.client.post(f'/api/todos/{todo_id}/toggle')
        self.assertEqual(toggled.status_code, 200)
        conn = self.get_db()
        row = conn.execute(
            'SELECT done FROM todos WHERE id=?', (todo_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row['done'], 0)

        deleted = self.client.delete(f'/api/todos/{todo_id}')
        self.assertEqual(deleted.status_code, 200)
        conn = self.get_db()
        remaining = conn.execute(
            'SELECT COUNT(*) FROM todos WHERE id=?', (todo_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 0)

    def test_patch_validation_and_missing_row(self):
        missing_done = self.client.patch('/api/todos/99999', json={})
        self.assertEqual(missing_done.status_code, 400)
        self.assertEqual(missing_done.get_json(), {'error': 'done required'})

        missing_row = self.client.patch(
            '/api/todos/99999',
            json={'done': True},
        )
        self.assertEqual(missing_row.status_code, 404)
        self.assertEqual(missing_row.get_json(), {'error': 'not found'})


if __name__ == '__main__':
    unittest.main()
