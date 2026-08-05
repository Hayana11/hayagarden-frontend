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
import select
import subprocess
import threading
import time

import config_store

NL = chr(10)
CC_STREAM_TIMEOUT = 360  # stall / inactivity seconds (runtime-tunable)
CC_STREAM_HARD_TIMEOUT = 1800  # absolute per-turn ceiling (runtime-tunable)
IDLE_REAP_SECONDS = 3 * 60 * 60
TOOL_PROFILE_LEGACY = 'legacy'
TOOL_PROFILE_TEXT_ONLY = 'text_only'

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
    def __init__(self, message, *, usage=None):
        super().__init__(message)
        self.usage = usage or empty_usage()


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
        # MODEL-1B: identity of the model argv this process was started with.
        self._model_identity = None
        # Durable history-rewrite epoch bound at last successful spawn.
        # Compared against the cross-process epoch before hot reuse so every
        # gunicorn worker lazily invalidates after a rewrite, even when the
        # app→gateway bridge only eagers one worker.
        self._history_rewrite_epoch = ''
        self._reset_session_meta(respawn_reason=None)

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

    def _spawn(self, system_text, env, *, reason='process_dead', tool_profile=TOOL_PROFILE_LEGACY):
        from chat.cc_model import cc_model_snapshot
        self._kill(quiet=True)
        self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
        _model, model_identity, model_args = cc_model_snapshot()
        base_args = [
            'claude', '-p',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            '--include-partial-messages',
            '--system-prompt', system_text,
            '--max-turns', '5',
            '--tools', '',
            '--thinking-display', 'summarized',
            '--exclude-dynamic-system-prompt-sections',
        ] + model_args
        if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
            args = base_args + ['--allowedTools', '']
        else:
            args = base_args + [
                '--mcp-config', self._mcp_config_path,
                '--strict-mcp-config',
                '--allowedTools', self._allowed_tools,
            ]
        self._proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=self._cwd, env=env,
        )
        # Bind epoch only after a successful spawn. A failed Popen must leave
        # the prior (stale) binding so hot reuse stays forbidden.
        try:
            from chat.cc_history_rewrite import current_history_rewrite_epoch
            bound_epoch = current_history_rewrite_epoch()
        except Exception:
            bound_epoch = '__unreadable__'
        self._history_rewrite_epoch = bound_epoch
        self._system_text = system_text
        self._model_identity = model_identity
        self._session_id = None
        self._cold = True
        self._generation += 1
        self._next_spawn_reason = None
        self._reset_session_meta(respawn_reason=reason)
        if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
            self._tool_surface_snapshot = {}
        else:
            try:
                from tools.cc_tool_surface import capture_tool_surface_snapshot
                self._tool_surface_snapshot = capture_tool_surface_snapshot(
                    self._allowed_tools,
                    mcp_config_path=self._mcp_config_path,
                )
            except Exception:
                self._tool_surface_snapshot = {}

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
        # Durable rewrite epoch: any resident spawned before the latest
        # committed rewrite loses hot-reuse on every worker, lazily.
        try:
            from chat.cc_history_rewrite import current_history_rewrite_epoch
            durable_epoch = current_history_rewrite_epoch()
        except Exception:
            durable_epoch = '__unreadable__'
        bound_epoch = getattr(self, '_history_rewrite_epoch', None) or ''
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
        if (time.time() - self._last_used) > IDLE_REAP_SECONDS:
            return 'idle'
        if system_text != self._system_text:
            return 'system_changed'

        hard = _cfg_int('CC_CONTEXT_HARD_LIMIT', 120_000)
        soft = _cfg_int('CC_CONTEXT_SOFT_LIMIT', 90_000)
        max_turns = _cfg_int('CC_MAX_RESIDENT_TURNS', 30)
        min_between = _cfg_int('CC_MIN_TURNS_BETWEEN_RESPAWNS', 5)

        if self._last_round_context >= hard:
            return 'hard_context'
        if self._resident_turn_count >= max_turns:
            return 'turn_limit'
        if (
            self._last_round_context >= soft
            and self._turns_since_respawn >= min_between
        ):
            return 'soft_context'
        return None

    def ensure_alive(self, system_text, env, *, tool_profile=TOOL_PROFILE_LEGACY):
        with self._lock:
            reason = self._decide_respawn_reason(system_text, tool_profile=tool_profile)
            if reason:
                if reason == 'history_rewrite':
                    self._system_text = None
                    self._session_id = None
                    self._model_identity = None
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
        with self._lock:
            if self._alive():
                raise ResidentError('staged spawn on live session')
            self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
            _model, model_identity, model_args = cc_model_snapshot()
            base_args = [
                'claude', '-p',
                '--input-format', 'stream-json',
                '--output-format', 'stream-json',
                '--verbose',
                '--include-partial-messages',
                '--system-prompt', system_text,
                '--max-turns', '5',
                '--tools', '',
                '--thinking-display', 'summarized',
                '--exclude-dynamic-system-prompt-sections',
                '--resume', resume_session_id,
            ] + model_args
            if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
                args = base_args + ['--allowedTools', '']
            else:
                args = base_args + [
                    '--mcp-config', self._mcp_config_path,
                    '--strict-mcp-config',
                    '--allowedTools', self._allowed_tools,
                ]
            try:
                self._proc = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, cwd=self._cwd, env=env,
                )
            except Exception as exc:
                self._proc = None
                raise ResidentError('staged_spawn_failed:%s' % exc) from exc
            try:
                from chat.cc_history_rewrite import current_history_rewrite_epoch
                self._history_rewrite_epoch = current_history_rewrite_epoch()
            except Exception:
                self._history_rewrite_epoch = '__unreadable__'
            self._system_text = system_text
            self._model_identity = model_identity
            self._session_id = resume_session_id
            self._cold = False
            self._generation += 1
            self._reset_session_meta(respawn_reason=reason)
            if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
                self._tool_surface_snapshot = {}
            else:
                try:
                    from tools.cc_tool_surface import capture_tool_surface_snapshot
                    self._tool_surface_snapshot = capture_tool_surface_snapshot(
                        self._allowed_tools,
                        mcp_config_path=self._mcp_config_path,
                    )
                except Exception:
                    self._tool_surface_snapshot = {}
            return self

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
        with self._lock:
            if self._alive():
                raise ResidentError('staged spawn on live session')
            self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
            _model, model_identity, model_args = cc_model_snapshot()
            base_args = [
                'claude', '-p',
                '--input-format', 'stream-json',
                '--output-format', 'stream-json',
                '--verbose',
                '--include-partial-messages',
                '--system-prompt', system_text,
                '--max-turns', '5',
                '--tools', '',
                '--thinking-display', 'summarized',
                '--exclude-dynamic-system-prompt-sections',
                '--session-id', session_id,
            ] + model_args
            if '--resume' in base_args:
                raise ResidentError('fresh_named must not carry --resume')
            if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
                args = base_args + ['--allowedTools', '']
            else:
                args = base_args + [
                    '--mcp-config', self._mcp_config_path,
                    '--strict-mcp-config',
                    '--allowedTools', self._allowed_tools,
                ]
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
            self._system_text = system_text
            self._model_identity = model_identity
            self._session_id = session_id
            self._cold = True
            self._generation += 1
            self._reset_session_meta(respawn_reason=reason)
            if self._tool_profile == TOOL_PROFILE_TEXT_ONLY:
                self._tool_surface_snapshot = {}
            else:
                try:
                    from tools.cc_tool_surface import capture_tool_surface_snapshot
                    self._tool_surface_snapshot = capture_tool_surface_snapshot(
                        self._allowed_tools,
                        mcp_config_path=self._mcp_config_path,
                    )
                except Exception:
                    self._tool_surface_snapshot = {}
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

    def _attach_jsonl_usage_with_retry(self, usage, jsonl_cursor=None):
        """JSONL 落盘可能略晚于 stdout；短退避重试直到 stream totals 对齐。"""
        from tools.cc_jsonl_usage import attach_jsonl_usage, replay_session_jsonl

        if not self._session_id:
            return usage
        replay_cursor = self._jsonl_replay_cursor(jsonl_cursor)
        delays = (0.0, 0.05, 0.15, 0.35)
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
                return last_merged
            has_usage = any(
                int(usage.get(key) or 0) > 0
                for key in ('cache_creation', 'input_tokens', 'output_tokens')
            )
            if not has_usage:
                break
        return last_merged

    def send_turn(
        self,
        content,
        commit_meta=None,
        on_stdin_flushed=None,
        idle_heartbeat_sec=None,
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
        payload = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}, ensure_ascii=False)
        try:
            proc.stdin.write(payload + NL)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._kill(quiet=True)
            raise ResidentError('resident 进程管道已断: ' + str(e))

        if on_stdin_flushed is not None:
            try:
                on_stdin_flushed()
            except BaseException:
                # Message already entered the pipe; fail closed — kill and re-raise.
                self._kill(quiet=True)
                raise

        # 信已塞进门缝：立刻提交 resident 游标（即使后续流中断也不重复塞）
        self._commit_sent_context(commit_meta)

        timeout_reason = [None]  # 'stall' | 'hard'
        # Stall = inactivity; hard = absolute ceiling. Defaults stay 360 / 1800.
        stall_timeout = _cfg_int('CC_STREAM_TIMEOUT', CC_STREAM_TIMEOUT)
        hard_timeout = _cfg_int('CC_STREAM_HARD_TIMEOUT', CC_STREAM_HARD_TIMEOUT)

        def _kill_on_timeout(reason):
            timeout_reason[0] = reason
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
        use_idle_heartbeat = (
            idle_heartbeat_sec is not None and float(idle_heartbeat_sec) > 0
        )
        heartbeat_interval = float(idle_heartbeat_sec) if use_idle_heartbeat else 0.0
        try:
            try:
                while True:
                    if use_idle_heartbeat:
                        ready, _, _ = select.select(
                            [proc.stdout], [], [], heartbeat_interval,
                        )
                        if not ready:
                            if proc.poll() is not None:
                                break
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
                            if current_round is not None:
                                current_round['context_tokens'] = (
                                    int(current_round.get('input_tokens') or 0)
                                    + int(current_round.get('cache_read') or 0)
                                    + int(current_round.get('cache_creation') or 0)
                                )
                                current_round['complete'] = True
                                rounds.append(current_round)
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
                        elif ev_type == 'message_delta':
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
                        for b in (msg.get('content') or []):
                            if isinstance(b, dict) and b.get('type') == 'tool_use':
                                if current_round is not None and not current_round.get('complete'):
                                    current_round['context_tokens'] = (
                                        int(current_round.get('input_tokens') or 0)
                                        + int(current_round.get('cache_read') or 0)
                                        + int(current_round.get('cache_creation') or 0)
                                    )
                                    current_round['complete'] = True
                                    rounds.append(current_round)
                                    current_round = None
                                yield ('tool_use', {
                                    'id': b.get('id'),
                                    'name': b.get('name', ''),
                                    'args': b.get('input') or {},
                                })
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
                        if d.get('is_error'):
                            is_err = str(d.get('result', ''))[:300]
                        # result.usage 只做校验/fallback，不覆盖已解析的 rounds
                        if current_round is not None:
                            current_round['context_tokens'] = (
                                int(current_round.get('input_tokens') or 0)
                                + int(current_round.get('cache_read') or 0)
                                + int(current_round.get('cache_creation') or 0)
                            )
                            current_round['complete'] = not bool(d.get('is_error'))
                            rounds.append(current_round)
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

        if current_round is not None:
            current_round['context_tokens'] = (
                int(current_round.get('input_tokens') or 0)
                + int(current_round.get('cache_read') or 0)
                + int(current_round.get('cache_creation') or 0)
            )
            current_round['complete'] = False
            rounds.append(current_round)

        usage = summarize_rounds(
            rounds,
            resident_turn_count=self._resident_turn_count + 1,
            respawn_reason=respawn_reason,
            max_round_context=self._max_round_context,
        )
        # JSONL 只补 request identity / TTL bucket / model；stream totals 保持权威。
        try:
            usage = self._attach_jsonl_usage_with_retry(usage, jsonl_cursor)
        except Exception:
            pass
        # 观测辅助字段：不改变既有 Usage v2 公开语义，供 gateway 组装 runtime
        usage['_obs_idle_seconds_before_turn'] = idle_seconds_before_turn
        usage['_obs_resident_generation'] = self._generation
        usage['_obs_resident_pid'] = getattr(proc, 'pid', None)
        usage['_obs_claude_session_id'] = self._session_id
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
            )
        if timeout_reason[0] == 'hard':
            raise ResidentError(
                'claude code 单轮超过绝对上限 (%ds)，resident 进程已重启' % hard_timeout,
                usage=usage,
            )
        if not saw_result:
            self._kill(quiet=True)
            raise ResidentError('resident 进程在本轮回复完成前退出', usage=usage)
        if is_err:
            # payload 已写入 resident：kill 强制下一轮冷启动，避免脏会话继续热轮
            self._kill(quiet=True)
            raise ResidentError('claude code 返回错误: ' + is_err, usage=usage)

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
    def tool_profile(self):
        return self._tool_profile

    @property
    def model_identity(self):
        return getattr(self, '_model_identity', None)

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
            self._cold = True
            self._next_spawn_reason = str(reason or 'history_rewrite')
            self._reset_session_meta(respawn_reason=self._next_spawn_reason)
