"""Same-origin Soft Window BFF for the React dash.

Browser path (nginx strips ``/api/gw`` → gateway):
  GET  /api/gw/daily-context/current
  GET  /api/gw/daily-context/carryover-candidates
  POST /api/gw/daily-context/select-carryover

Gateway registers the stripped paths ``/daily-context/*`` and injects
``Authorization: Bearer <DAILY_SOFT_WINDOW_TOKEN>`` when calling the
existing protected routes on app.py (5050).

Browser access requires Moments owner auth (``moments_owner`` cookie or
valid owner Bearer via ``moments_auth.require_owner``). The Soft Window
token never leaves the server and is never taken from the browser
Authorization header.

Original ``/api/daily-context/*`` Bearer protection is unchanged.
Flag-off still returns 404 (after owner auth).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from flask import Blueprint, Response, request

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


def _host_matches(netloc: str, host: str) -> bool:
    """Compare URL netloc to request.host (case-insensitive)."""
    return bool(netloc) and netloc.lower() == (host or '').lower()


def same_origin_mutation_ok(flask_request) -> bool:
    """CSRF fence for browser POST mutations.

    - Origin/Referer present and matching Host → allow
    - Origin/Referer present and mismatched → reject (cross-origin)
    - Both missing → allow cautiously (same-origin clients / test clients
      that omit Origin); cross-origin browsers always send Origin on POST
    """
    host = (flask_request.host or '').strip()
    if not host:
        return False

    origin = (flask_request.headers.get('Origin') or '').strip()
    if origin:
        parsed = urllib.parse.urlparse(origin)
        return _host_matches(parsed.netloc, host)

    referer = (flask_request.headers.get('Referer') or '').strip()
    if referer:
        parsed = urllib.parse.urlparse(referer)
        return _host_matches(parsed.netloc, host)

    # Missing Origin and Referer: do not treat as cross-origin.
    return True


def create_daily_context_bff_blueprint(
    *,
    token_getter: Optional[Callable[[], str]] = None,
    upstream_base: Optional[str] = None,
    owner_guard: Optional[Callable] = None,
) -> Blueprint:
    """Browser-facing BFF. Does not weaken the Bearer-protected upstream routes."""
    blueprint = Blueprint('daily_context_bff', __name__)
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
        token = str(get_token() or '').strip()
        if not token:
            # Mirror upstream "token not configured" when flag would be on;
            # when flag is off upstream returns 404 without needing the token.
            from chat.daily_context import enabled as _dsw_enabled
            if not _dsw_enabled():
                return Response(
                    json.dumps({'ok': False, 'error': 'disabled'}, ensure_ascii=False),
                    status=404,
                    mimetype='application/json',
                )
            return Response(
                json.dumps(
                    {'ok': False, 'error': 'daily soft window token not configured'},
                    ensure_ascii=False,
                ),
                status=503,
                mimetype='application/json',
            )

        # Server Bearer only — never forward browser Authorization.
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
            logger.exception('daily-context BFF upstream failed')
            return Response(
                json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False),
                status=502,
                mimetype='application/json',
            )

    @blueprint.route('/daily-context/current', methods=['GET'])
    def bff_current():
        denied = _require_browser_owner()
        if denied is not None:
            return denied
        return _proxy('GET', '/api/daily-context/current')

    @blueprint.route('/daily-context/carryover-candidates', methods=['GET'])
    def bff_candidates():
        denied = _require_browser_owner()
        if denied is not None:
            return denied
        return _proxy('GET', '/api/daily-context/carryover-candidates')

    @blueprint.route('/daily-context/select-carryover', methods=['POST'])
    def bff_select():
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
        return _proxy('POST', '/api/daily-context/select-carryover', body=body)

    return blueprint
