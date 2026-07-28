"""Ledger API reliability: 404 on missing rows, amount/budget/date validation.

Uses a temporary SQLite DB and monkeypatches app.get_db — never the production DB.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _chat_schema(conn: sqlite3.Connection) -> None:
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


def _ensure_app_importable() -> None:
    db = '/opt/frontend/memories.db'
    os.makedirs(os.path.dirname(db), exist_ok=True)
    open('/opt/frontend/.env', 'a').close()
    conn = sqlite3.connect(db)
    _chat_schema(conn)
    conn.commit()
    conn.close()


_ensure_app_importable()
import app as app_module  # noqa: E402


def _make_get_db(db_path: str):
    def get_db():
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
        import json
        meta = json.loads(row['meta'])
        self.assertEqual(set(meta.keys()), {'who', 'reason', 'note', 'mem', 'read', 'later'})
        self.assertNotIn('evil', meta)


if __name__ == '__main__':
    unittest.main()
