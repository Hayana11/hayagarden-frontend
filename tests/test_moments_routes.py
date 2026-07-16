import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from moments_routes import create_moments_blueprint


class MomentsRouteTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE posts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "author TEXT DEFAULT 'fyodor', "
            "created_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO posts (type, content, created_at) VALUES ('THOUGHT', '一条念头', '2026-07-16 09:00:00')"
        )
        conn.commit()
        conn.close()

        app = Flask(__name__)
        app.register_blueprint(create_moments_blueprint(memories_db_path=self.db_path))
        self.client = app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_feed_route_returns_items(self):
        response = self.client.get('/api/moments/feed?limit=10')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(payload['items'][0]['kind'], 'thought')
        self.assertEqual(payload['items'][0]['item_key'], 'thought:1')

    def test_invalid_limit_returns_400(self):
        response = self.client.get('/api/moments/feed?limit=0')
        self.assertEqual(response.status_code, 400)

    def test_invalid_cursor_returns_400(self):
        response = self.client.get('/api/moments/feed?cursor=broken')
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
