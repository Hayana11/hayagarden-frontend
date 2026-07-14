import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from account_balance_routes import create_relay_account_blueprint


class ValidationError(RuntimeError):
    pass


class VaultError(RuntimeError):
    pass


class AccountBalanceRouteTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.db_path = handle.name
        handle.close()
        self.seen_query = {}

        def get_db():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        self.get_db = get_db
        self.ensure_tables()
        conn = self.get_db()
        conn.execute(
            'INSERT INTO relay_presets (id,name,url) VALUES (?,?,?)',
            (1, 'tree', 'https://tree.example.com/v1/messages'),
        )
        conn.commit()
        conn.close()

        def query_balance(channel, **kwargs):
            self.seen_query.update({'channel': channel, **kwargs})
            return {
                'supported': True,
                'error': None,
                'source': 'https://tree.example.com/api/user/self',
                'credential_kind': kwargs['credential_kind'],
                'remaining_usd': 23.5,
                'used_usd': 2.0,
                'total_usd': 25.5,
            }

        app = Flask(__name__)
        app.register_blueprint(create_relay_account_blueprint(
            get_db=self.get_db,
            ensure_tables=self.ensure_tables,
            normalize_credential=lambda kind, secret: (kind, secret.strip()),
            encrypt_secret=lambda secret: 'ciphertext-only',
            decrypt_secret=lambda ciphertext: 'console-secret',
            query_balance=query_balance,
            validation_error=ValidationError,
            vault_error=VaultError,
        ))
        self.client = app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)

    def ensure_tables(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS relay_presets (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relay_account_credentials (
                preset_id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                credential_kind TEXT NOT NULL,
                secret_ciphertext TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        conn.commit()
        conn.close()

    def test_put_and_get_never_return_plaintext_or_ciphertext(self):
        saved = self.client.put('/api/config/relay-presets/1/account-credentials', json={
            'user_id': '27',
            'credential_kind': 'access_token',
            'credential_secret': 'console-secret',
        })
        saved_text = saved.get_data(as_text=True)
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn('console-secret', saved_text)
        self.assertNotIn('ciphertext-only', saved_text)

        conn = self.get_db()
        row = conn.execute(
            'SELECT user_id,credential_kind,secret_ciphertext FROM relay_account_credentials WHERE preset_id=1'
        ).fetchone()
        conn.close()
        self.assertEqual(dict(row), {
            'user_id': '27',
            'credential_kind': 'access_token',
            'secret_ciphertext': 'ciphertext-only',
        })

        balance = self.client.get('/api/config/relay-presets/1/account-balance')
        balance_text = balance.get_data(as_text=True)
        self.assertEqual(balance.status_code, 200)
        self.assertNotIn('console-secret', balance_text)
        self.assertNotIn('ciphertext-only', balance_text)
        self.assertEqual(balance.get_json()['balance']['remaining_usd'], 23.5)
        self.assertEqual(self.seen_query['credential_secret'], 'console-secret')

    def test_put_rejects_body_over_8192_bytes_before_parsing(self):
        response = self.client.put(
            '/api/config/relay-presets/1/account-credentials',
            data='x' * 8193,
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 413)

    def test_all_routes_return_404_for_missing_relay(self):
        put_response = self.client.put('/api/config/relay-presets/999/account-credentials', json={
            'user_id': '27',
            'credential_kind': 'access_token',
            'credential_secret': 'console-secret',
        })
        get_response = self.client.get('/api/config/relay-presets/999/account-balance')
        delete_response = self.client.delete('/api/config/relay-presets/999/account-credentials')

        self.assertEqual(put_response.status_code, 404)
        self.assertEqual(get_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)
        for response in (put_response, get_response, delete_response):
            self.assertEqual(response.get_json()['error'], 'not found')

    def test_delete_removes_saved_ciphertext(self):
        self.client.put('/api/config/relay-presets/1/account-credentials', json={
            'user_id': '27',
            'credential_kind': 'access_token',
            'credential_secret': 'console-secret',
        })
        response = self.client.delete('/api/config/relay-presets/1/account-credentials')
        self.assertEqual(response.status_code, 200)
        conn = self.get_db()
        count = conn.execute('SELECT COUNT(*) FROM relay_account_credentials').fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
