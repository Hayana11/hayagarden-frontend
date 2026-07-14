"""Flask routes for encrypted per-relay console credentials and account balance."""

from __future__ import annotations

import re
from typing import Callable

from flask import Blueprint, jsonify, request


def create_relay_account_blueprint(
    *,
    get_db: Callable,
    ensure_tables: Callable,
    normalize_credential: Callable | None = None,
    encrypt_secret: Callable | None = None,
    decrypt_secret: Callable | None = None,
    query_balance: Callable | None = None,
    validation_error: type[Exception] | None = None,
    vault_error: type[Exception] | None = None,
) -> Blueprint:
    """Build the routes with injectable dependencies so secret handling is integration-testable."""
    if any(value is None for value in (
        normalize_credential,
        encrypt_secret,
        decrypt_secret,
        query_balance,
        validation_error,
        vault_error,
    )):
        from relay.channel_intelligence import (
            ChannelInspectionError,
            normalize_console_credential,
            query_channel_account_balance,
        )
        from relay.credential_vault import (
            CredentialVaultError,
            decrypt_secret as vault_decrypt,
            encrypt_secret as vault_encrypt,
        )
        normalize_credential = normalize_console_credential
        encrypt_secret = vault_encrypt
        decrypt_secret = vault_decrypt
        query_balance = query_channel_account_balance
        validation_error = ChannelInspectionError
        vault_error = CredentialVaultError

    blueprint = Blueprint('relay_account_balance', __name__)

    def relay_exists(preset_id: int) -> bool:
        conn = get_db()
        try:
            return conn.execute('SELECT 1 FROM relay_presets WHERE id=?', (preset_id,)).fetchone() is not None
        finally:
            conn.close()

    @blueprint.route('/api/config/relay-presets/<int:preset_id>/account-credentials', methods=['PUT'])
    def save_relay_preset_account_credentials(preset_id):
        """Encrypt and save one relay's revocable console credential without returning it."""
        if request.content_length and request.content_length > 8192:
            return jsonify({'ok': False, 'error': '请求内容过大'}), 413
        ensure_tables()
        if not relay_exists(preset_id):
            return jsonify({'error': 'not found'}), 404

        data = request.get_json(silent=True) or {}
        user_id = str(data.get('user_id') or '').strip()
        credential_kind = str(data.get('credential_kind') or '').strip()
        credential_secret = str(data.get('credential_secret') or '')
        try:
            normalized_kind, normalized_secret = normalize_credential(
                credential_kind,
                credential_secret,
            )
            if not re.fullmatch(r'[1-9]\d{0,18}', user_id):
                raise validation_error('New-Api-User 必须是数字用户 ID')
            ciphertext = encrypt_secret(normalized_secret)
            conn = get_db()
            try:
                conn.execute('''
                    INSERT INTO relay_account_credentials
                        (preset_id,user_id,credential_kind,secret_ciphertext)
                    VALUES (?,?,?,?)
                    ON CONFLICT(preset_id) DO UPDATE SET
                        user_id=excluded.user_id,
                        credential_kind=excluded.credential_kind,
                        secret_ciphertext=excluded.secret_ciphertext,
                        updated_at=datetime('now','+8 hours')
                ''', (preset_id, user_id, normalized_kind, ciphertext))
                conn.commit()
            finally:
                conn.close()
            return jsonify({'ok': True, 'configured': True, 'credential_kind': normalized_kind})
        except validation_error as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except vault_error as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 503
        except Exception:
            return jsonify({'ok': False, 'error': '控制台凭据保存失败'}), 500

    @blueprint.route('/api/config/relay-presets/<int:preset_id>/account-credentials', methods=['DELETE'])
    def delete_relay_preset_account_credentials(preset_id):
        ensure_tables()
        if not relay_exists(preset_id):
            return jsonify({'error': 'not found'}), 404
        conn = get_db()
        try:
            conn.execute('DELETE FROM relay_account_credentials WHERE preset_id=?', (preset_id,))
            conn.commit()
        finally:
            conn.close()
        return jsonify({'ok': True, 'configured': False})

    @blueprint.route('/api/config/relay-presets/<int:preset_id>/account-balance', methods=['GET'])
    def get_relay_preset_account_balance(preset_id):
        """Decrypt one saved console credential in memory and query the relay account balance."""
        ensure_tables()
        conn = get_db()
        try:
            row = conn.execute(
                '''
                SELECT r.id,r.name,r.url,c.user_id,c.credential_kind,c.secret_ciphertext
                FROM relay_presets r
                LEFT JOIN relay_account_credentials c ON c.preset_id=r.id
                WHERE r.id=?
                ''',
                (preset_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({'error': 'not found'}), 404
        if not row['secret_ciphertext']:
            return jsonify({'ok': True, 'balance': {
                'supported': False,
                'error': 'missing_credentials',
                'source': '',
            }})

        try:
            credential_secret = decrypt_secret(row['secret_ciphertext'])
            balance = query_balance(
                {
                    'id': row['id'],
                    'name': row['name'],
                    'base_url': row['url'],
                },
                credential_kind=row['credential_kind'],
                credential_secret=credential_secret,
                user_id=row['user_id'],
            )
            return jsonify({'ok': True, 'balance': balance})
        except validation_error as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 502
        except vault_error as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 503
        except Exception:
            return jsonify({'ok': False, 'error': '账户余额查询失败'}), 500

    return blueprint
