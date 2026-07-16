"""Owner authentication for moments write endpoints."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Callable

from flask import Request, Response

COOKIE_NAME = 'moments_owner'
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_SCOPE = b'moments-owner-v1'
_ENV_PATH = '/opt/frontend/.env'


class OwnerAuthError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _read_env_token() -> str:
    token = (os.environ.get('MOMENTS_OWNER_TOKEN') or '').strip()
    if token:
        return token
    try:
        with open(_ENV_PATH, encoding='utf-8') as handle:
            for line in handle:
                key, _, value = line.partition('=')
                if key.strip() == 'MOMENTS_OWNER_TOKEN':
                    return value.strip().strip('"').strip("'")
    except OSError:
        return ''
    return ''


def owner_token_getter() -> Callable[[], str]:
    cached = {'value': _read_env_token()}

    def getter() -> str:
        if not cached['value']:
            cached['value'] = _read_env_token()
        return cached['value']

    return getter


_get_owner_token = owner_token_getter()


def owner_token_configured() -> bool:
    return bool(_get_owner_token())


def owner_session_digest(token: str) -> str:
    return hmac.new(token.encode('utf-8'), _SCOPE, hashlib.sha256).hexdigest()


def is_owner_authenticated(request: Request) -> bool:
    token = _get_owner_token()
    if not token:
        return False
    expected = owner_session_digest(token)
    header = (request.headers.get('Authorization') or '').strip()
    if header.startswith('Bearer '):
        supplied = header[7:].strip()
        if supplied and hmac.compare_digest(supplied, token):
            return True
    cookie = (request.cookies.get(COOKIE_NAME) or '').strip()
    return bool(cookie and hmac.compare_digest(cookie, expected))


def require_owner(request: Request) -> None:
    if not owner_token_configured():
        raise OwnerAuthError('owner auth is not configured', 503)
    if not is_owner_authenticated(request):
        raise OwnerAuthError('unauthorized', 401)


def apply_owner_cookie(response: Response) -> None:
    """Set the owner session cookie. Always Secure (HTTPS-only)."""
    token = _get_owner_token()
    if not token:
        return
    response.set_cookie(
        COOKIE_NAME,
        owner_session_digest(token),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite='Lax',
        path='/',
    )


def clear_owner_cookie(response: Response) -> None:
    response.set_cookie(COOKIE_NAME, '', max_age=0, httponly=True, secure=True, samesite='Lax', path='/')


def verify_owner_token(token: str) -> bool:
    expected = _get_owner_token()
    supplied = (token or '').strip()
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))
