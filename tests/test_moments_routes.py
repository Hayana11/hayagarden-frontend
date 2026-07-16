import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from moments_routes import create_moments_blueprint
import moments_store


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
        moments_store.ensure_schema(self.db_path)

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

    def test_posts_feed_type_route(self):
        response = self.client.get('/api/moments/feed?limit=10&type=posts')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(payload['items'][0]['kind'], 'thought')

    def test_collect_intent_route(self):
        import moments_turn
        moments_turn.ensure_turn_schema(self.db_path)
        turn = moments_turn.begin_turn({}, conversation_id='hayana-chat', memories_db_path=self.db_path)
        response = self.client.post('/api/moments/collect-intent', json={
            'turn_key': turn['turn_key'],
            'previous_turns': 0,
            'caption': '测试',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])

    def test_react_route_toggles_like(self):
        response = self.client.post(
            '/api/moments/react',
            json={'item_key': 'thought:1', 'reaction': 'like'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['social']['likes'], 1)

        response = self.client.post(
            '/api/moments/react',
            json={'item_key': 'thought:1', 'reaction': 'like'},
        )
        self.assertEqual(response.get_json()['social']['likes'], 0)

    def test_comments_route_create_and_list(self):
        create = self.client.post(
            '/api/moments/comments',
            json={'item_key': 'thought:1', 'content': '好'},
        )
        self.assertEqual(create.status_code, 200)
        self.assertEqual(create.get_json()['social']['comments'], 1)

        listing = self.client.get('/api/moments/comments?item_key=thought:1')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.get_json()['items']), 1)
        self.assertEqual(listing.get_json()['items'][0]['content'], '好')

    def test_react_missing_item_returns_404(self):
        response = self.client.post(
            '/api/moments/react',
            json={'item_key': 'thought:999999', 'reaction': 'like'},
        )
        self.assertEqual(response.status_code, 404)

    def test_comment_missing_item_returns_404(self):
        response = self.client.post(
            '/api/moments/comments',
            json={'item_key': 'thought:999999', 'content': '不存在'},
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_limit_returns_400(self):
        response = self.client.get('/api/moments/feed?limit=0')
        self.assertEqual(response.status_code, 400)

    def test_invalid_cursor_returns_400(self):
        response = self.client.get('/api/moments/feed?cursor=broken')
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
