"""Period tracker: cycle starts, type compat, migration idempotency, date validation.

Uses a temporary SQLite database — never touches /opt/frontend/memories.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import period_logic as period  # noqa: E402


class PeriodLogicTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.db_path = handle.name
        handle.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        period.migrate_period_compat(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def _period_dates(self):
        rows = self.conn.execute(
            "SELECT date FROM period_records WHERE type='period' ORDER BY date"
        ).fetchall()
        return [r['date'] for r in rows]

    def _insert_legacy(self, date, rtype, note=''):
        self.conn.execute(
            "INSERT INTO period_records (date, type, note) VALUES (?,?,?)",
            (date, rtype, note),
        )

    def test_five_consecutive_bleeding_days_one_cycle_start(self):
        for d in ['2026-06-01', '2026-06-02', '2026-06-03', '2026-06-04', '2026-06-05']:
            period.put_period_day(self.conn, d, {'came': True})
        self.conn.commit()
        self.assertEqual(self._period_dates(), ['2026-06-01'])
        stats = period.derive_cycle_stats(self.conn)
        self.assertEqual(stats['last_period'], '2026-06-01')

    def test_average_cycle_28_from_group_starts(self):
        for d in ['2026-06-01', '2026-06-02', '2026-06-03', '2026-06-04', '2026-06-05']:
            period.put_period_day(self.conn, d, {'came': True})
        for d in ['2026-06-29', '2026-06-30', '2026-07-01', '2026-07-02', '2026-07-03']:
            period.put_period_day(self.conn, d, {'came': True})
        self.conn.commit()
        stats = period.derive_cycle_stats(self.conn)
        self.assertEqual(stats['last_period'], '2026-06-29')
        self.assertEqual(stats['cycle_length'], 28)
        self.assertEqual(self._period_dates(), ['2026-06-01', '2026-06-29'])

    def test_longer_second_period_does_not_shorten_cycle(self):
        # Round 1: 5 days; Round 2 starts 28 days later but lasts 7 days.
        for d in ['2026-06-01', '2026-06-02', '2026-06-03', '2026-06-04', '2026-06-05']:
            period.put_period_day(self.conn, d, {'came': True})
        for d in [
            '2026-06-29', '2026-06-30', '2026-07-01', '2026-07-02',
            '2026-07-03', '2026-07-04', '2026-07-05',
        ]:
            period.put_period_day(self.conn, d, {'came': True})
        self.conn.commit()
        stats = period.derive_cycle_stats(self.conn)
        self.assertEqual(stats['cycle_length'], 28)
        self.assertEqual(stats['last_period'], '2026-06-29')
        # Must NOT be 24 (Jun 5 -> Jun 29).
        self.assertNotEqual(stats['cycle_length'], 24)

    def test_start_type_is_cycle_start(self):
        self._insert_legacy('2026-05-10', 'start', 'chat')
        self.conn.commit()
        # Trigger should mirror type=period immediately.
        self.assertEqual(self._period_dates(), ['2026-05-10'])
        period.rebuild_period_start_markers(self.conn)
        self.conn.commit()
        stats = period.derive_cycle_stats(self.conn)
        self.assertEqual(stats['last_period'], '2026-05-10')
        days = period.merge_legacy_into_days(
            self.conn.execute('SELECT date,data FROM period_days').fetchall(),
            self.conn.execute('SELECT date,type FROM period_records').fetchall(),
        )
        self.assertTrue(days['2026-05-10']['came'])

    def test_end_is_not_intimacy_or_cycle_start(self):
        self._insert_legacy('2026-05-10', 'start')
        self._insert_legacy('2026-05-15', 'end')
        self.conn.commit()
        period.rebuild_period_start_markers(self.conn)
        self.conn.commit()
        self.assertEqual(self._period_dates(), ['2026-05-10'])
        stats = period.derive_cycle_stats(self.conn)
        self.assertEqual(stats['last_period'], '2026-05-10')
        days = period.merge_legacy_into_days(
            [],
            self.conn.execute('SELECT date,type FROM period_records').fetchall(),
        )
        self.assertNotIn('2026-05-15', days)
        self.assertEqual(period.record_type_label('end'), '经期结束')
        self.assertNotEqual(period.record_type_label('end'), '亲密')

    def test_sex_preserved_through_migration(self):
        self._insert_legacy('2026-06-01', 'period')
        self._insert_legacy('2026-06-02', 'period')
        self._insert_legacy('2026-06-03', 'period')
        self._insert_legacy('2026-06-10', 'sex', 'keep me')
        self.conn.commit()
        period.migrate_period_compat(self.conn)
        self.conn.commit()
        sex = self.conn.execute(
            "SELECT date, note FROM period_records WHERE type='sex'"
        ).fetchall()
        self.assertEqual(len(sex), 1)
        self.assertEqual(sex[0]['date'], '2026-06-10')
        self.assertEqual(sex[0]['note'], 'keep me')
        self.assertEqual(self._period_dates(), ['2026-06-01'])

    def test_duplicate_same_day_does_not_skew_stats(self):
        self._insert_legacy('2026-06-01', 'period')
        self._insert_legacy('2026-06-01', 'period')
        self._insert_legacy('2026-06-29', 'period')
        self.conn.commit()
        period.rebuild_period_start_markers(self.conn)
        self.conn.commit()
        self.assertEqual(self._period_dates(), ['2026-06-01', '2026-06-29'])
        self.assertEqual(period.derive_cycle_stats(self.conn)['cycle_length'], 28)

    def test_invalid_date_rejected_by_put(self):
        with self.assertRaises(ValueError):
            period.put_period_day(self.conn, '2026-13-40', {'came': True})
        with self.assertRaises(ValueError):
            period.put_period_day(self.conn, 'not-a-date', {'came': True})
        self.assertFalse(period.is_valid_ymd('2026-02-30'))
        self.assertFalse(period.is_valid_ymd('2026/06/01'))

    def test_dirty_history_skipped_in_stats(self):
        self._insert_legacy('2026-06-01', 'period')
        self._insert_legacy('bogus', 'period')
        self._insert_legacy('2026-02-30', 'period')
        self._insert_legacy('2026-06-29', 'period')
        self.conn.commit()
        stats = period.derive_cycle_stats(self.conn)
        self.assertEqual(stats['last_period'], '2026-06-29')
        self.assertEqual(stats['cycle_length'], 28)

    def test_migration_idempotent(self):
        for d in ['2026-06-01', '2026-06-02', '2026-06-03']:
            self._insert_legacy(d, 'period')
        self._insert_legacy('2026-06-10', 'sex')
        self.conn.commit()
        a = period.migrate_period_compat(self.conn)
        self.conn.commit()
        snap = {
            'period': self._period_dates(),
            'sex': [r['date'] for r in self.conn.execute(
                "SELECT date FROM period_records WHERE type='sex' ORDER BY date"
            )],
            'days': [r['date'] for r in self.conn.execute(
                "SELECT date FROM period_days ORDER BY date"
            )],
        }
        b = period.migrate_period_compat(self.conn)
        self.conn.commit()
        snap2 = {
            'period': self._period_dates(),
            'sex': [r['date'] for r in self.conn.execute(
                "SELECT date FROM period_records WHERE type='sex' ORDER BY date"
            )],
            'days': [r['date'] for r in self.conn.execute(
                "SELECT date FROM period_days ORDER BY date"
            )],
        }
        self.assertEqual(snap, snap2)
        self.assertEqual(a['starts'], b['starts'])

    def test_new_page_multi_day_leaves_only_starts_in_period_type(self):
        for d in ['2026-06-01', '2026-06-02', '2026-06-03', '2026-06-04', '2026-06-05']:
            period.put_period_day(self.conn, d, {'came': True, 'flow': '中等'})
        for d in ['2026-06-29', '2026-06-30', '2026-07-01']:
            period.put_period_day(self.conn, d, {'came': True})
        self.conn.commit()
        # Unmodified chat query shape:
        rows = self.conn.execute(
            "SELECT date FROM period_records WHERE type='period' ORDER BY date"
        ).fetchall()
        self.assertEqual([r['date'] for r in rows], ['2026-06-01', '2026-06-29'])

    def test_chat_start_visible_to_legacy_period_query(self):
        # Simulate gateway INSERT without going through our PUT API.
        self.conn.execute(
            "INSERT INTO period_records (type, date, note) VALUES (?, ?, ?)",
            ('start', '2026-07-20', 'from chat'),
        )
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT date FROM period_records WHERE type='period' ORDER BY date"
        ).fetchall()
        self.assertEqual([r['date'] for r in rows], ['2026-07-20'])

    def test_unknown_type_not_intimacy(self):
        self.assertEqual(period.record_type_label('weird'), '记录')
        self.assertEqual(period.record_type_label('sex'), '亲密')
        days = period.merge_legacy_into_days(
            [],
            [{'date': '2026-07-01', 'type': 'weird'}],
        )
        self.assertEqual(days, {})


class PeriodRouteValidationTests(unittest.TestCase):
    """HTTP-level checks against a mini Flask app wired to a temp DB."""

    def setUp(self):
        from flask import Flask, jsonify, request

        handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.db_path = handle.name
        handle.close()

        def get_db():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        conn = get_db()
        period.migrate_period_compat(conn)
        conn.commit()
        conn.close()

        app = Flask(__name__)

        @app.route('/api/period/day', methods=['PUT', 'POST'])
        def put_day():
            data = request.get_json() or {}
            date = (data.get('date') or '').strip()
            record = data.get('record')
            if not period.is_valid_ymd(date) or not isinstance(record, dict):
                return jsonify({'error': 'invalid date or record'}), 400
            c = get_db()
            try:
                cleaned = period.put_period_day(c, date, record)
                c.commit()
                return jsonify({'ok': True, 'date': date, 'record': cleaned})
            finally:
                c.close()

        @app.route('/api/period/records', methods=['POST'])
        def add_rec():
            data = request.get_json() or {}
            date = (data.get('date') or '').strip()
            rtype = (data.get('type') or '').strip()
            if not period.is_valid_ymd(date) or rtype not in ('period', 'sex'):
                return jsonify({'error': 'invalid date or type'}), 400
            return jsonify({'ok': True})

        @app.route('/api/period/settings', methods=['PUT', 'POST'])
        def put_settings():
            data = request.get_json() or {}
            ls = (data.get('last_start') or '').strip() if isinstance(data.get('last_start'), str) else ''
            if ls and not period.is_valid_ymd(ls):
                return jsonify({'error': 'invalid last_start date'}), 400
            return jsonify({'ok': True})

        @app.route('/api/period/stats', methods=['GET'])
        def stats():
            c = get_db()
            try:
                # Inject a dirty row, then ensure stats still returns 200.
                c.execute(
                    "INSERT INTO period_records (date, type) VALUES ('nope', 'period')"
                )
                c.commit()
                s = period.derive_cycle_stats(c)
                return jsonify({
                    'last_period': s['last_period'],
                    'cycle_length': s['cycle_length'],
                    'next_period': s['next_period'],
                    'ovulation': s['ovulation'],
                })
            finally:
                c.close()

        self.client = app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_day_invalid_date_400(self):
        r = self.client.put(
            '/api/period/day',
            json={'date': '2026-02-30', 'record': {'came': True}},
        )
        self.assertEqual(r.status_code, 400)

    def test_records_invalid_date_400(self):
        r = self.client.post(
            '/api/period/records',
            json={'date': 'bad', 'type': 'period'},
        )
        self.assertEqual(r.status_code, 400)

    def test_settings_invalid_last_start_400(self):
        r = self.client.put(
            '/api/period/settings',
            json={'last_start': '2026-13-01'},
        )
        self.assertEqual(r.status_code, 400)

    def test_stats_dirty_date_not_500(self):
        r = self.client.get('/api/period/stats')
        self.assertEqual(r.status_code, 200)
        self.assertIn('last_period', r.get_json())


if __name__ == '__main__':
    unittest.main()
