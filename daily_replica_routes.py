"""Owner-only HTTP routes for the one-shot 9A Daily replica."""
from __future__ import annotations

import hmac

from flask import Blueprint, jsonify, request

from chat.daily_replica_ab import ReplicaContractError


def create_daily_replica_blueprint(*, manager, owner_token: str) -> Blueprint:
    bp = Blueprint('daily_replica_debug', __name__)

    def _auth_error():
        expected = str(owner_token or '').strip()
        if not expected:
            return jsonify({'ok': False, 'error': 'owner token not configured'}), 503
        header = request.headers.get('Authorization', '')
        supplied = header[7:].strip() if header.startswith('Bearer ') else ''
        if not supplied or not hmac.compare_digest(supplied, expected):
            response = jsonify({'ok': False, 'error': 'unauthorized'})
            response.status_code = 401
            response.headers['WWW-Authenticate'] = 'Bearer'
            return response
        return None

    def _error(exc: ReplicaContractError):
        code = str(getattr(exc, 'error_code', '') or 'REPLICA_ERROR')
        status = 404 if code == 'REPLICA_NOT_FOUND' else 409
        if code in ('REPLICA_USER_MESSAGE_ID_INVALID', 'REPLICA_USER_MESSAGE_INVALID'):
            status = 400
        return jsonify({'ok': False, 'error': str(exc), 'code': code}), status

    @bp.route('/api/debug/daily-replica/a', methods=['POST'])
    def start_a():
        auth = _auth_error()
        if auth:
            return auth
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(manager.start_a(user_message_id=int(data.get('user_message_id') or 0)))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'user_message_id required'}), 400
        except ReplicaContractError as exc:
            return _error(exc)

    @bp.route('/api/debug/daily-replica/b', methods=['POST'])
    def run_b():
        auth = _auth_error()
        if auth:
            return auth
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(manager.run_experiment_b(
                experiment_id=str(data.get('experiment_id') or ''),
                a_reproduction_confirmed=data.get('a_reproduced') is True,
            ))
        except ReplicaContractError as exc:
            return _error(exc)

    @bp.route('/api/debug/daily-replica/close', methods=['POST'])
    def close():
        auth = _auth_error()
        if auth:
            return auth
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(manager.close(
                experiment_id=str(data.get('experiment_id') or ''),
            ))
        except ReplicaContractError as exc:
            return _error(exc)

    return bp
