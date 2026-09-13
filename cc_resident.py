"""Persistent (resident) Claude Code subprocess for Fyodor's solo chat.

Cold start sends persona/history/full state once. Hot turns only send the new
user message, recall, changed state components, and new group-chat rows.
State/group cursors commit after stdin write+flush. Feedback/dream claims are
returned on done and consumed only after assistant DB persistence.
Client disconnect (GeneratorExit) kills the resident to avoid stdout pollution.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import select
import subprocess
import tempfile
import threading
import time
import uuid

import config_store

NL = chr(10)
CC_STREAM_TIMEOUT = 360  # stall / inactivity seconds (runtime-tunable)
CC_STREAM_HARD_TIMEOUT = 1800  # absolute per-turn ceiling (runtime-tunable)
CC_STREAM_RESULT_GRACE = 30  # wait for result after provider end_turn (runtime-tunable)
IDLE_REAP_SECONDS = 3 * 60 * 60
TOOL_PROFILE_LEGACY = 'legacy'
TOOL_PROFILE_TEXT_ONLY = 'text_only'
TOOL_PROFILE_UH_A0 = 'uh_a0'

JSONL_FINALITY_PROFILE_DEFAULT = 'default'
JSONL_FINALITY_PROFILE_UNIFIED_NORMAL_WAKE = 'unified_normal_wake'
JSONL_FINALITY_RETRY_DELAYS = (0.0, 0.05, 0.15, 0.35)
# The normal Wake provider result is authoritative before JSONL has necessarily
# flushed its final multi-tool assistant rows.  This remains a bounded proof:
# every read replays the same frozen session cursor and only an exact totals
# match can release the caller.
UNIFIED_NORMAL_WAKE_JSONL_FINALITY_RETRY_DELAYS = (
    0.0, 0.05, 0.15, 0.35, 0.45,
)

# Claude stdout events that refresh the stall / inactivity deadline.
# Gateway/SSE heartbeats are synthetic and must NOT be listed here.
_CLAUDE_ACTIVITY_STREAM_EVENTS = frozenset({
    'message_start',
    'message_delta',
})
_CLAUDE_ACTIVITY_DELTA_TYPES = frozenset({
    'thinking_delta',
    'text_delta',
})

_USAGE_PROVENANCE_FIELDS = (
    'input_tokens',
    'output_tokens',
    'cache_read_input_tokens',
    'cache_creation_input_tokens',
)


def _diagnostic_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _diagnostic_usage_snapshot(usage):
    usage = usage if isinstance(usage, dict) else {}
    return {
        key: _diagnostic_int(usage.get(key))
        for key in _USAGE_PROVENANCE_FIELDS
    }


def _diagnostic_round_snapshot(round_row):
    if not isinstance(round_row, dict):
        return None
    return {
        'round_index': _diagnostic_int(round_row.get('index')),
        'complete': bool(round_row.get('complete')),
        'input_tokens': _diagnostic_int(round_row.get('input_tokens')),
        'output_tokens': _diagnostic_int(round_row.get('output_tokens')),
        'cache_read': _diagnostic_int(round_row.get('cache_read')),
        'cache_creation': _diagnostic_int(round_row.get('cache_creation')),
        'context_tokens': _diagnostic_int(round_row.get('context_tokens')),
    }


def _diagnostic_request_id(*events):
    for event in events:
        if not isinstance(event, dict):
            continue
        for key in ('request_id', 'requestId'):
            value = str(event.get(key) or '').strip()
            if value:
                return value[:200]
    return None


def _log_wake_round_usage(
    *,
    diagnostic_wake_run_id,
    turn_identity,
    round_index,
    event_source,
    usage,
    current_round_before,
    current_round_after,
    round_already_complete,
    provider_request_id=None,
):
    if not diagnostic_wake_run_id:
        return
    payload = {
        'stage': 'ROUND_USAGE',
        'wake_run_id': str(diagnostic_wake_run_id),
        'turn_identity': str(turn_identity or ''),
        'round_index': _diagnostic_int(round_index),
        'event_source': str(event_source),
        'usage_present': isinstance(usage, dict) and bool(usage),
        'current_round_before': current_round_before,
        'current_round_after': current_round_after,
        'round_already_complete': bool(round_already_complete),
    }
    payload.update(_diagnostic_usage_snapshot(usage))
    if provider_request_id:
        payload['provider_request_id'] = str(provider_request_id)[:200]
    logging.getLogger(__name__).info(
        '[WAKE-LIVE] %s',
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _log_wake_round_close(
    *,
    diagnostic_wake_run_id,
    turn_identity,
    round_row,
    close_reason,
):
    if not diagnostic_wake_run_id:
        return
    snapshot = _diagnostic_round_snapshot(round_row) or {}
    payload = {
        'stage': 'ROUND_CLOSE',
        'wake_run_id': str(diagnostic_wake_run_id),
        'turn_identity': str(turn_identity or ''),
        'round_index': snapshot.get('round_index'),
        'close_reason': str(close_reason),
        'final_usage': {
            key: snapshot.get(key)
            for key in (
                'input_tokens',
                'output_tokens',
                'cache_read',
                'cache_creation',
                'context_tokens',
            )
        },
    }
    logging.getLogger(__name__).info(
        '[WAKE-LIVE] %s',
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )




def _cfg_int(key, default):
    try:
        return int(config_store.get_int(key, default))
    except Exception:
        return default


def _claude_event_is_activity(d):
    """True when a parsed Claude stdout event counts as real activity."""
    if not isinstance(d, dict):
        return False
    t = d.get('type')
    if t == 'system' and d.get('subtype') == 'init':
        return True
    if t in ('assistant', 'result'):
        return True
    if t == 'user':
        for b in ((d.get('message') or {}).get('content') or []):
            if isinstance(b, dict) and b.get('type') == 'tool_result':
                return True
        return False
    if t == 'stream_event':
        ev = d.get('event') or {}
        ev_type = ev.get('type')
        if ev_type in _CLAUDE_ACTIVITY_STREAM_EVENTS:
            return True
        if ev_type == 'content_block_delta':
            delta = ev.get('delta') or {}
            return delta.get('type') in _CLAUDE_ACTIVITY_DELTA_TYPES
        # tool_use may also appear as a content_block_start name; treat
        # content_block_start with tool_use as activity if present.
        if ev_type == 'content_block_start':
            block = ev.get('content_block') or {}
            return block.get('type') == 'tool_use'
        return False
    return False


class ProviderTerminalTracker:
    """Tracks provider progress and the authoritative result boundary."""

    def __init__(self, grace_seconds):
        self.grace_seconds = max(0.1, float(grace_seconds))
        self.turn_identity = uuid.uuid4().hex
        self.last_provider_event_type = None
        self.last_provider_activity_at = None
        self.end_turn_seen = False
        self.end_turn_seen_at = None
        self.result_seen = False
        self.terminal_reason = None

    @staticmethod
    def _stop_reason(event):
        if not isinstance(event, dict):
            return None
        t = event.get('type')
        if t == 'stream_event':
            streamed = event.get('event') or {}
            delta = streamed.get('delta') or {}
            return (
                delta.get('stop_reason')
                or streamed.get('stop_reason')
                or (streamed.get('message') or {}).get('stop_reason')
            )
        if t == 'assistant':
            return (
                (event.get('message') or {}).get('stop_reason')
                or event.get('stop_reason')
            )
        return None

    def observe(self, event):
        if self.terminal_reason is not None or not isinstance(event, dict):
            return
        event_type = event.get('type')
        if event_type == 'stream_event':
            streamed = event.get('event') or {}
            label = 'stream_event:%s' % (streamed.get('type') or 'unknown')
        else:
            label = str(event_type or 'unknown')
        self.last_provider_event_type = label
        if event_type == 'result':
            self.result_seen = True
            self.terminal_reason = 'result'
            self.last_provider_activity_at = time.time()
            return
        if _claude_event_is_activity(event):
            self.last_provider_activity_at = time.time()
        if self._stop_reason(event) == 'end_turn' and not self.end_turn_seen:
            self.end_turn_seen = True
            self.end_turn_seen_at = time.monotonic()

    def grace_expired(self, now=None):
        if self.terminal_reason is not None or not self.end_turn_seen:
            return False
        current = time.monotonic() if now is None else float(now)
        if current - float(self.end_turn_seen_at) < self.grace_seconds:
            return False
        self.terminal_reason = 'result_missing_after_end_turn'
        return True

    def snapshot(self, *, terminal_reason=None, partial_rescue_performed=False,
                 lock_released=None):
        return {
            'turn_identity': self.turn_identity,
            'resident_generation': None,
            'resident_pid': None,
            'claude_session_id': None,
            'last_provider_event_type': self.last_provider_event_type,
            'last_provider_activity_at': self.last_provider_activity_at,
            'end_turn_seen': bool(self.end_turn_seen),
            'end_turn_seen_at': self.end_turn_seen_at,
            'result_seen': bool(self.result_seen),
            'terminal_reason': terminal_reason or self.terminal_reason,
            'partial_rescue_performed': bool(partial_rescue_performed),
            'lock_released': lock_released,
        }


class StreamWatchdog:
    """Single-thread stall + hard deadline watchdog for one send_turn.

    Stall timeout: no Claude activity for ``stall_timeout`` seconds.
    Hard timeout: turn wall time exceeds ``hard_timeout`` regardless of activity.
    Stale checks cannot kill after ``note_activity`` refreshes the stall deadline.
    """

    def __init__(
        self,
        *,
        stall_timeout,
        hard_timeout,
        on_timeout,
        is_proc_alive,
        poll_cap_sec=0.2,
    ):
        self.stall_timeout = float(stall_timeout)
        self.hard_timeout = float(hard_timeout)
        self._on_timeout = on_timeout
        self._is_proc_alive = is_proc_alive
        self._poll_cap_sec = float(poll_cap_sec)
        self._lock = threading.Lock()
        now = time.monotonic()
        self._started_at = now
        self._last_activity_at = now
        self._stopped = False
        self._fired_reason = None  # 'stall' | 'hard'
        self._thread = None

    @property
    def fired_reason(self):
        with self._lock:
            return self._fired_reason

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name='cc-stream-watchdog', daemon=True,
        )
        self._thread.start()

    def stop(self):
        with self._lock:
            self._stopped = True

    def note_activity(self):
        with self._lock:
            if self._stopped or self._fired_reason is not None:
                return
            self._last_activity_at = time.monotonic()

    def _try_fire(self, reason):
        """Knock-window check then fire. Returns True if timeout committed.

        Alive probe runs unlocked so ``note_activity`` can refresh a stall
        deadline between the first observation and the final commit. A stale
        fire after refresh must return False and must not kill.
        """
        with self._lock:
            if self._stopped or self._fired_reason is not None:
                return False
            now = time.monotonic()
            if reason == 'hard':
                if (now - self._started_at) < self.hard_timeout:
                    return False
            else:
                if (now - self._last_activity_at) < self.stall_timeout:
                    return False
        try:
            alive = bool(self._is_proc_alive())
        except Exception:
            alive = False
        if not alive:
            # Process already gone — leave EOF handling to the reader loop.
            return False
        with self._lock:
            if self._stopped or self._fired_reason is not None:
                return False
            now = time.monotonic()
            if reason == 'hard':
                if (now - self._started_at) < self.hard_timeout:
                    return False
            else:
                # Activity refreshed while we knocked — stale fire abandoned.
                if (now - self._last_activity_at) < self.stall_timeout:
                    return False
            self._fired_reason = reason
            self._stopped = True
        try:
            self._on_timeout(reason)
        except Exception:
            pass
        return True

    def _loop(self):
        while True:
            with self._lock:
                if self._stopped:
                    return
                now = time.monotonic()
                stall_left = self.stall_timeout - (now - self._last_activity_at)
                hard_left = self.hard_timeout - (now - self._started_at)
            if hard_left <= 0:
                if self._try_fire('hard'):
                    return
            elif stall_left <= 0:
                if self._try_fire('stall'):
                    return
            wait = self._poll_cap_sec
            if hard_left > 0:
                wait = min(wait, hard_left)
            if stall_left > 0:
                wait = min(wait, stall_left)
            time.sleep(max(0.01, wait))


class ResidentError(RuntimeError):
    def __init__(self, message, *, usage=None, diagnostics=None, error_code=None):
        super().__init__(message)
        self.usage = usage or empty_usage()
        self.diagnostics = dict(diagnostics or {})
        self.error_code = error_code


def empty_usage(**overrides):
    base = {
        'v': 2,
        'provider': 'claude_code',
        'num_rounds': 0,
        'input_tokens': 0,
        'output_tokens': 0,
        'cache_read': 0,
        'cache_creation': 0,
        'cache_creation_5m': 0,
        'cache_creation_1h': 0,
        'request_ids': [],
        'request_count': 0,
        'last_round_context': 0,
        'max_round_context': 0,
        'resident_turn_count': 0,
        'respawn_reason': None,
        'rounds': [],
    }
    base.update(overrides)
    return base


def summarize_rounds(rounds, *, resident_turn_count=0, respawn_reason=None, max_round_context=0):
    rounds = list(rounds or [])
    usage = empty_usage(
        num_rounds=len(rounds),
        input_tokens=sum(int(r.get('input_tokens') or 0) for r in rounds),
        output_tokens=sum(int(r.get('output_tokens') or 0) for r in rounds),
        cache_read=sum(int(r.get('cache_read') or 0) for r in rounds),
        cache_creation=sum(int(r.get('cache_creation') or 0) for r in rounds),
        cache_creation_5m=sum(int(r.get('cache_creation_5m') or 0) for r in rounds),
        cache_creation_1h=sum(int(r.get('cache_creation_1h') or 0) for r in rounds),
        request_ids=[r.get('request_id') for r in rounds if r.get('request_id')],
        request_count=len([r for r in rounds if r.get('request_id')]),
        resident_turn_count=resident_turn_count,
        respawn_reason=respawn_reason,
        rounds=rounds,
    )
    if rounds:
        last = rounds[-1]
        usage['last_round_context'] = int(last.get('context_tokens') or 0)
        usage['max_round_context'] = max(
            int(max_round_context or 0),
            max(int(r.get('context_tokens') or 0) for r in rounds),
        )
    else:
        usage['max_round_context'] = int(max_round_context or 0)
    return usage


def normalize_cache_info(raw):
    """Accept legacy v1 and v2 cache_info without raising."""
    if not isinstance(raw, dict):
        return empty_usage()
    version = int(raw.get('v') or 1)
    if version >= 2:
        usage = empty_usage()
        for key in usage:
            if key in raw:
                usage[key] = raw[key]
        usage['v'] = 2
        usage['num_rounds'] = int(raw.get('num_rounds') or len(raw.get('rounds') or []) or 1)
        usage['rounds'] = list(raw.get('rounds') or [])
        # 阶段 1A：成功回复可选观测字段（缺省保持缺失，不写成 0/false）
        for key in ('observation_version', 'context_breakdown', 'runtime', 'jsonl_usage'):
            if key in raw:
                usage[key] = raw[key]
        return usage
    return empty_usage(
        v=1,
        num_rounds=1,
        cache_read=int(raw.get('cache_read') or 0),
        cache_creation=int(raw.get('cache_creation') or 0),
        input_tokens=int(raw.get('input_tokens') or 0),
        output_tokens=int(raw.get('output_tokens') or 0),
    )


class ResidentSession:
    """One persistent `claude` subprocess. Not safe for concurrent turns —
    caller must serialize (gateway.py already does via _gen_acquire_or_wait)."""

    def __init__(self, cwd, allowed_tools, mcp_config_path):
        self._cwd = cwd
        self._allowed_tools = allowed_tools
        self._mcp_config_path = mcp_config_path
        self._proc = None
        self._system_text = None
        self._session_id = None
        self._cold = True
        self._last_used = 0.0
        self._generation = 0
        self._lock = threading.Lock()
        self._next_spawn_reason = None
        self._tool_profile = TOOL_PROFILE_LEGACY
        # Provider-authoritative deferred tool call awaiting user confirmation.
        # In-memory only; no approval/database/state-machine persistence.
        self._pending_deferred = None
        # MODEL-1B: identity of the model argv this process was started with.
        self._model_identity = None
        # CC-CHAT-EFFORT-R1: identity of the effort argv this process was
        # started with. None preserves compatibility with pre-R1 fake fixtures.
        self._effort_identity = None
        self._effort_value = None
        # Durable history-rewrite epoch bound at last successful spawn.
        # Compared against the cross-process epoch before hot reuse so every
        # gunicorn worker lazily invalidates after a rewrite, even when the
        # app→gateway bridge only eagers one worker.
        self._history_rewrite_epoch = ''
        # M2-02B: surface identity bound to the currently alive UH-A0 process.
        # It is written only after Popen succeeds and is intentionally not
        # refreshed by _reset_session_meta or by a failed spawn.
        self._bound_tool_surface_fingerprint = None
        # Each UH-A0 resident owns one opaque lease path for its full lifetime.
        # It is allocated before the first Claude spawn and never derives from
        # the provider session id, which is unavailable on cold start.
        self._uh_a0_turn_lease_path = self._new_uh_a0_turn_lease_path()
        # No-benefit respawn loop breaker (P0 cold-storm fix, Fence C).
        # Deliberately *not* reset by ``_reset_session_meta`` / ``_spawn`` —
        # the estimate/generation pair must survive across the respawn it gates.
        self._last_cold_bootstrap_estimate = 0
        self._last_cold_bootstrap_generation = 0
        # Captured in ``_decide_respawn_reason`` when returning ``hard_context``,
        # *before* ``_spawn`` resets ``_turns_since_respawn``. Used to distinguish
        # immediate post-cold storms from genuine hot growth respawns.
        self._hard_context_pre_spawn_turns = None
        self._reset_session_meta(respawn_reason=None)

    @staticmethod
    def _new_uh_a0_turn_lease_path():
        lease_dir = os.path.join(tempfile.gettempdir(), 'hayagarden-uh-a0-turn-leases')
        return os.path.join(lease_dir, f'{uuid.uuid4().hex}.json')

    def _prepare_spawn_env(self, env):
        """Bind the resident-owned lease path for every UH-A0 spawn."""
        if self._tool_profile != TOOL_PROFILE_UH_A0:
            return env
        prepared = dict(os.environ if env is None else env)
        prepared['UH_A0_TURN_LEASE_PATH'] = self._uh_a0_turn_lease_path
        return prepared

    def _reset_session_meta(self, *, respawn_reason):
        self._resident_turn_count = 0
        self._last_round_context = 0
        self._max_round_context = 0
        self._pending_respawn_reason = respawn_reason
        self._turns_since_respawn = 0
        self._last_state_snapshot = {}
        self._last_state_send_snapshot = {}
        self._last_successful_lean_state = False
        self._last_state_anchor_generation = -1
        self._last_state_schema_version = None
        self._turns_since_state_anchor = 0
        self._state_delta_chars_since_anchor = 0
        self._last_state_anchor_version = None
        self._committed_file_hashes = set()
        self._pending_file_hashes = set()
        self._last_group_message_id = 0
        self._group_cursor_initialized = False
        self._last_rel_fingerprint = None
        self._turns_since_rel_sent = 0
        self._last_rel_mood = None
        self._keepwarm_lease_expires_at = None
        self._tool_surface_snapshot = {}

    def _build_spawn_tool_flags(self, *, env=None):
        """Split built-in availability (--tools) from MCP permission args.

        UH-A0 uses a fixed physical surface from ``cc_capability_adapter``.
        Legacy keeps ``--tools ''`` and the constructor allowlist. text_only
        keeps both built-ins and MCP executable surfaces empty.
        """
        if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
            return {
                'tools': '',
                'extra': ['--allowedTools', ''],
                'surface_allowed': '',
                'mcp_path': None,
                'surface_fingerprint': None,
            }
        if self._tool_profile == TOOL_PROFILE_UH_A0:
            from tools.cc_capability_adapter import build_uh_a0_spawn_plan
            spawn_env = self._prepare_spawn_env(env)
            plan = build_uh_a0_spawn_plan(
                cwd=self._cwd,
                legacy_mcp_config_path=self._mcp_config_path,
                env=spawn_env,
            )
            return {
                'tools': plan['built_in_tools_csv'],
                'extra': list(plan['spawn_extra_args']),
                'surface_allowed': plan['surface_allowlist_csv'],
                'mcp_path': plan['mcp_config_path'],
                'surface_fingerprint': plan['physical_surface_fingerprint'],
            }
        return {
            'tools': '',
            'extra': [
                '--mcp-config', self._mcp_config_path,
                '--strict-mcp-config',
                '--allowedTools', self._allowed_tools,
            ],
            'surface_allowed': self._allowed_tools,
            'mcp_path': self._mcp_config_path,
            'surface_fingerprint': None,
        }

    def _require_spawn_surface_fingerprint(self, tool_flags):
        """Validate the surface identity before creating a UH-A0 process."""
        if self._tool_profile != TOOL_PROFILE_UH_A0:
            return None
        fingerprint = str(tool_flags.get('surface_fingerprint') or '').strip()
        if not fingerprint:
            raise ResidentError('uh_a0_surface_fingerprint_missing')
        return fingerprint

    def _capture_tool_surface(self, tool_flags):
        if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
            self._tool_surface_snapshot = {}
            return
        try:
            from tools.cc_tool_surface import capture_tool_surface_snapshot
            self._tool_surface_snapshot = capture_tool_surface_snapshot(
                tool_flags.get('surface_allowed') or '',
                mcp_config_path=tool_flags.get('mcp_path') or self._mcp_config_path,
            )
        except Exception:
            self._tool_surface_snapshot = {}

    def _spawn(self, system_text, env, *, reason='process_dead', tool_profile=TOOL_PROFILE_LEGACY):
        from chat.cc_model import cc_model_snapshot
        from chat.cc_effort import cc_effort_snapshot
        from chat.cc_runtime import ClaudeRuntimeError, claude_cmd, require_pinned_claude_version
        self._kill(quiet=True)
        self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
        env = self._prepare_spawn_env(env)
        try:
            require_pinned_claude_version(env=env, cwd=self._cwd)
        except ClaudeRuntimeError as exc:
            raise ResidentError('claude_runtime:%s' % exc) from exc
        _model, model_identity, model_args = cc_model_snapshot()
        effort, effort_identity, effort_args = cc_effort_snapshot()
        tool_flags = self._build_spawn_tool_flags(env=env)
        surface_fingerprint = self._require_spawn_surface_fingerprint(tool_flags)
        base_args = claude_cmd(
            '-p',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            '--include-partial-messages',
            '--system-prompt', system_text,
            '--max-turns', '5',
            '--tools', tool_flags['tools'],
            '--thinking-display', 'summarized',
            '--exclude-dynamic-system-prompt-sections',
        ) + model_args + effort_args
        args = base_args + list(tool_flags['extra'])
        self._proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=self._cwd, env=env,
        )
        if surface_fingerprint is not None:
            self._bound_tool_surface_fingerprint = surface_fingerprint
        # Bind epoch only after a successful spawn. A failed Popen must leave
        # the prior (stale) binding so hot reuse stays forbidden.
        try:
            from chat.cc_history_rewrite import (
                current_history_rewrite_epoch,
                sanitize_bound_epoch,
            )
            bound_epoch = sanitize_bound_epoch(current_history_rewrite_epoch())
        except Exception:
            bound_epoch = ''
        self._history_rewrite_epoch = bound_epoch
        self._system_text = system_text
        self._model_identity = model_identity
        self._effort_identity = effort_identity
        self._effort_value = effort or None
        self._session_id = None
        self._cold = True
        self._generation += 1
        self._next_spawn_reason = None
        self._reset_session_meta(respawn_reason=reason)
        self._capture_tool_surface(tool_flags)

    def _kill(self, quiet=False):
        proc, self._proc = self._proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=8)
            except Exception:
                pass
        if not quiet:
            try:
                proc.stdout.close()
            except Exception:
                pass
        try:
            proc.stderr.close()
        except Exception:
            pass

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _decide_respawn_reason(self, system_text, *, tool_profile=TOOL_PROFILE_LEGACY):
        from chat.cc_model import cc_model_identity
        from chat.cc_effort import cc_effort_identity
        # Durable rewrite epoch: any resident spawned before the latest
        # committed rewrite loses hot-reuse on every worker, lazily.
        try:
            from chat.cc_history_rewrite import (
                current_history_rewrite_epoch,
                is_unreadable_epoch,
                sanitize_bound_epoch,
            )
            durable_epoch = current_history_rewrite_epoch()
        except Exception:
            raise ResidentError('durable_history_epoch_unreadable')
        if is_unreadable_epoch(durable_epoch):
            raise ResidentError('durable_history_epoch_unreadable')
        bound_epoch = sanitize_bound_epoch(getattr(self, '_history_rewrite_epoch', None) or '')
        if durable_epoch and durable_epoch != bound_epoch:
            return 'history_rewrite'
        if not self._alive():
            return self._next_spawn_reason or 'process_dead'
        if str(tool_profile or TOOL_PROFILE_LEGACY) != str(self._tool_profile or TOOL_PROFILE_LEGACY):
            return 'tool_profile_changed'
        # MODEL-1B: only compare when this resident was actually spawned with an
        # identity. Fake/pre-1B alive fixtures keep _model_identity=None and must
        # still reach turn_limit / idle / system_changed contracts.
        stored_identity = getattr(self, '_model_identity', None)
        if stored_identity is not None and cc_model_identity() != stored_identity:
            return 'model_changed'
        stored_effort_identity = getattr(self, '_effort_identity', None)
        if (
            stored_effort_identity is not None
            and cc_effort_identity() != stored_effort_identity
        ):
            return 'effort_changed'
        # Never-used residents (_last_used == 0) have no idle age — peek_idle_seconds
        # returns None. Only reap after a real successful use older than IDLE_REAP.
        idle_seconds = self.peek_idle_seconds()
        if idle_seconds is not None and idle_seconds > IDLE_REAP_SECONDS:
            return 'idle'
        if system_text != self._system_text:
            return 'system_changed'

        hard = _cfg_int('CC_CONTEXT_HARD_LIMIT', 120_000)
        soft = _cfg_int('CC_CONTEXT_SOFT_LIMIT', 90_000)
        max_turns = _cfg_int('CC_MAX_RESIDENT_TURNS', 30)
        min_between = _cfg_int('CC_MIN_TURNS_BETWEEN_RESPAWNS', 5)

        if self._last_round_context >= hard:
            self._hard_context_pre_spawn_turns = int(self._turns_since_respawn or 0)
            return 'hard_context'
        if self._resident_turn_count >= max_turns:
            return 'turn_limit'
        if (
            self._last_round_context >= soft
            and self._turns_since_respawn >= min_between
        ):
            return 'soft_context'

        # M2-02B is lazy invalidation: compare only at the read-only
        # pre-stdin decision boundary. The adapter fingerprint is pure and
        # fail-closed, so storage failure cannot keep an open resident hot.
        if (
            str(tool_profile or TOOL_PROFILE_LEGACY) == TOOL_PROFILE_UH_A0
            and str(self._tool_profile or TOOL_PROFILE_LEGACY) == TOOL_PROFILE_UH_A0
        ):
            bound_surface = str(
                getattr(self, '_bound_tool_surface_fingerprint', None) or ''
            ).strip()
            if bound_surface:
                try:
                    from tools.cc_capability_adapter import physical_surface_fingerprint
                    current_surface = str(physical_surface_fingerprint() or '').strip()
                except Exception:
                    current_surface = ''
                if current_surface != bound_surface:
                    return 'tool_surface_changed'

        return None

    def ensure_alive(self, system_text, env, *, tool_profile=TOOL_PROFILE_LEGACY):
        with self._lock:
            reason = self._decide_respawn_reason(system_text, tool_profile=tool_profile)
            if reason:
                if reason == 'history_rewrite':
                    self._system_text = None
                    self._session_id = None
                    self._model_identity = None
                    self._effort_identity = None
                    self._effort_value = None
                    self._cold = True
                    self._next_spawn_reason = 'history_rewrite'
                self._spawn(system_text, env, reason=reason, tool_profile=tool_profile)
            return self._cold

    def peek_respawn_reason(self, system_text, *, tool_profile=TOOL_PROFILE_LEGACY):
        """Read-only: same reason as ``_decide_respawn_reason``, or None.

        Does not spawn, kill, change generation, or write stdin.
        """
        with self._lock:
            return self._decide_respawn_reason(system_text, tool_profile=tool_profile)

    def spawn_resumable(
        self,
        system_text,
        env,
        *,
        resume_session_id,
        tool_profile=TOOL_PROFILE_LEGACY,
        reason='forge_staged',
    ):
        """Start this (staged) instance with --resume. Must not be the live singleton mid-turn.

        Does not send stdin. Caller runs a no-stdin health window afterwards.
        """
        resume_session_id = str(resume_session_id or '').strip()
        if not resume_session_id:
            raise ResidentError('resume_session_id required')
        from chat.cc_model import cc_model_snapshot
        from chat.cc_effort import cc_effort_snapshot
        from chat.cc_runtime import ClaudeRuntimeError, claude_cmd, require_pinned_claude_version
        with self._lock:
            if self._alive():
                raise ResidentError('staged spawn on live session')
            self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
            env = self._prepare_spawn_env(env)
            try:
                require_pinned_claude_version(env=env, cwd=self._cwd)
            except ClaudeRuntimeError as exc:
                raise ResidentError('claude_runtime:%s' % exc) from exc
            _model, model_identity, model_args = cc_model_snapshot()
            effort, effort_identity, effort_args = cc_effort_snapshot()
            tool_flags = self._build_spawn_tool_flags(env=env)
            surface_fingerprint = self._require_spawn_surface_fingerprint(tool_flags)
            base_args = claude_cmd(
                '-p',
                '--input-format', 'stream-json',
                '--output-format', 'stream-json',
                '--verbose',
                '--include-partial-messages',
                '--system-prompt', system_text,
                '--max-turns', '5',
                '--tools', tool_flags['tools'],
                '--thinking-display', 'summarized',
                '--exclude-dynamic-system-prompt-sections',
                '--resume', resume_session_id,
            ) + model_args + effort_args
            args = base_args + list(tool_flags['extra'])
            try:
                self._proc = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, cwd=self._cwd, env=env,
                )
            except Exception as exc:
                self._proc = None
                raise ResidentError('staged_spawn_failed:%s' % exc) from exc
            if surface_fingerprint is not None:
                self._bound_tool_surface_fingerprint = surface_fingerprint
            try:
                from chat.cc_history_rewrite import (
                    current_history_rewrite_epoch,
                    sanitize_bound_epoch,
                )
                self._history_rewrite_epoch = sanitize_bound_epoch(
                    current_history_rewrite_epoch(),
                )
            except Exception:
                self._history_rewrite_epoch = ''
            self._system_text = system_text
            self._model_identity = model_identity
            self._effort_identity = effort_identity
            self._effort_value = effort or None
            self._session_id = resume_session_id
            self._cold = False
            self._generation += 1
            self._reset_session_meta(respawn_reason=reason)
            self._capture_tool_surface(tool_flags)
            return self

    def _capture_authoritative_deferred(self, result):
        """Capture only Claude's result.deferred_tool_use identity."""
        from tools.execution_fence import build_approval_id, capability_for_tool

        session_id = str(result.get('session_id') or '').strip()
        raw = result.get('deferred_tool_use')
        if not session_id or not isinstance(raw, dict):
            raise ResidentError('tool_deferred_missing_authoritative_payload')
        tool_use_id = str(raw.get('id') or '').strip()
        tool_name = str(raw.get('name') or '').strip()
        tool_input = raw.get('input')
        if not tool_use_id or not tool_name or not isinstance(tool_input, dict):
            raise ResidentError('tool_deferred_malformed_authoritative_payload')
        capability_id = capability_for_tool(tool_name)
        if not capability_id:
            raise ResidentError('tool_deferred_unknown_tool')
        pending = {
            'session_id': session_id,
            'tool_use_id': tool_use_id,
            'tool_name': tool_name,
            'tool_input': copy.deepcopy(tool_input),
            'approval_id': build_approval_id(
                capability_id, tool_name, tool_input,
            ),
        }
        self._session_id = session_id
        self._pending_deferred = pending
        return pending

    def resume_pending_deferred_turn(
        self,
        content,
        env,
        turn_lease,
        *,
        commit_meta=None,
        on_stdin_flushed=None,
        idle_heartbeat_sec=None,
        turn_runtime=None,
    ):
        """Resume one provider-deferred action with a new confirmation lease."""
        from tools.capability_manifest import get_capability
        from tools.execution_fence import (
            UH_A0TurnRuntime,
            capability_for_tool,
            evaluate_tool_call,
        )

        pending = copy.deepcopy(self._pending_deferred)
        if not pending:
            raise ResidentError('deferred_resume:no_pending_tool')
        if self._tool_profile != TOOL_PROFILE_UH_A0:
            raise ResidentError('deferred_resume:tool_profile_not_uh_a0')
        if not isinstance(turn_lease, dict):
            raise ResidentError('deferred_resume:LEASE_MISMATCH')
        if turn_lease.get('issued_from') != 'user_confirmation':
            raise ResidentError('deferred_resume:LEASE_MISMATCH')
        if pending['approval_id'] not in tuple(turn_lease.get('approval_ids') or ()):
            raise ResidentError('deferred_resume:LEASE_MISMATCH')
        pending_tool_name = str(pending.get('tool_name') or '').strip()
        capability_id = capability_for_tool(pending_tool_name)
        entry = get_capability(capability_id) if capability_id else None
        raw_binding = ((entry or {}).get('provider_bindings') or {}).get('claude_code')
        if isinstance(raw_binding, str):
            current_bindings = {raw_binding.strip()} if raw_binding.strip() else set()
        elif isinstance(raw_binding, (list, tuple)):
            current_bindings = {
                value.strip()
                for value in raw_binding
                if isinstance(value, str) and value.strip()
            }
        else:
            current_bindings = set()
        if pending_tool_name not in current_bindings:
            self._pending_deferred = None
            raise ResidentError('deferred_resume:LEASE_MISMATCH')

        decision = evaluate_tool_call(
            pending['tool_name'],
            pending['tool_input'],
            turn_lease,
        )
        if decision.get('lease_decision') != 'ALLOW':
            raise ResidentError(
                'deferred_resume:' + str(decision.get('lease_decision') or 'LEASE_MISMATCH')
            )
        if self._alive():
            raise ResidentError('deferred_resume:process_still_alive')
        if not self._system_text:
            raise ResidentError('deferred_resume:missing_system_prompt')

        resume_env = dict(env or os.environ)
        self.spawn_resumable(
            self._system_text,
            resume_env,
            resume_session_id=pending['session_id'],
            tool_profile=TOOL_PROFILE_UH_A0,
            reason='deferred_resume',
        )
        runtime = turn_runtime
        if runtime is None:
            runtime = UH_A0TurnRuntime(
                getattr(
                    self, '_uh_a0_turn_lease_path',
                    '/opt/frontend/.uh-a0-current-turn-lease.json',
                ),
                session_id=pending['session_id'],
            )
        try:
            for event, payload in self.send_turn(
                content,
                commit_meta=commit_meta,
                on_stdin_flushed=on_stdin_flushed,
                idle_heartbeat_sec=idle_heartbeat_sec,
                turn_lease=turn_lease,
                turn_runtime=runtime,
            ):
                yield event, payload
                if (
                    event == 'tool_result'
                    and isinstance(payload, dict)
                    and payload.get('tool_use_id') == pending['tool_use_id']
                    and self._pending_deferred == pending
                ):
                    # The concrete write has been consumed; replaying the same
                    # approval must fail closed even if later text streaming fails.
                    self._pending_deferred = None
        except BaseException:
            self._kill(quiet=True)
            raise
        else:
            if self._pending_deferred == pending:
                self._pending_deferred = None

    def spawn_fresh_named(
        self,
        system_text,
        env,
        *,
        session_id,
        tool_profile=TOOL_PROFILE_LEGACY,
        reason='forge_fresh_named',
    ):
        """Fresh Claude with ``--session-id`` (never ``--resume``).

        Same process args / system prompt path as ``_spawn`` and
        ``spawn_resumable``; only the session naming flag differs. Callers must
        still supply the production system/persona/memory/state text — native
        cold cuts transcript carryover, not system injection.
        """
        import uuid as _uuid

        session_id = str(session_id or '').strip()
        if not session_id:
            raise ResidentError('session_id required')
        try:
            _uuid.UUID(session_id)
        except (TypeError, ValueError) as exc:
            raise ResidentError('session_id must be uuid') from exc
        from chat.cc_model import cc_model_snapshot
        from chat.cc_effort import cc_effort_snapshot
        from chat.cc_runtime import ClaudeRuntimeError, claude_cmd, require_pinned_claude_version
        with self._lock:
            if self._alive():
                raise ResidentError('staged spawn on live session')
            self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
            env = self._prepare_spawn_env(env)
            try:
                require_pinned_claude_version(env=env, cwd=self._cwd)
            except ClaudeRuntimeError as exc:
                raise ResidentError('claude_runtime:%s' % exc) from exc
            _model, model_identity, model_args = cc_model_snapshot()
            effort, effort_identity, effort_args = cc_effort_snapshot()
            tool_flags = self._build_spawn_tool_flags(env=env)
            surface_fingerprint = self._require_spawn_surface_fingerprint(tool_flags)
            base_args = claude_cmd(
                '-p',
                '--input-format', 'stream-json',
                '--output-format', 'stream-json',
                '--verbose',
                '--include-partial-messages',
                '--system-prompt', system_text,
                '--max-turns', '5',
                '--tools', tool_flags['tools'],
                '--thinking-display', 'summarized',
                '--exclude-dynamic-system-prompt-sections',
                '--session-id', session_id,
            ) + model_args + effort_args
            if '--resume' in base_args:
                raise ResidentError('fresh_named must not carry --resume')
            args = base_args + list(tool_flags['extra'])
            if '--resume' in args:
                raise ResidentError('fresh_named must not carry --resume')
            try:
                self._proc = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, cwd=self._cwd, env=env,
                )
            except Exception as exc:
                self._proc = None
                raise ResidentError('staged_spawn_failed:%s' % exc) from exc
            if surface_fingerprint is not None:
                self._bound_tool_surface_fingerprint = surface_fingerprint
            self._system_text = system_text
            self._model_identity = model_identity
            self._effort_identity = effort_identity
            self._effort_value = effort or None
            self._session_id = session_id
            self._cold = True
            self._generation += 1
            self._reset_session_meta(respawn_reason=reason)
            self._capture_tool_surface(tool_flags)
            return self

    def wait_staged_health(
        self,
        *,
        health_ms=None,
        jsonl_path=None,
        expected_sha256=None,
    ):
        """No-stdin health window for staged --resume process."""
        import config_store
        from tools.claude_forge_core import sha256_file

        if health_ms is None:
            try:
                health_ms = int(config_store.get_int('CONTEXT_SWITCH_STAGED_HEALTH_MS', 2000))
            except Exception:
                health_ms = 2000
        health_ms = max(0, int(health_ms))
        before = None
        if jsonl_path is not None:
            from pathlib import Path
            path = Path(jsonl_path)
            if not path.is_file():
                raise ResidentError('staged_spawn_failed:jsonl_missing')
            before = path.read_bytes()
            if expected_sha256 and sha256_file(path) != str(expected_sha256):
                raise ResidentError('staged_jsonl_mutated_before_handoff')

        deadline = time.time() + (health_ms / 1000.0)
        stderr_fatal_markers = (
            'error: ',
            'ENOENT',
            'Cannot resume',
            'session not found',
            'Invalid resume',
        )
        while time.time() < deadline:
            if not self._alive():
                err = ''
                try:
                    if self._proc and self._proc.stderr:
                        err = (self._proc.stderr.read() or '')[:2000]
                except Exception:
                    err = ''
                lower = err.lower()
                if any(m.lower() in lower for m in stderr_fatal_markers):
                    raise ResidentError('staged_stderr_fatal')
                raise ResidentError('staged_exited_during_health_window')
            # Non-blocking peek at stderr for fatal markers without consuming all.
            time.sleep(min(0.05, max(0.0, deadline - time.time())))

        if not self._alive():
            raise ResidentError('staged_exited_during_health_window')
        if jsonl_path is not None:
            from pathlib import Path
            path = Path(jsonl_path)
            after = path.read_bytes()
            if before != after:
                raise ResidentError('staged_jsonl_mutated_before_handoff')
            if expected_sha256 and sha256_file(path) != str(expected_sha256):
                raise ResidentError('staged_jsonl_mutated_before_handoff')
        return True

    def _commit_sent_context(self, commit_meta):
        """flush 后只提交仍存活 resident 内的游标；one-shot 不在这里消费。"""
        from chat.context_budget import merge_cumulative_state_send, normalize_known_file_refs
        if not commit_meta:
            return
        if commit_meta.get('rel_fingerprint'):
            self._last_rel_fingerprint = commit_meta['rel_fingerprint']
            self._turns_since_rel_sent = 0
            if 'rel_mood' in commit_meta:
                self._last_rel_mood = commit_meta.get('rel_mood')
        elif commit_meta.get('rel_tick'):
            # Only successful user turns that skipped relationship advance the cadence.
            self._turns_since_rel_sent += 1
        if 'state_snapshot' in commit_meta:
            self._last_state_snapshot = copy.deepcopy(commit_meta['state_snapshot'] or {})
        if 'state_send_snapshot' in commit_meta:
            if commit_meta.get('lean_state_reanchor'):
                self._last_state_send_snapshot = merge_cumulative_state_send(
                    {},
                    commit_meta['state_send_snapshot'] or {},
                )
            else:
                self._last_state_send_snapshot = merge_cumulative_state_send(
                    self._last_state_send_snapshot,
                    commit_meta['state_send_snapshot'] or {},
                )
        if 'lean_state_active' in commit_meta:
            self._last_successful_lean_state = bool(commit_meta['lean_state_active'])
        if commit_meta.get('lean_state_reanchor'):
            self._last_state_anchor_generation = self._generation
            self._last_state_schema_version = commit_meta.get('state_schema_version')
            self._turns_since_state_anchor = 0
            self._state_delta_chars_since_anchor = 0
            if commit_meta.get('state_version'):
                self._last_state_anchor_version = commit_meta['state_version']
        elif commit_meta.get('lean_state_active'):
            self._turns_since_state_anchor += 1
            self._state_delta_chars_since_anchor += int(
                commit_meta.get('state_context_chars') or 0
            )
        pending_files = commit_meta.get('file_inject_hashes')
        if pending_files is not None:
            self._committed_file_hashes = normalize_known_file_refs(pending_files)
            self._pending_file_hashes = set()
        if commit_meta.get('group_cursor_initialized'):
            self._group_cursor_initialized = True
            if commit_meta.get('group_max_id') is not None:
                self._last_group_message_id = int(commit_meta['group_max_id'])
        elif (
            self._group_cursor_initialized
            and commit_meta.get('group_max_id') is not None
        ):
            self._last_group_message_id = int(commit_meta['group_max_id'])

    @staticmethod
    def _extract_one_shot_claims(commit_meta):
        meta = commit_meta or {}
        return {
            'feedback_ids': list(meta.get('feedback_ids') or []),
            'dream_id': meta.get('dream_id'),
            'wake_ids': list(meta.get('wake_ids') or []),
        }

    def peek_idle_seconds(self):
        """在更新 _last_used 前计算 idle；从未成功用过时返回 None。"""
        if not self._last_used:
            return None
        return max(0.0, time.time() - float(self._last_used))

    def _maybe_set_session_id(self, data):
        if not isinstance(data, dict):
            return
        sid = data.get('session_id') or data.get('sessionId')
        if sid:
            self._session_id = str(sid)

    def _jsonl_replay_cursor(self, jsonl_cursor=None):
        from tools.cc_jsonl_usage import snapshot_session_jsonl

        replay_cursor = jsonl_cursor
        if self._session_id and replay_cursor is None:
            replay_cursor = snapshot_session_jsonl(self._cwd, self._session_id)
            # 冷启动首轮：session_id 在流结束后才出现，需从文件头回放。
            if replay_cursor is not None and self._cold:
                replay_cursor = dict(replay_cursor)
                replay_cursor['offset'] = 0
        return replay_cursor

    def _jsonl_usage_complete(self, merged):
        jsonl_usage = (merged or {}).get('jsonl_usage') or {}
        return jsonl_usage.get('stream_totals_match') is True

    @staticmethod
    def _with_jsonl_finality_state(merged, state):
        jsonl_usage = (merged or {}).get('jsonl_usage')
        if not isinstance(jsonl_usage, dict):
            return merged
        out = dict(merged)
        out['jsonl_usage'] = dict(jsonl_usage)
        out['jsonl_usage']['finality_state'] = state
        return out

    def _attach_jsonl_usage_with_retry(
        self,
        usage,
        jsonl_cursor=None,
        *,
        finality_profile=JSONL_FINALITY_PROFILE_DEFAULT,
    ):
        """Prove durable JSONL finality against one frozen session cursor.

        FINALITY_PENDING is only an intermediate state.  The caller may
        proceed only after stream_totals_match is exactly True; a bounded
        timeout returns the last non-final proof so existing fail-closed
        handling remains in force.
        """
        from tools.cc_jsonl_usage import attach_jsonl_usage, replay_session_jsonl

        if not self._session_id:
            return usage
        replay_cursor = self._jsonl_replay_cursor(jsonl_cursor)
        delays = (
            UNIFIED_NORMAL_WAKE_JSONL_FINALITY_RETRY_DELAYS
            if finality_profile == JSONL_FINALITY_PROFILE_UNIFIED_NORMAL_WAKE
            else JSONL_FINALITY_RETRY_DELAYS
        )
        last_replay = None
        last_merged = usage
        for delay in delays:
            if delay:
                time.sleep(delay)
            last_replay = replay_session_jsonl(
                self._cwd, self._session_id, cursor=replay_cursor,
            )
            last_merged = attach_jsonl_usage(usage, last_replay)
            if self._jsonl_usage_complete(last_merged):
                if finality_profile == JSONL_FINALITY_PROFILE_UNIFIED_NORMAL_WAKE:
                    return self._with_jsonl_finality_state(last_merged, 'FINAL')
                return last_merged
            has_usage = any(
                int(usage.get(key) or 0) > 0
                for key in ('cache_creation', 'input_tokens', 'output_tokens')
            )
            if not has_usage:
                break
            if finality_profile == JSONL_FINALITY_PROFILE_UNIFIED_NORMAL_WAKE:
                last_merged = self._with_jsonl_finality_state(
                    last_merged, 'FINALITY_PENDING',
                )
        return last_merged

    def send_turn(
        self,
        content,
        commit_meta=None,
        on_stdin_flushed=None,
        idle_heartbeat_sec=None,
        turn_lease=None,
        turn_runtime=None,
        jsonl_finality_profile=JSONL_FINALITY_PROFILE_DEFAULT,
        on_stdin_begin=None,
        diagnostic_wake_run_id=None,
    ):
        """Yield ('text'/'think'/'tool_use'/'tool_result'/'done', payload).

        done payload is (text, thinking, usage_dict, one_shot_claims).
        Flush 后只提交 state/group cursor；feedback/dream claims 经 done 带回，
        由 gateway 在 assistant 落库成功后再 consume。
        result.is_error / EOF / GeneratorExit（客户端断开）后 kill resident。

        ``on_stdin_flushed`` (optional): sync callback after successful
        ``stdin.write + flush``, before ``_commit_sent_context`` and any
        stdout read. Not called if write/flush fails. If the callback raises,
        the resident is killed and the exception is re-raised (message already sent).
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise ResidentError('resident 进程不存在，需要先 ensure_alive')

        uh_a0_runtime = None
        uh_a0_turn_id = None
        if self._tool_profile == TOOL_PROFILE_UH_A0:
            if turn_lease is None:
                raise ResidentError('uh_a0_turn_lease_required')
            from tools.execution_fence import UH_A0TurnRuntime
            uh_a0_runtime = turn_runtime
            if uh_a0_runtime is None:
                uh_a0_runtime = UH_A0TurnRuntime(
                    self._uh_a0_turn_lease_path,
                    session_id=self._session_id,
                )
            uh_a0_runtime.start_turn(turn_lease, session_id=self._session_id)
            uh_a0_turn_id = str(turn_lease['turn_id'])

        # 热 resident 从当前 EOF 开始；冷启动拿到 session_id 后从文件头回放。
        jsonl_cursor = None
        try:
            from tools.cc_jsonl_usage import snapshot_session_jsonl
            jsonl_cursor = snapshot_session_jsonl(self._cwd, self._session_id)
        except Exception:
            pass

        # 必须在更新 _last_used 前计算（成功路径末尾才写 _last_used）
        idle_seconds_before_turn = self.peek_idle_seconds()
        respawn_reason = self._pending_respawn_reason
        one_shot_claims = self._extract_one_shot_claims(commit_meta)
        # content may be str (text-only) or multimodal list (text + image blocks).
        # Validate before any stdin write so a bad vision turn never pollutes
        # the resident session / Claude transcript.
        try:
            from chat.cc_vision_bridge import (
                VisionBridgeError,
                assert_claude_user_content_safe,
            )
            assert_claude_user_content_safe(content)
        except VisionBridgeError as exc:
            if uh_a0_runtime is not None:
                uh_a0_runtime.abort_turn(turn_id=uh_a0_turn_id)
            raise ResidentError('vision input rejected: ' + str(exc)) from exc
        payload = json.dumps(
            {'type': 'user', 'message': {'role': 'user', 'content': content}},
            ensure_ascii=False,
        )
        if on_stdin_begin is not None:
            try:
                on_stdin_begin()
            except Exception:
                # Diagnostics must never change the provider send contract.
                pass
        try:
            proc.stdin.write(payload + NL)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            if uh_a0_runtime is not None:
                uh_a0_runtime.abort_turn(turn_id=uh_a0_turn_id)
            self._kill(quiet=True)
            raise ResidentError('resident 进程管道已断: ' + str(e))

        if on_stdin_flushed is not None:
            try:
                on_stdin_flushed()
            except BaseException:
                # Message already entered the pipe; fail closed — kill and re-raise.
                if uh_a0_runtime is not None:
                    uh_a0_runtime.abort_turn(turn_id=uh_a0_turn_id)
                self._kill(quiet=True)
                raise

        # 信已塞进门缝：立刻提交 resident 游标（即使后续流中断也不重复塞）
        try:
            self._commit_sent_context(commit_meta)
        except BaseException:
            if uh_a0_runtime is not None:
                uh_a0_runtime.abort_turn(turn_id=uh_a0_turn_id)
            raise

        timeout_reason = [None]  # 'stall' | 'hard'
        terminal_reason = [None]
        # Stall = inactivity; hard = absolute ceiling. Defaults stay 360 / 1800.
        stall_timeout = _cfg_int('CC_STREAM_TIMEOUT', CC_STREAM_TIMEOUT)
        hard_timeout = _cfg_int('CC_STREAM_HARD_TIMEOUT', CC_STREAM_HARD_TIMEOUT)
        result_grace = _cfg_int('CC_STREAM_RESULT_GRACE', CC_STREAM_RESULT_GRACE)
        terminal = ProviderTerminalTracker(result_grace)

        def _kill_on_timeout(reason):
            timeout_reason[0] = reason
            terminal_reason[0] = reason
            self._kill(quiet=True)

        watchdog = StreamWatchdog(
            stall_timeout=stall_timeout,
            hard_timeout=hard_timeout,
            on_timeout=_kill_on_timeout,
            is_proc_alive=lambda: proc.poll() is None,
        )
        watchdog.start()

        think_acc, text_acc = [], []
        rounds = []
        current_round = None
        is_err = None
        saw_result = False

        def _record_round_usage(
            event_source,
            event_usage,
            *,
            before,
            after,
            round_already_complete,
            provider_request_id=None,
        ):
            if event_source != 'result' and not event_usage:
                return
            if after is not None:
                round_index = after.get('round_index')
            elif before is not None:
                round_index = before.get('round_index')
            elif rounds:
                round_index = rounds[-1].get('index')
            else:
                round_index = None
            _log_wake_round_usage(
                diagnostic_wake_run_id=diagnostic_wake_run_id,
                turn_identity=terminal.turn_identity,
                round_index=round_index,
                event_source=event_source,
                usage=event_usage,
                current_round_before=before,
                current_round_after=after,
                round_already_complete=round_already_complete,
                provider_request_id=provider_request_id,
            )

        def _close_round(round_row, close_reason, *, complete):
            if round_row is None:
                return
            round_row['context_tokens'] = (
                int(round_row.get('input_tokens') or 0)
                + int(round_row.get('cache_read') or 0)
                + int(round_row.get('cache_creation') or 0)
            )
            round_row['complete'] = bool(complete)
            rounds.append(round_row)
            _log_wake_round_close(
                diagnostic_wake_run_id=diagnostic_wake_run_id,
                turn_identity=terminal.turn_identity,
                round_row=round_row,
                close_reason=close_reason,
            )
        use_idle_heartbeat = (
            idle_heartbeat_sec is not None and float(idle_heartbeat_sec) > 0
        )
        heartbeat_interval = float(idle_heartbeat_sec) if use_idle_heartbeat else 0.0
        try:
            try:
                while True:
                    # A provider end_turn is not a successful terminal event. Give
                    # Claude a bounded chance to emit the authoritative result.
                    if terminal.grace_expired():
                        terminal_reason[0] = 'result_missing_after_end_turn'
                        self._kill(quiet=True)
                        break
                    # Keep the established blocking readline contract for
                    # ordinary turns. Poll only when a timer must wake us for
                    # synthetic heartbeats or the post-end_turn result grace.
                    polling_required = use_idle_heartbeat or terminal.end_turn_seen
                    ready = True
                    if polling_required:
                        poll_interval = heartbeat_interval if use_idle_heartbeat else 0.2
                        try:
                            ready, _, _ = select.select(
                                [proc.stdout], [], [], poll_interval,
                            )
                        except (AttributeError, OSError, TypeError, ValueError):
                            # In-memory StringIO fixtures have no file descriptor.
                            # They remain on direct readline semantics; real pipes
                            # take the polling path above.
                            ready = True
                    if not ready:
                        if proc.poll() is not None:
                            break
                        if terminal.grace_expired():
                            terminal_reason[0] = 'result_missing_after_end_turn'
                            self._kill(quiet=True)
                            break
                        if use_idle_heartbeat:
                            # Synthetic SSE heartbeat — not Claude activity.
                            yield ('heartbeat', None)
                        continue
                    raw_line = proc.stdout.readline()
                    if raw_line == '':
                        break
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    # Refresh stall deadline before heavier event handling so a
                    # concurrent watchdog knock observes the new activity.
                    terminal.observe(d)
                    if _claude_event_is_activity(d):
                        watchdog.note_activity()
                    self._maybe_set_session_id(d)
                    t = d.get('type')
                    if t == 'system' and d.get('subtype') == 'init':
                        self._session_id = d.get('session_id') or self._session_id
                    elif t == 'stream_event':
                        ev = d.get('event') or {}
                        ev_type = ev.get('type')
                        if ev_type == 'message_start':
                            before_round = _diagnostic_round_snapshot(current_round)
                            if current_round is not None:
                                _close_round(
                                    current_round,
                                    'next_message_start',
                                    complete=True,
                                )
                            current_round = {
                                'index': len(rounds) + 1,
                                'complete': False,
                                'input_tokens': 0,
                                'output_tokens': 0,
                                'cache_read': 0,
                                'cache_creation': 0,
                                'context_tokens': 0,
                            }
                            u = (ev.get('message') or {}).get('usage') or {}
                            if u:
                                current_round['input_tokens'] = max(
                                    current_round['input_tokens'], int(u.get('input_tokens') or 0)
                                )
                                current_round['output_tokens'] = max(
                                    current_round['output_tokens'], int(u.get('output_tokens') or 0)
                                )
                                current_round['cache_read'] = max(
                                    current_round['cache_read'], int(u.get('cache_read_input_tokens') or 0)
                                )
                                current_round['cache_creation'] = max(
                                    current_round['cache_creation'], int(u.get('cache_creation_input_tokens') or 0)
                                )
                                _record_round_usage(
                                    'message_start',
                                    u,
                                    before=before_round,
                                    after=_diagnostic_round_snapshot(current_round),
                                    round_already_complete=False,
                                    provider_request_id=_diagnostic_request_id(d, ev),
                                )
                        elif ev_type == 'message_delta':
                            before_round = _diagnostic_round_snapshot(current_round)
                            u = ev.get('usage') or {}
                            if current_round is not None and u:
                                current_round['input_tokens'] = max(
                                    current_round['input_tokens'], int(u.get('input_tokens') or 0)
                                )
                                current_round['output_tokens'] = max(
                                    current_round['output_tokens'], int(u.get('output_tokens') or 0)
                                )
                                current_round['cache_read'] = max(
                                    current_round['cache_read'], int(u.get('cache_read_input_tokens') or 0)
                                )
                                current_round['cache_creation'] = max(
                                    current_round['cache_creation'], int(u.get('cache_creation_input_tokens') or 0)
                                )
                            if u:
                                _record_round_usage(
                                    'message_delta',
                                    u,
                                    before=before_round,
                                    after=_diagnostic_round_snapshot(current_round),
                                    round_already_complete=(
                                        bool(before_round and before_round.get('complete'))
                                        or (before_round is None and bool(rounds))
                                    ),
                                    provider_request_id=_diagnostic_request_id(d, ev),
                                )
                        elif ev_type == 'content_block_delta':
                            delta = ev.get('delta') or {}
                            if delta.get('type') == 'text_delta':
                                chunk = delta.get('text', '')
                                if chunk:
                                    text_acc.append(chunk)
                                    yield ('text', chunk)
                            elif delta.get('type') == 'thinking_delta':
                                chunk = delta.get('thinking', '')
                                if chunk:
                                    think_acc.append(chunk)
                                    yield ('think', chunk)
                    elif t == 'assistant':
                        before_round = _diagnostic_round_snapshot(current_round)
                        created_round = current_round is None
                        msg = d.get('message') or {}
                        u = msg.get('usage') or {}
                        if current_round is None:
                            current_round = {
                                'index': len(rounds) + 1,
                                'complete': False,
                                'input_tokens': 0,
                                'output_tokens': 0,
                                'cache_read': 0,
                                'cache_creation': 0,
                                'context_tokens': 0,
                            }
                        if u:
                            current_round['input_tokens'] = max(
                                current_round['input_tokens'], int(u.get('input_tokens') or 0)
                            )
                            current_round['output_tokens'] = max(
                                current_round['output_tokens'], int(u.get('output_tokens') or 0)
                            )
                            current_round['cache_read'] = max(
                                current_round['cache_read'], int(u.get('cache_read_input_tokens') or 0)
                            )
                            current_round['cache_creation'] = max(
                                current_round['cache_creation'], int(u.get('cache_creation_input_tokens') or 0)
                            )
                            _record_round_usage(
                                'assistant',
                                u,
                                before=before_round,
                                after=_diagnostic_round_snapshot(current_round),
                                round_already_complete=(
                                    False
                                    if created_round
                                    else bool(before_round and before_round.get('complete'))
                                ),
                                provider_request_id=_diagnostic_request_id(d),
                            )
                        for b in (msg.get('content') or []):
                            if isinstance(b, dict) and b.get('type') == 'tool_use':
                                if current_round is not None and not current_round.get('complete'):
                                    _close_round(
                                        current_round,
                                        'assistant_tool_use',
                                        complete=True,
                                    )
                                    current_round = None
                                tool_payload = {
                                    'id': b.get('id'),
                                    'name': b.get('name', ''),
                                    'args': b.get('input') or {},
                                }
                                if uh_a0_runtime is not None:
                                    fence = uh_a0_runtime.evaluate(
                                        tool_payload['name'], tool_payload['args'],
                                    )
                                    tool_payload.update({
                                        'capability_id': fence.get('capability_id'),
                                        'lease_decision': fence.get('lease_decision'),
                                    })
                                    if fence.get('approval_id'):
                                        tool_payload['approval_id'] = fence['approval_id']
                                yield ('tool_use', tool_payload)
                    elif t == 'user':
                        for b in ((d.get('message') or {}).get('content') or []):
                            if isinstance(b, dict) and b.get('type') == 'tool_result':
                                rc = b.get('content')
                                if isinstance(rc, list):
                                    rc = ''.join(x.get('text', '') for x in rc if isinstance(x, dict))
                                yield ('tool_result', {
                                    'tool_use_id': b.get('tool_use_id'),
                                    'result': str(rc or ''),
                                    'is_error': bool(b.get('is_error')),
                                })
                    elif t == 'result':
                        saw_result = True
                        result_usage = d.get('usage')
                        if not isinstance(result_usage, dict):
                            result_usage = {}
                        before_round = _diagnostic_round_snapshot(current_round)
                        _record_round_usage(
                            'result',
                            result_usage,
                            before=before_round,
                            after=_diagnostic_round_snapshot(current_round),
                            round_already_complete=(
                                bool(before_round and before_round.get('complete'))
                                or (before_round is None and bool(rounds))
                            ),
                            provider_request_id=_diagnostic_request_id(d),
                        )
                        if (
                            uh_a0_runtime is not None
                            and d.get('stop_reason') == 'tool_deferred'
                        ):
                            # The result is the sole authoritative source for
                            # pending identity; capture it before exposing state.
                            pending = self._capture_authoritative_deferred(d)
                            uh_a0_runtime.end_turn(turn_id=uh_a0_turn_id)
                            from tools.execution_fence import (
                                approval_prompt,
                                read_current_turn_lease,
                            )
                            if read_current_turn_lease(uh_a0_runtime.path)[0] is not None:
                                self._pending_deferred = None
                                raise ResidentError('tool_deferred_lease_not_cleared')
                            deferred_payload = {
                                'id': pending['tool_use_id'],
                                'name': pending['tool_name'],
                                'args': copy.deepcopy(pending['tool_input']),
                                'tool_input': copy.deepcopy(pending['tool_input']),
                                'approval_id': pending['approval_id'],
                                'deferred_tool_use': True,
                                'status': 'waiting_for_confirmation',
                            }
                            prompt = approval_prompt(
                                pending['tool_name'], pending['tool_input'],
                            )
                            if prompt is not None:
                                deferred_payload['approval_prompt'] = prompt
                            self._kill(quiet=True)
                            yield ('tool_use', deferred_payload)
                        if d.get('is_error'):
                            is_err = str(d.get('result', ''))[:300]
                        # result.usage is diagnostics only; it never updates
                        # or replaces the stream round totals.
                        if current_round is not None:
                            _close_round(
                                current_round,
                                'provider_result',
                                complete=not bool(d.get('is_error')),
                            )
                            current_round = None
                        break
            except GeneratorExit:
                # 客户端断开 SSE：必须 kill，否则残留 stdout 会污染下一轮
                self._kill(quiet=True)
                raise
            except BaseException:
                # Watchdog may close pipes mid-read; prefer typed timeout errors.
                if timeout_reason[0] is not None:
                    pass
                else:
                    if self._proc is not None:
                        self._kill(quiet=True)
                    raise
        finally:
            watchdog.stop()
            if uh_a0_runtime is not None:
                uh_a0_runtime.end_turn(turn_id=uh_a0_turn_id)

        if current_round is not None:
            _close_round(current_round, 'loop_finalizer', complete=False)
            current_round = None

        usage = summarize_rounds(
            rounds,
            resident_turn_count=self._resident_turn_count + 1,
            respawn_reason=respawn_reason,
            max_round_context=self._max_round_context,
        )
        # 观测辅助字段：不改变既有 Usage v2 公开语义，供 gateway 组装 runtime
        usage['_obs_idle_seconds_before_turn'] = idle_seconds_before_turn
        usage['_obs_resident_generation'] = self._generation
        usage['_obs_resident_pid'] = getattr(proc, 'pid', None)
        usage['_obs_claude_session_id'] = self._session_id
        usage['_obs_last_provider_event_type'] = terminal.last_provider_event_type
        usage['_obs_last_provider_activity_at'] = terminal.last_provider_activity_at
        usage['_obs_end_turn_seen'] = bool(terminal.end_turn_seen)
        usage['_obs_end_turn_seen_at'] = terminal.end_turn_seen_at
        usage['_obs_result_seen'] = bool(terminal.result_seen)
        usage['_obs_terminal_reason'] = terminal_reason[0] or terminal.terminal_reason
        usage['_obs_turn_identity'] = terminal.turn_identity
        usage['_obs_effort'] = getattr(self, '_effort_value', None)
        usage['_obs_keepwarm_lease_expires_at'] = self._keepwarm_lease_expires_at
        surface = self._tool_surface_snapshot or {}
        usage['_obs_tool_schema_sha256'] = surface.get('tool_schema_sha256')
        usage['_obs_tool_schema_text'] = surface.get('tool_schema_text')
        usage['_obs_tool_schema_source'] = surface.get('tool_schema_source')
        usage['_obs_tool_schema_measurement_status'] = surface.get(
            'tool_schema_measurement_status'
        )
        usage['_obs_tool_count'] = surface.get('tool_count')

        if timeout_reason[0] == 'stall':
            raise ResidentError(
                'claude code 长时间无活动 (%ds)，resident 进程已重启' % stall_timeout,
                usage=usage,
                diagnostics=terminal.snapshot(terminal_reason='stall'),
                error_code='provider_stall_timeout',
            )
        if timeout_reason[0] == 'hard':
            raise ResidentError(
                'claude code 单轮超过绝对上限 (%ds)，resident 进程已重启' % hard_timeout,
                usage=usage,
                diagnostics=terminal.snapshot(terminal_reason='hard_timeout'),
                error_code='provider_hard_timeout',
            )
        if terminal_reason[0] == 'result_missing_after_end_turn':
            self._kill(quiet=True)
            diagnostics = terminal.snapshot(
                terminal_reason='result_missing_after_end_turn',
            )
            diagnostics.update({
                'resident_generation': self._generation,
                'resident_pid': getattr(proc, 'pid', None),
                'claude_session_id': self._session_id,
            })
            raise ResidentError(
                '回复收尾异常：provider 已报告 end_turn，但 result 未到达',
                usage=usage,
                diagnostics=diagnostics,
                error_code='result_missing_after_end_turn',
            )
        if not saw_result:
            self._kill(quiet=True)
            diagnostics = terminal.snapshot(terminal_reason='result_missing_before_terminal')
            diagnostics.update({
                'resident_generation': self._generation,
                'resident_pid': getattr(proc, 'pid', None),
                'claude_session_id': self._session_id,
            })
            raise ResidentError(
                'resident 进程在本轮回复完成前退出',
                usage=usage,
                diagnostics=diagnostics,
                error_code='result_missing_before_terminal',
            )
        if is_err:
            # payload 已写入 resident：kill 强制下一轮冷启动，避免脏会话继续热轮
            self._kill(quiet=True)
            raise ResidentError('claude code 返回错误: ' + is_err, usage=usage)

        # Only a successful authoritative provider terminal may enter JSONL
        # durable-finality proof.  Terminal failures deliberately skip replay;
        # in particular, Wake's extended profile must never mask a missing
        # result, stall, hard timeout, or provider error.
        try:
            if jsonl_finality_profile == JSONL_FINALITY_PROFILE_DEFAULT:
                usage = self._attach_jsonl_usage_with_retry(usage, jsonl_cursor)
            else:
                usage = self._attach_jsonl_usage_with_retry(
                    usage,
                    jsonl_cursor,
                    finality_profile=jsonl_finality_profile,
                )
        except Exception:
            pass

        self._cold = False
        self._last_used = time.time()
        self._resident_turn_count += 1
        self._turns_since_respawn += 1
        self._pending_respawn_reason = None
        self._last_round_context = int(usage.get('last_round_context') or 0)
        self._max_round_context = max(self._max_round_context, int(usage.get('max_round_context') or 0))
        usage['resident_turn_count'] = self._resident_turn_count
        usage['max_round_context'] = self._max_round_context
        # claims 只经 done 内部回传，不得写入公开 cache_info
        yield ('done', (''.join(text_acc).strip(), ''.join(think_acc), usage, one_shot_claims))

    def is_cold(self):
        return self._cold

    @property
    def last_state_snapshot(self):
        return self._last_state_snapshot

    @property
    def last_state_send_snapshot(self):
        return self._last_state_send_snapshot

    @property
    def last_successful_lean_state(self):
        return self._last_successful_lean_state

    @property
    def last_state_anchor_generation(self):
        return self._last_state_anchor_generation

    @property
    def last_state_schema_version(self):
        return self._last_state_schema_version

    @property
    def turns_since_state_anchor(self):
        return self._turns_since_state_anchor

    @property
    def state_delta_chars_since_anchor(self):
        return self._state_delta_chars_since_anchor

    @property
    def last_state_anchor_version(self):
        return self._last_state_anchor_version

    @property
    def committed_file_hashes(self):
        return set(self._committed_file_hashes)

    @property
    def last_group_message_id(self):
        return self._last_group_message_id

    @property
    def group_cursor_initialized(self):
        return self._group_cursor_initialized

    @property
    def last_rel_fingerprint(self):
        return self._last_rel_fingerprint

    @property
    def turns_since_rel_sent(self):
        return self._turns_since_rel_sent

    @property
    def last_rel_mood(self):
        return self._last_rel_mood

    @property
    def pending_respawn_reason(self):
        return self._pending_respawn_reason

    @property
    def last_round_context(self):
        return int(self._last_round_context or 0)

    @property
    def turns_since_respawn(self):
        return int(self._turns_since_respawn or 0)

    @property
    def hard_context_pre_spawn_turns(self):
        """Turns since respawn when ``hard_context`` was last decided, or None."""
        val = self._hard_context_pre_spawn_turns
        return None if val is None else int(val)

    @property
    def last_cold_bootstrap_estimate(self):
        """Whole-prompt estimate of the most recent cold bootstrap sent.

        Used only to gate an *immediate* post-cold ``hard_context`` storm
        (Fence C) when paired with ``last_cold_bootstrap_generation``.
        """
        return self._last_cold_bootstrap_estimate

    @property
    def last_cold_bootstrap_generation(self):
        """Resident generation that produced ``last_cold_bootstrap_estimate``."""
        return int(self._last_cold_bootstrap_generation or 0)

    def note_cold_bootstrap_estimate(self, estimate):
        """Record the whole-prompt estimate for the cold bootstrap about to
        be sent. Called before ``send_turn`` so a failure to send still
        updates the baseline (the content was decided regardless)."""
        try:
            self._last_cold_bootstrap_estimate = max(0, int(estimate or 0))
            self._last_cold_bootstrap_generation = int(self._generation or 0)
        except (TypeError, ValueError):
            pass

    def clear_hard_context_pre_spawn_turns(self):
        self._hard_context_pre_spawn_turns = None

    @property
    def pending_deferred(self):
        return copy.deepcopy(self._pending_deferred)

    @property
    def tool_profile(self):
        return self._tool_profile

    @property
    def model_identity(self):
        return getattr(self, '_model_identity', None)

    @property
    def effort_identity(self):
        return getattr(self, '_effort_identity', None)

    @property
    def generation(self):
        return self._generation

    @property
    def session_id(self):
        return self._session_id

    @property
    def cwd(self):
        return self._cwd

    @property
    def system_text(self):
        return self._system_text

    @property
    def allowed_tools(self):
        return self._allowed_tools

    @property
    def mcp_config_path(self):
        return self._mcp_config_path

    @property
    def bound_tool_surface_fingerprint(self):
        """Fingerprint bound to the currently alive process generation, if trusted."""
        return str(
            getattr(self, '_bound_tool_surface_fingerprint', None) or ''
        ) or None

    @property
    def tool_surface_snapshot(self):
        return self._tool_surface_snapshot or {}

    @property
    def keepwarm_lease_expires_at(self):
        return self._keepwarm_lease_expires_at

    def set_keepwarm_lease_expires_at(self, value):
        """UH-A will write authoritative keepwarm lease expiry (ISO-8601)."""
        self._keepwarm_lease_expires_at = value

    @property
    def resident_pid(self):
        proc = self._proc
        return getattr(proc, 'pid', None) if proc is not None else None

    def shutdown(self):
        with self._lock:
            self._kill()

    def invalidate_for_history_rewrite(self, reason='history_rewrite'):
        """Terminate the old-world resident and clear its reusable identity."""
        with self._lock:
            self._kill(quiet=True)
            self._system_text = None
            self._session_id = None
            self._model_identity = None
            self._effort_identity = None
            self._effort_value = None
            self._cold = True
            self._next_spawn_reason = str(reason or 'history_rewrite')
            self._reset_session_meta(respawn_reason=self._next_spawn_reason)
