"""Wake ``call`` action: he phones her instead of sending a message.

Off unless runtime_config WAKE_CALL_ENABLED is true. When on, the Planner may
propose ``call`` beside ``none``/``message``; B2 routes it here. The Gate runs
twice: once on the Planner decision and again right before ringing, because
"she is typing" and "a call is open" change within seconds. The ring itself
re-checks "no open call" and the 3-hour cooldown inside the transaction that
creates the call (voice_call.store), so two Wakes cannot both ring.

The opening line is written by the relay Renderer (never the shared Chat
resident, whose transcript would otherwise hold a line chat history lacks) and
is said only when she answers. The wake_log row is an audit record
(action='call'); message delivery and its ack never see it.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

import config_store

_LOG = logging.getLogger('wake_call')

CONFIG_KEY = 'WAKE_CALL_ENABLED'
CONTENT_TARGET = 'call_opening'
COOLDOWN_S = 3 * 3600          # same as voice_call.service.PROACTIVE_COOLDOWN_S
RING_TIMEOUT_S = 60            # same as voice_call.store.RING_TIMEOUT_S
MAX_OPENING_CHARS = 200        # same as voice_call.service.MAX_OPENING_CHARS
APP_BASE = 'http://127.0.0.1:5050'
RING_PATH = '/internal/voice_call/ring'
TOKEN_KEY = 'VOICE_CALL_INTERNAL_TOKEN'
TOKEN_HEADER = 'X-Voice-Call-Internal-Token'

_GATE_ALLOW = 'ALLOW'
_GATE_BLOCK = 'BLOCK'
_BLOCK_PRIORITY = (
    'duplicate',
    'user_active',
    'call_open',
    'cooldown',
    'tool_unavailable',
    'precondition_failed',
)


def call_enabled() -> bool:
    """Fail-safe: missing / bad config → OFF."""
    try:
        return config_store.get_bool(CONFIG_KEY, default=False)
    except Exception:
        return False


def _call_state(get_db_fn: Callable, clock: Callable[[], float]) -> tuple[bool, Optional[float]]:
    """(a call is ringing or active, when he last placed one). Advisory: ring re-checks."""
    conn = get_db_fn()
    try:
        cols = {row[1] for row in conn.execute('PRAGMA table_info(voice_calls)')}
        if not cols:
            return False, None
        now = clock()
        open_row = conn.execute(
            "SELECT 1 FROM voice_calls WHERE state='active' "
            "OR (state='ringing' AND created_at>?) LIMIT 1",
            (now - RING_TIMEOUT_S,),
        ).fetchone()
        last = None
        if 'caller' in cols:
            last = conn.execute(
                "SELECT MAX(created_at) FROM voice_calls WHERE caller='fyodor'"
            ).fetchone()[0]
        return bool(open_row), last
    finally:
        conn.close()


def evaluate_call_gate(
    *,
    planner_decision: Mapping[str, Any],
    skill_view: Any,
    wake_run_id: str,
    get_db_fn: Callable,
    now: Any,
    chat_busy: bool = False,
    wake_run_id_seen: Callable[[str], bool] = lambda _rid: False,
    min_idle_minutes: float = 30.0,
    clock: Callable[[], float] = time.time,
) -> tuple[str, str]:
    """Deterministic call Gate. Returns (ALLOW|BLOCK, reason_code).

    "She is active" is never a stored flag: it is the live generating state plus
    the time since her last real message (interaction clock), so it cannot stick.
    """
    if str(planner_decision.get('action_candidate') or '').strip() != 'call':
        return _GATE_BLOCK, 'precondition_failed'
    blocks: list[str] = []
    rid = str(wake_run_id or '').strip()
    if rid and wake_run_id_seen(rid):
        blocks.append('duplicate')
    if chat_busy:
        blocks.append('user_active')
    try:
        from chat.interaction_state import read_interaction_clock
        interaction = read_interaction_clock(get_db_fn, now=now)
        if not interaction.reliable or interaction.user_idle_minutes is None:
            blocks.append('precondition_failed')
        elif interaction.user_idle_minutes < float(min_idle_minutes):
            blocks.append('user_active')
    except Exception as exc:
        _LOG.warning('call gate clock read failed: %s', exc)
        blocks.append('precondition_failed')
    if not call_enabled() or 'call' not in set(skill_view.resolved_action_capability):
        blocks.append('tool_unavailable')
    try:
        call_open, last = _call_state(get_db_fn, clock)
        if call_open:
            blocks.append('call_open')
        if last is not None and clock() - last < COOLDOWN_S:
            blocks.append('cooldown')
    except Exception as exc:
        _LOG.warning('call gate call state read failed: %s', exc)
        blocks.append('precondition_failed')
    for reason in _BLOCK_PRIORITY:
        if reason in blocks:
            return _GATE_BLOCK, reason
    return _GATE_ALLOW, 'ok'


def validate_opening(text: str) -> str:
    """The Renderer's opening, or ValueError: one or two short spoken lines."""
    from chat.behavior_authority_b3 import validate_rendered_content
    opening = validate_rendered_content(text).strip()
    if len(opening) > MAX_OPENING_CHARS:
        raise ValueError('call_opening_too_long')
    return opening


def _read_token() -> str:
    from voice_call.tts import read_env_value
    return read_env_value(TOKEN_KEY)


def _urllib_post(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def ring_her(
    opening: str,
    language: Optional[str] = None,
    *,
    post: Callable = _urllib_post,
    token_fn: Callable[[], str] = _read_token,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Ask the app to ring her. Returns (rang, reason): reason is the call id or why not."""
    token = token_fn() or ''
    if not token:
        return False, 'internal_token_missing'
    body = {'opening': opening}
    if language:
        body['language'] = language
    headers = {'Content-Type': 'application/json', TOKEN_HEADER: token}
    try:
        status, data = post(APP_BASE + RING_PATH, headers,
                            json.dumps(body, ensure_ascii=False).encode('utf-8'), timeout)
    except Exception as exc:
        return False, 'ring_request_failed:%s' % type(exc).__name__
    if status == 201:
        try:
            return True, str(json.loads(data.decode('utf-8'))['call']['id'])
        except Exception:
            return True, ''
    return False, {409: 'call_open', 429: 'cooldown'}.get(status, 'ring_http_%s' % status)
