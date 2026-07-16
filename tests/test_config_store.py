import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import config_store


class ConfigStoreMutateTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE runtime_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
        conn.close()
        self._db_patch = mock.patch.object(config_store, 'DB_PATH', self.db_path)
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        os.unlink(self.db_path)

    def test_mutate_serializes_concurrent_appends(self):
        key = 'TEST_COUNTER'
        barrier = threading.Barrier(3)
        errors = []

        def bump():
            try:
                barrier.wait(timeout=5)
                config_store.mutate(key, '0', lambda raw: str(int(raw or '0') + 1))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=bump) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(config_store.get(key), '3')


if __name__ == '__main__':
    unittest.main()
