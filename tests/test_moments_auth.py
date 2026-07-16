import os
import unittest
from unittest import mock

from flask import Flask

from moments_auth import (
    OwnerAuthError,
    apply_owner_cookie,
    is_owner_authenticated,
    owner_session_digest,
    owner_token_configured,
    require_owner,
    verify_owner_token,
)
from moments_routes import create_moments_blueprint


class MomentsAuthTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {'MOMENTS_OWNER_TOKEN': 'test-owner-token'}, clear=False)
        self._env.start()
        import moments_auth
        moments_auth._get_owner_token = moments_auth.owner_token_getter()

    def tearDown(self):
        self._env.stop()

    def test_verify_owner_token(self):
        self.assertTrue(verify_owner_token('test-owner-token'))
        self.assertFalse(verify_owner_token('wrong'))

    def test_require_owner_accepts_bearer(self):
        with Flask(__name__).test_request_context(
            headers={'Authorization': 'Bearer test-owner-token'},
        ):
            require_owner(__import__('flask').request)

    def test_require_owner_rejects_missing(self):
        with Flask(__name__).test_request_context():
            with self.assertRaises(OwnerAuthError) as ctx:
                require_owner(__import__('flask').request)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_owner_cookie_authenticates(self):
        app = Flask(__name__)
        with app.test_request_context():
            from flask import request
            digest = owner_session_digest('test-owner-token')
            with app.test_request_context('/', headers={'Cookie': f'moments_owner={digest}'}):
                self.assertTrue(is_owner_authenticated(request))

    def test_session_route_sets_cookie(self):
        app = Flask(__name__)
        app.register_blueprint(create_moments_blueprint(memories_db_path=':memory:'))
        client = app.test_client()
        response = client.post('/api/moments/session', json={'token': 'test-owner-token'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('moments_owner', response.headers.get('Set-Cookie', ''))

    def test_write_routes_require_owner(self):
        app = Flask(__name__)
        app.register_blueprint(create_moments_blueprint(memories_db_path=':memory:'))
        client = app.test_client()
        self.assertEqual(client.post('/api/moments/react', json={'item_key': 'thought:1', 'reaction': 'like'}).status_code, 401)


if __name__ == '__main__':
    unittest.main()
