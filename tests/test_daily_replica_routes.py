"""Owner auth and explicit B gate tests for 9A-R HTTP routes."""
from __future__ import annotations

import unittest

from flask import Flask

from chat.daily_replica_ab import ReplicaContractError
from daily_replica_routes import create_daily_replica_blueprint


class FakeManager:
    def __init__(self):
        self.calls = []

    def start_a(self, *, user_message_id):
        self.calls.append(('a', user_message_id))
        return {'ok': True, 'variant': 'A'}

    def run_experiment_b(self, *, experiment_id, a_reproduction_confirmed):
        self.calls.append(('b', experiment_id, a_reproduction_confirmed))
        if not a_reproduction_confirmed:
            raise ReplicaContractError('gate', error_code='REPLICA_A_GATE_REQUIRED')
        return {'ok': True, 'variant': 'B'}

    def close(self, *, experiment_id):
        self.calls.append(('close', experiment_id))
        return {'ok': True, 'closed': True}


class DailyReplicaRouteTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        app = Flask(__name__)
        app.register_blueprint(create_daily_replica_blueprint(
            manager=self.manager, owner_token='secret',
        ))
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer secret'}

    def test_owner_auth_is_required(self):
        response = self.client.post('/api/debug/daily-replica/a', json={'user_message_id': 10})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.manager.calls, [])

    def test_a_accepts_only_an_existing_message_id_shape(self):
        response = self.client.post(
            '/api/debug/daily-replica/a', json={'user_message_id': 10},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.calls, [('a', 10)])

    def test_b_requires_literal_true_confirmation(self):
        response = self.client.post(
            '/api/debug/daily-replica/b',
            json={'experiment_id': 'exp-1', 'a_reproduced': 'yes'},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'REPLICA_A_GATE_REQUIRED')
        self.assertEqual(self.manager.calls[-1], ('b', 'exp-1', False))


if __name__ == '__main__':
    unittest.main()
