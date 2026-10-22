import datetime
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import emotion_history
from valence_scale import normalize_valence


class EmotionHistoryTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        emotion_history.ensure_schema(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_append_and_fetch_daily_series(self):
        today = datetime.datetime.utcnow() + emotion_history.SHANGHAI_OFFSET
        day1 = (today - datetime.timedelta(days=3)).strftime('%Y-%m-%d')
        day2 = (today - datetime.timedelta(days=2)).strftime('%Y-%m-%d')
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            """
            INSERT INTO emotion_history (recorded_at, valence, arousal, mood_word, source)
            VALUES (?, ?, ?, ?, 'score')
            """,
            [
                (f'{day1} 09:00:00', 0.2, 0.3, '平静'),
                (f'{day1} 21:00:00', 0.8, 0.5, '愉悦'),
                (f'{day2} 10:00:00', 0.6, 0.4, '松弛'),
            ],
        )
        conn.commit()
        conn.close()

        series = emotion_history.fetch_series(30, db_path=self.db_path)
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0]['day'], day1)
        self.assertAlmostEqual(series[0]['valence'], normalize_valence(0.5, scale='unipolar'), places=3)
        self.assertEqual(series[0]['samples'], 2)
        self.assertEqual(series[1]['day'], day2)

    def test_old_table_gains_event_key_and_keyed_points_dedupe(self):
        os.unlink(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE emotion_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "recorded_at TEXT NOT NULL, valence REAL NOT NULL, arousal REAL NOT NULL, "
            "mood_word TEXT, source TEXT DEFAULT 'score')"
        )
        conn.execute(
            "INSERT INTO emotion_history (recorded_at, valence, arousal, mood_word) "
            "VALUES ('2026-07-10 09:00:00', 0.2, 0.3, '旧')"
        )
        conn.commit()
        conn.close()
        for _ in range(2):
            emotion_history.append_snapshot(
                0.7, 0.4, '新', db_path=self.db_path, source='v3_affect',
                source_event_key='user_scored:42',
            )
        emotion_history.append_snapshot(0.5, 0.5, '无键', db_path=self.db_path)
        emotion_history.append_snapshot(0.5, 0.5, '无键', db_path=self.db_path)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            'SELECT mood_word, source_event_key FROM emotion_history ORDER BY id'
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [('旧', None), ('新', 'user_scored:42'),
                                ('无键', None), ('无键', None)])

    def test_concurrent_upgrade_of_old_table_adds_column_once(self):
        os.unlink(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE emotion_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "recorded_at TEXT NOT NULL, valence REAL NOT NULL, arousal REAL NOT NULL, "
            "mood_word TEXT, source TEXT DEFAULT 'score')"
        )
        conn.commit()
        conn.close()

        # Both upgraders pause right after reading the schema, so without
        # serialisation each sees the old table and both run ALTER TABLE.
        barrier = threading.Barrier(2, timeout=1.0)

        class PausingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                result = super().execute(sql, *args)
                if sql.startswith('PRAGMA table_info'):
                    rows = result.fetchall()
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
                    return iter(rows)
                return result

        real_connect = sqlite3.connect

        def pausing_connect(*args, **kwargs):
            kwargs['factory'] = PausingConnection
            return real_connect(*args, **kwargs)

        errors = []

        def upgrade():
            try:
                emotion_history.ensure_schema(self.db_path)
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion
                errors.append(exc)

        with mock.patch.object(emotion_history.sqlite3, 'connect', side_effect=pausing_connect):
            threads = [threading.Thread(target=upgrade) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        conn = sqlite3.connect(self.db_path)
        cols = [r[1] for r in conn.execute('PRAGMA table_info(emotion_history)')]
        conn.close()
        self.assertEqual(cols.count('source_event_key'), 1)

    def test_fetch_series_reconciles_from_event_ledger(self):
        import json
        today = (datetime.datetime.utcnow() + emotion_history.SHANGHAI_OFFSET).strftime('%Y-%m-%d')
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE internal_state_events (id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, "
            "event_type TEXT, status TEXT, applied_at TEXT, result_json TEXT)"
        )
        rows = [
            ('user_scored:1', 'user_scored', 'applied', f'{today} 10:00:01',
             json.dumps({'settled_affect': {'valence': 0.9, 'arousal': 0.2,
                                            'mood_word': '甜', 'scored_at': f'{today} 10:00:00'}})),
            ('user_scored:2', 'user_scored', 'stale_skipped', f'{today} 10:05:01', None),
            ('user_rule:3', 'user_rule', 'applied', f'{today} 10:06:01',
             json.dumps({'settled_affect': {'valence': 0.1, 'arousal': 0.1}})),
        ]
        conn.executemany(
            'INSERT INTO internal_state_events (event_key, event_type, status, applied_at, result_json) '
            'VALUES (?, ?, ?, ?, ?)', rows,
        )
        conn.commit()
        conn.close()
        for _ in range(2):
            series = emotion_history.fetch_series(7, db_path=self.db_path)
        self.assertEqual([(p['day'], p['mood_word'], p['samples']) for p in series],
                         [(today, '甜', 1)])

    def test_append_snapshot_writes_row(self):
        emotion_history.append_snapshot(0.72, 0.41, '愉悦', db_path=self.db_path, source='score')
        conn = sqlite3.connect(self.db_path)
        count = conn.execute('SELECT COUNT(*) FROM emotion_history').fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
