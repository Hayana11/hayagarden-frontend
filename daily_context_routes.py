"""HTTP routes for Daily Soft Window (default disabled)."""
from __future__ import annotations

import hmac
import os
from typing import Any

from flask import Blueprint, jsonify, request

daily_context_bp = Blueprint('daily_context', __name__)

ENV_PATH = os.environ.get('HAYAGARDEN_ENV_PATH', '/opt/frontend/.env')


def _load_token() -> str:
    token = os.environ.get('DAILY_SOFT_WINDOW_TOKEN', '').strip()
    if token:
        return token
    try:
        with open(ENV_PATH, encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('DAILY_SOFT_WINDOW_TOKEN='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    # Fall back to moments owner token for internal use.
    token = os.environ.get('MOMENTS_OWNER_TOKEN', '').strip()
    if token:
        return token
    try:
        with open(ENV_PATH, encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('MOMENTS_OWNER_TOKEN='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return ''


def _auth_error():
    from chat.daily_context import enabled
    if not enabled():
        return jsonify({'ok': False, 'error': 'disabled'}), 404
    expected = _load_token()
    if not expected:
        return jsonify({'ok': False, 'error': 'daily soft window token not configured'}), 503
    header = request.headers.get('Authorization', '')
    supplied = header[7:].strip() if header.startswith('Bearer ') else ''
    if not supplied or not hmac.compare_digest(supplied, expected):
        response = jsonify({'ok': False, 'error': 'unauthorized'})
        response.status_code = 401
        response.headers['WWW-Authenticate'] = 'Bearer'
        return response
    return None


def _db_path_override():
    return request.args.get('db_path') or None


@daily_context_bp.route('/api/daily-context/current', methods=['GET'])
def daily_context_current():
    err = _auth_error()
    if err:
        return err
    from chat.daily_context import DEFAULT_CHAT_ID, current_summary

    chat_id = str(request.args.get('chat_id') or DEFAULT_CHAT_ID)
    try:
        data = current_summary(chat_id=chat_id, db_path=_db_path_override())
        return jsonify({'ok': True, **data})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@daily_context_bp.route('/api/daily-context/carryover-candidates', methods=['GET'])
def daily_context_carryover_candidates():
    err = _auth_error()
    if err:
        return err
    from chat.daily_context import (
        DEFAULT_CHAT_ID,
        get_or_create_daily_context,
        list_carryover_candidates,
    )

    chat_id = str(request.args.get('chat_id') or DEFAULT_CHAT_ID)
    try:
        ctx = get_or_create_daily_context(chat_id=chat_id, db_path=_db_path_override())
        items = list_carryover_candidates(int(ctx['id']), limit=10, db_path=_db_path_override())
        # Strip author internal fields beyond role/preview.
        safe = [{
            'message_id': i['message_id'],
            'role': i['role'],
            'content_preview': i['content_preview'],
            'created_at': i['created_at'],
        } for i in items]
        return jsonify({
            'ok': True,
            'context_id': int(ctx['id']),
            'context_epoch': int(ctx['context_epoch']),
            'candidates': safe,
        })
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@daily_context_bp.route('/api/daily-context/select-carryover', methods=['POST'])
def daily_context_select_carryover():
    err = _auth_error()
    if err:
        return err
    from chat.daily_context import (
        ConflictError,
        DEFAULT_CHAT_ID,
        get_or_create_daily_context,
        select_carryover,
    )

    data = request.get_json(silent=True) or {}
    chat_id = str(data.get('chat_id') or request.args.get('chat_id') or DEFAULT_CHAT_ID)
    try:
        count = int(data.get('count'))
    except Exception:
        return jsonify({'ok': False, 'error': 'count must be 0|3|5|10'}), 400
    try:
        ctx = get_or_create_daily_context(chat_id=chat_id, db_path=_db_path_override())
        result = select_carryover(int(ctx['id']), count, db_path=_db_path_override())
        return jsonify({
            'ok': True,
            'selected_message_ids': result['selected_message_ids'],
            'finalized_at': result['finalized_at'],
            'context_epoch': result['context_epoch'],
            'carryover_count': result['carryover_count'],
        })
    except ConflictError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 409
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
