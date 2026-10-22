import datetime
import os
import sqlite3
import tempfile
import unittest

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

    def test_append_snapshot_writes_row(self):
        emotion_history.append_snapshot(0.72, 0.41, '愉悦', db_path=self.db_path, source='score')
        conn = sqlite3.connect(self.db_path)
        count = conn.execute('SELECT COUNT(*) FROM emotion_history').fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
