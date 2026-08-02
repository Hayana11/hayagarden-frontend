"""Browser BFF for manual context window (owner auth + server Bearer injection)."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from flask import Blueprint, Response, request

from daily_context_bff import same_origin_mutation_ok
from moments_auth import OwnerAuthError, require_owner

logger = logging.getLogger(__name__)

ENV_PATH = os.environ.get('HAYAGARDEN_ENV_PATH', '/opt/frontend/.env')
DEFAULT_UPSTREAM = os.environ.get('DAILY_SOFT_WINDOW_UPSTREAM', 'http://127.0.0.1:5050')

SwitchSuccessCallback = Callable[[dict[str, Any]], None]


def _positive_int_field(data: dict[str, Any], key: str) -> Optional[int]:
    value = data.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return int(value)
    return None


def parse_switch_success_payload(payload: bytes) -> Optional[dict[str, Any]]:
    """Validate upstream switch success body for resident-close callback."""
    try:
        data = json.loads(payload.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get('ok') is not True:
        return None
    required = (
        'source_context_id',
        'source_context_epoch',
        'source_resident_generation',
        'target_context_id',
        'target_context_epoch',
    )
    parsed: dict[str, Any] = {'ok': True}
    for key in required:
        val = _positive_int_field(data, key)
        if val is None:
            return None
        parsed[key] = val
    for optional in ('requested_round_count', 'selected_round_count', 'resident_generation'):
        if optional in data:
            parsed[optional] = data[optional]
    if 'selected_message_ids' in data and isinstance(data['selected_message_ids'], list):
        parsed['selected_message_ids'] = data['selected_message_ids']
    return parsed


def _invoke_switch_success_callback(
    callback: Optional[SwitchSuccessCallback],
    payload: bytes,
) -> None:
    if callback is None:
        return
    parsed = parse_switch_success_payload(payload)
    if parsed is None:
        return
    try:
        callback(parsed)
    except Exception:
        logger.exception('context-window switch on_switch_success failed')


def _default_token_from_env() -> str:
    token = os.environ.get('DAILY_SOFT_WINDOW_TOKEN', '').strip()
    if token:
        return token
    try:
        with open(ENV_PATH, encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('DAILY_SOFT_WINDOW_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ''


def _auth_error_response(exc: OwnerAuthError) -> Response:
    return Response(
        json.dumps({'ok': False, 'error': exc.message}, ensure_ascii=False),
        status=exc.status_code,
        mimetype='application/json',
    )


def create_context_window_bff_blueprint(
    *,
    token_getter: Optional[Callable[[], str]] = None,
    upstream_base: Optional[str] = None,
    owner_guard: Optional[Callable] = None,
    on_switch_success: Optional[SwitchSuccessCallback] = None,
    switch_runner: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> Blueprint:
    blueprint = Blueprint('context_window_bff', __name__)
    get_token = token_getter or _default_token_from_env
    base = (upstream_base or DEFAULT_UPSTREAM).rstrip('/')
    guard = owner_guard or require_owner

    def _require_browser_owner() -> Optional[Response]:
        try:
            guard(request)
            return None
        except OwnerAuthError as exc:
            return _auth_error_response(exc)

    def _proxy(method: str, path: str, body: Optional[bytes] = None) -> Response:
        from chat.context_window import enabled as _cw_enabled
        if not _cw_enabled():
            return Response(
                json.dumps({'ok': False, 'error': 'disabled'}, ensure_ascii=False),
                status=404,
                mimetype='application/json',
            )
        token = str(get_token() or '').strip()
        if not token:
            return Response(
                json.dumps(
                    {'ok': False, 'error': 'daily soft window token not configured'},
                    ensure_ascii=False,
                ),
                status=503,
                mimetype='application/json',
            )
        headers = {
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/json',
        }
        if body is not None:
            headers['Content-Type'] = 'application/json'
        qs = request.query_string.decode('utf-8', 'ignore')
        url = base + path + (('?' + qs) if qs else '')
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = resp.read()
                return Response(payload, status=resp.status, mimetype='application/json')
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b'{}'
            return Response(payload, status=exc.code, mimetype='application/json')
        except Exception as exc:
            logger.exception('context-window BFF upstream failed')
            return Response(
                json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False),
                status=502,
                mimetype='application/json',
            )

    def _proxy_switch(body: bytes) -> Response:
        from chat.context_window import enabled as _cw_enabled
        if not _cw_enabled():
            return Response(
                json.dumps({'ok': False, 'error': 'disabled'}, ensure_ascii=False),
                status=404,
                mimetype='application/json',
            )
        token = str(get_token() or '').strip()
        if not token:
            return Response(
                json.dumps(
                    {'ok': False, 'error': 'daily soft window token not configured'},
                    ensure_ascii=False,
                ),
                status=503,
                mimetype='application/json',
            )
        headers = {
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        url = base + '/api/context-window/switch'
        req = urllib.request.Request(url, data=body, method='POST', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
                status = int(resp.status)
                if 200 <= status < 300:
                    _invoke_switch_success_callback(on_switch_success, payload)
                return Response(payload, status=status, mimetype='application/json')
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b'{}'
            return Response(payload, status=exc.code, mimetype='application/json')
        except Exception as exc:
            logger.exception('context-window BFF switch upstream failed')
            return Response(
                json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False),
                status=502,
                mimetype='application/json',
            )

    @blueprint.route('/context-window/current', methods=['GET'])
    def bff_current():
        denied = _require_browser_owner()
        if denied is not None:
            return denied
        return _proxy('GET', '/api/context-window/current')

    @blueprint.route('/context-window/carryover-candidates', methods=['GET'])
    def bff_candidates():
        denied = _require_browser_owner()
        if denied is not None:
            return denied
        return _proxy('GET', '/api/context-window/carryover-candidates')

    @blueprint.route('/context-window/switch', methods=['POST'])
    def bff_switch():
        denied = _require_browser_owner()
        if denied is not None:
            return denied
        if not same_origin_mutation_ok(request):
            return Response(
                json.dumps({'ok': False, 'error': 'cross-origin mutation rejected'}, ensure_ascii=False),
                status=403,
                mimetype='application/json',
            )
        body = request.get_data(cache=False, as_text=False) or b'{}'
        if switch_runner is not None:
            from chat.context_window import (
                CarryoverMessageUnforgeableError,
                FirstTurnFinalizePendingError,
                IdempotencyMismatchError,
                StaleSourceContextError,
                SwitchFailedError,
                SwitchInProgressError,
                WindowBusyError,
                enabled as _cw_enabled,
                parse_strict_json_carryover_count,
                parse_strict_json_positive_int,
            )
            if not _cw_enabled():
                return Response(
                    json.dumps({'ok': False, 'error': 'disabled'}, ensure_ascii=False),
                    status=404,
                    mimetype='application/json',
                )
            try:
                data = json.loads(body.decode('utf-8') or '{}')
            except (json.JSONDecodeError, UnicodeDecodeError):
                return Response(
                    json.dumps({'ok': False, 'error': 'invalid switch payload'}, ensure_ascii=False),
                    status=400,
                    mimetype='application/json',
                )
            if not isinstance(data, dict):
                return Response(
                    json.dumps({'ok': False, 'error': 'invalid switch payload'}, ensure_ascii=False),
                    status=400,
                    mimetype='application/json',
                )
            try:
                payload = {
                    'source_context_id': parse_strict_json_positive_int(
                        'source_context_id', data.get('source_context_id'),
                    ),
                    'source_context_epoch': parse_strict_json_positive_int(
                        'source_context_epoch', data.get('source_context_epoch'),
                    ),
                    'count': parse_strict_json_carryover_count(data.get('count')),
                    'request_id': str(data['request_id']),
                    'chat_id': str(data.get('chat_id') or 'default'),
                }
            except (KeyError, TypeError, ValueError) as exc:
                return Response(
                    json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False),
                    status=400,
                    mimetype='application/json',
                )
            try:
                result = switch_runner(payload)
                out = json.dumps({'ok': True, **result}, ensure_ascii=False).encode('utf-8')
                return Response(out, status=200, mimetype='application/json')
            except IdempotencyMismatchError as exc:
                return Response(
                    json.dumps({'ok': False, 'error': str(exc), 'code': 'idempotency_mismatch'}, ensure_ascii=False),
                    status=409,
                    mimetype='application/json',
                )
            except StaleSourceContextError as exc:
                return Response(
                    json.dumps({'ok': False, 'error': str(exc), 'code': 'stale_source_context'}, ensure_ascii=False),
                    status=409,
                    mimetype='application/json',
                )
            except CarryoverMessageUnforgeableError as exc:
                return Response(
                    json.dumps({'ok': False, 'error': str(exc), 'code': 'carryover_message_unforgeable'}, ensure_ascii=False),
                    status=409,
                    mimetype='application/json',
                )
            except SwitchFailedError as exc:
                return Response(
                    json.dumps({'ok': False, 'error': str(exc), 'code': exc.error_code}, ensure_ascii=False),
                    status=409,
                    mimetype='application/json',
                )
            except FirstTurnFinalizePendingError as exc:
                return Response(
                    json.dumps({
                        'ok': False,
                        'error': str(exc),
                        'code': 'FIRST_TURN_FINALIZE_PENDING',
                        'retryable': True,
                    }, ensure_ascii=False),
                    status=423,
                    mimetype='application/json',
                )
            except (WindowBusyError, SwitchInProgressError) as exc:
                code = 'switch_in_progress' if isinstance(exc, SwitchInProgressError) else 'window_busy'
                return Response(
                    json.dumps({
                        'ok': False,
                        'error': str(exc),
                        'code': code,
                        'retryable': True,
                    }, ensure_ascii=False),
                    status=423,
                    mimetype='application/json',
                )
            except Exception as exc:
                logger.exception('context-window BFF local switch_runner failed')
                return Response(
                    json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False),
                    status=500,
                    mimetype='application/json',
                )
        return _proxy_switch(body)

    return blueprint
