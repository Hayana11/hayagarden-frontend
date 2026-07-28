"""Browser BFF for manual context window (owner auth + server Bearer injection)."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from flask import Blueprint, Response, request

from daily_context_bff import same_origin_mutation_ok
from moments_auth import OwnerAuthError, require_owner

logger = logging.getLogger(__name__)

ENV_PATH = os.environ.get('HAYAGARDEN_ENV_PATH', '/opt/frontend/.env')
DEFAULT_UPSTREAM = os.environ.get('DAILY_SOFT_WINDOW_UPSTREAM', 'http://127.0.0.1:5050')


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
        return _proxy('POST', '/api/context-window/switch', body=body)

    return blueprint
