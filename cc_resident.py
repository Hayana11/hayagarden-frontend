"""Persistent (resident) Claude Code subprocess for Fyodor's solo chat.

Cold start sends persona/history/full state once. Hot turns only send the new
user message, recall, changed state components, and new group-chat rows.
State/group cursors commit after stdin write+flush. Feedback/dream claims are
returned on done and consumed only after assistant DB persistence.
Client disconnect (GeneratorExit) kills the resident to avoid stdout pollution.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

import config_store

NL = chr(10)
CC_STREAM_TIMEOUT = 360  # stall / inactivity seconds (runtime-tunable)
CC_STREAM_HARD_TIMEOUT = 1800  # absolute per-turn ceiling (runtime-tunable)
CC_STREAM_RESULT_GRACE = 30  # wait for result after provider end_turn (runtime-tunable)
CC_TERMINAL_RECOVERY_PROBE = 20  # durable-completion probe before ordinary stall
CC_TERMINAL_RECOVERY_EOF_GRACE = 4
CC_TERMINAL_RECOVERY_SIGTERM_GRACE = 4
IDLE_REAP_SECONDS = 3 * 60 * 60
STALE_CACHE_CONTEXT_THRESHOLD = 70_000
STALE_CACHE_MAX_AGE_SECONDS = 3_300
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


_TIMEOUT_DIAGNOSTIC_MAX_CHARS = 8_000
_TIMEOUT_DIAGNOSTIC_TRANSCRIPT_SCAN_BYTES = 64 * 1024


def _bounded_diagnostic_text(value, limit=_TIMEOUT_DIAGNOSTIC_MAX_CHARS):
    """Return bounded diagnostic text without accepting arbitrary payloads."""
    if value is None:
        return None
    return str(value)[:int(limit)]


def _diagnostic_path_hash(path):
    if not path:
        return None
    try:
        return hashlib.sha256(os.fsencode(str(path))).hexdigest()
    except Exception:
        return None


def _diagnostic_proc_read(pid, name, *, limit=4_000):
    """Best-effort bounded /proc read; never expose a read failure to cleanup."""
    try:
        safe_pid = int(pid)
        if safe_pid <= 0:
            raise ValueError('invalid pid')
        with open('/proc/%d/%s' % (safe_pid, name), 'r', encoding='utf-8', errors='replace') as handle:
            return _bounded_diagnostic_text(handle.read(limit + 1), limit)
    except Exception:
        return 'unavailable'


def _diagnostic_thread_dump(*, max_threads=12, max_stack_chars=1_200):
    """Bounded stack-only dump; deliberately never serializes frame locals."""
    try:
        frames = sys._current_frames()
        rows = []
        for thread in threading.enumerate()[:max_threads]:
            frame = frames.get(thread.ident)
            if frame is None:
                continue
            try:
                stack = ''.join(traceback.format_stack(frame, limit=16))
            except Exception:
                stack = 'unavailable'
            rows.append({
                'name': str(thread.name or '')[:120],
                'ident': _diagnostic_int(thread.ident),
                'stack': _bounded_diagnostic_text(stack, max_stack_chars),
            })
        return rows
    except Exception:
        return [{'name': 'unavailable', 'ident': None, 'stack': 'unavailable'}]


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


@dataclass(frozen=True)
class ProviderTerminalReceipt:
    """Typed authority emitted only after resident reads a successful result."""

    terminal_kind: str
    source: str
    turn_identity: str
    process_generation: int
    claude_session_id: str
    result_is_error: bool
    result_stop_reason: str

    @classmethod
    def from_result_event(
        cls,
        event,
        *,
        turn_identity,
        process_generation,
        claude_session_id,
    ):
        if not isinstance(event, dict) or event.get('type') != 'result':
            raise ValueError('provider terminal receipt requires type=result')
        if bool(event.get('is_error')):
            raise ValueError('provider terminal receipt cannot represent provider error')
        if event.get('stop_reason') != 'end_turn':
            raise ValueError('provider terminal receipt requires final end_turn')
        turn_id = str(turn_identity or '').strip()
        if not turn_id:
            raise ValueError('provider terminal receipt turn identity is missing')
        return cls(
            terminal_kind='provider_result',
            source='resident_live_stdout',
            turn_identity=turn_id,
            process_generation=int(process_generation),
            claude_session_id=str(claude_session_id or '').strip(),
            result_is_error=False,
            result_stop_reason=str(event.get('stop_reason') or ''),
        )


class ResidentTurnUsage(dict):
    """Existing usage mapping with typed, non-serialized turn metadata."""

    terminal_receipt: ProviderTerminalReceipt | None
    _candidate_cache_refresh_at: float | None
    _candidate_cache_refresh_monotonic: float | None

    def __init__(
        self,
        *args,
        terminal_receipt=None,
        candidate_cache_refresh_at=None,
        candidate_cache_refresh_monotonic=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.terminal_receipt = terminal_receipt
        self._candidate_cache_refresh_at = candidate_cache_refresh_at
        self._candidate_cache_refresh_monotonic = candidate_cache_refresh_monotonic


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
        if not isinstance(event, dict):
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
            self.last_provider_activity_at = time.time()
            return
        if _claude_event_is_activity(event):
            self.last_provider_activity_at = time.time()
        if self._stop_reason(event) == 'end_turn' and not self.end_turn_seen:
            self.end_turn_seen = True
            self.end_turn_seen_at = time.monotonic()

    def grace_expired(self, now=None):
        if self.result_seen or not self.end_turn_seen:
            return False
        current = time.monotonic() if now is None else float(now)
        return current - float(self.end_turn_seen_at) >= self.grace_seconds

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
            'terminal_reason': terminal_reason,
            'partial_rescue_performed': bool(partial_rescue_performed),
            'lock_released': lock_released,
        }

class TurnTerminalAuthority:
    """Single linearizer for one turn's provider terminal outcome."""

    RUNNING = 'RUNNING'
    SUCCESS = 'SUCCESS'
    PROVIDER_ERROR = 'PROVIDER_ERROR'
    STALL = 'STALL'
    HARD_TIMEOUT = 'HARD_TIMEOUT'
    RESULT_MISSING_AFTER_END_TURN = 'RESULT_MISSING_AFTER_END_TURN'
    CLI_TERMINAL_RESULT_MISSING = 'CLI_TERMINAL_RESULT_MISSING'
    DISCONNECTED = 'DISCONNECTED'
    DEFERRED = 'DEFERRED'

    _TIMEOUT_OUTCOMES = {
        'stall': (STALL, 'stall'),
        'hard': (HARD_TIMEOUT, 'hard_timeout'),
        'result_missing_after_end_turn': (
            RESULT_MISSING_AFTER_END_TURN,
            'result_missing_after_end_turn',
        ),
        'cli_terminal_result_missing': (
            CLI_TERMINAL_RESULT_MISSING,
            'cli_terminal_result_missing',
        ),
    }

    def __init__(self, *, turn_identity):
        self.turn_identity = str(turn_identity or '')
        self._lock = threading.Lock()
        self._outcome = self.RUNNING
        self._terminal_reason = None
        self._linearization_source = None
        self._linearized_at = None
        self._timeout_candidate_type = None
        self._timeout_candidate_received_at = None
        self._timeout_candidate_count = 0
        self._result_accepted_at = None
        self._resident_kill_requested = False
        self._resident_killed = False
        self._resident_kill_reason = None
        self._cleanup_count = 0
        self._cleanup_reason = None
        self._stale_stall_rejected_count = 0
        self._actual_stall_accepted_count = 0
        self._late_result_rejected_count = 0
        self._duplicate_terminal_signal_count = 0
        self._provider_error = None
        self._timeout_diagnostics = None
        # Lifecycle ownership is deliberately separate from the final
        # outcome.  A recovery claim never commits or overwrites a terminal
        # state; it only reserves the resident teardown path while RUNNING.
        self._recovery_claimed = False
        self._recovery_claimed_at = None
        self._recovery_claim_generation = None

    @property
    def outcome(self):
        with self._lock:
            return self._outcome

    @property
    def reason(self):
        with self._lock:
            return self._terminal_reason

    def is_terminal(self):
        return self.outcome != self.RUNNING

    def _commit_locked(self, outcome, reason, source):
        self._outcome = outcome
        self._terminal_reason = reason
        self._linearization_source = source
        self._linearized_at = time.time()

    def _reject_locked(self, signal):
        self._duplicate_terminal_signal_count += 1
        if signal == 'stall' and self._outcome == self.SUCCESS:
            self._stale_stall_rejected_count += 1
        if signal == 'provider_result' and self._outcome != self.SUCCESS:
            self._late_result_rejected_count += 1
        return False

    def submit_timeout_candidate(self, candidate_type):
        candidate_type = str(candidate_type or '').strip()
        mapped = self._TIMEOUT_OUTCOMES.get(candidate_type)
        if mapped is None:
            return False
        with self._lock:
            self._timeout_candidate_count += 1
            if self._timeout_candidate_type is None:
                self._timeout_candidate_type = candidate_type
                self._timeout_candidate_received_at = time.time()
            if self._outcome != self.RUNNING:
                return self._reject_locked(candidate_type)
            outcome, reason = mapped
            self._commit_locked(outcome, reason, 'watchdog:%s' % candidate_type)
            if candidate_type == 'stall':
                self._actual_stall_accepted_count += 1
            return True

    def accept_provider_result(self, terminal_receipt):
        if not isinstance(terminal_receipt, ProviderTerminalReceipt):
            return False
        with self._lock:
            if self._outcome != self.RUNNING:
                return self._reject_locked('provider_result')
            self._result_accepted_at = time.time()
            self._commit_locked(self.SUCCESS, 'result', 'provider_result')
            return True

    def accept_provider_error(self, provider_error):
        if not isinstance(provider_error, dict):
            provider_error = {}
        with self._lock:
            if self._outcome != self.RUNNING:
                return self._reject_locked('provider_error')
            self._provider_error = {
                key: provider_error.get(key)
                for key in (
                    'error_code',
                    'provider_error_type',
                    'provider_error_category',
                    'turn_failure_class',
                    'retryable',
                )
            }
            self._commit_locked(
                self.PROVIDER_ERROR, 'provider_error', 'provider_error',
            )
            return True

    def accept_disconnected(self, reason='disconnected'):
        with self._lock:
            if self._outcome != self.RUNNING:
                return self._reject_locked('disconnected')
            self._commit_locked(self.DISCONNECTED, reason, 'reader')
            return True

    def accept_deferred(self):
        with self._lock:
            if self._outcome != self.RUNNING:
                return self._reject_locked('deferred')
            self._commit_locked(self.DEFERRED, 'tool_deferred', 'provider_result')
            return True

    @property
    def recovery_claimed(self):
        with self._lock:
            return bool(self._recovery_claimed)

    def claim_terminal_recovery(self, probe_generation=None):
        """Atomically reserve recovery lifecycle ownership while RUNNING.

        This is not a terminal transition and cannot change the final
        outcome.  The caller must perform all transcript scanning before this
        method; the method only serializes the RUNNING check and lifecycle
        claim under the authority lock.
        """
        with self._lock:
            if self._outcome != self.RUNNING or self._recovery_claimed:
                return False
            self._recovery_claimed = True
            self._recovery_claimed_at = time.time()
            self._recovery_claim_generation = probe_generation
            return True

    def begin_cleanup(self, reason, *, allow_success=False):
        """Claim the serialized resident teardown token exactly once.

        Cleanup is a teardown/serialization token, not a second terminal
        outcome.  ``allow_success`` is used only when a recovered SUCCESS
        turn must make its resident non-reusable; the TurnTerminalAuthority
        still owns the final outcome in every case.
        """
        with self._lock:
            if (
                self._outcome == self.RUNNING
                or (self._outcome == self.SUCCESS and not allow_success)
                or self._cleanup_count
            ):
                return False
            self._cleanup_count = 1
            self._cleanup_reason = str(reason or self._terminal_reason or '')
            self._resident_kill_requested = True
            return True

    def record_resident_kill(self, reason, *, process_present):
        with self._lock:
            self._resident_killed = bool(process_present)
            self._resident_kill_reason = str(reason or self._cleanup_reason or '')

    def record_timeout_diagnostics(self, diagnostics):
        """Keep one bounded diagnostic sample for the accepted timeout only."""
        with self._lock:
            if self._timeout_diagnostics is None:
                self._timeout_diagnostics = dict(diagnostics or {})
                return True
            return False

    def snapshot(self):
        with self._lock:
            return {
                'turn_identity': self.turn_identity,
                'terminal_outcome': self._outcome,
                'terminal_reason': self._terminal_reason,
                'timeout_candidate_type': self._timeout_candidate_type,
                'timeout_candidate_received_at': self._timeout_candidate_received_at,
                'timeout_candidate_count': self._timeout_candidate_count,
                'result_accepted_at': self._result_accepted_at,
                'terminal_linearization_source': self._linearization_source,
                'terminal_linearized_at': self._linearized_at,
                'resident_kill_requested': self._resident_kill_requested,
                'resident_killed': self._resident_killed,
                'resident_kill_reason': self._resident_kill_reason,
                'cleanup_count': self._cleanup_count,
                'cleanup_reason': self._cleanup_reason,
                'stale_stall_rejected_count': self._stale_stall_rejected_count,
                'actual_stall_accepted_count': self._actual_stall_accepted_count,
                'late_result_rejected_count': self._late_result_rejected_count,
                'duplicate_terminal_signal_count': self._duplicate_terminal_signal_count,
                'provider_error': dict(self._provider_error or {}),
                'timeout_diagnostics': dict(self._timeout_diagnostics or {}),
                'recovery_claimed': bool(self._recovery_claimed),
                'recovery_claimed_at': self._recovery_claimed_at,
                'recovery_claim_generation': self._recovery_claim_generation,
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
        probe_timeout=None,
        on_probe=None,
    ):
        self.stall_timeout = float(stall_timeout)
        self.hard_timeout = float(hard_timeout)
        self._on_timeout = on_timeout
        self._on_probe = on_probe
        self._is_proc_alive = is_proc_alive
        self._poll_cap_sec = float(poll_cap_sec)
        self._probe_timeout = (
            None if probe_timeout is None else max(0.1, float(probe_timeout))
        )
        self._lock = threading.Lock()
        now = time.monotonic()
        self._started_at = now
        self._last_activity_at = now
        self._stopped = False
        self._fired_reason = None  # 'stall' | 'hard'
        # Probe eligibility belongs to a provider-inactivity epoch.  A failed
        # probe is consumed for this generation only; later real activity
        # creates a new generation and arms the next probe opportunity.
        self._activity_generation = 0
        self._probed_generation = None
        self._thread = None

    @property
    def fired_reason(self):
        with self._lock:
            return self._fired_reason

    @property
    def hard_deadline(self):
        with self._lock:
            return self._started_at + self.hard_timeout

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name='cc-stream-watchdog', daemon=True,
        )
        self._thread.start()

    def stop(self):
        with self._lock:
            self._stopped = True

    def claim_recovery(self, probe_generation, claim_lifecycle):
        """Revalidate probe state, then atomically claim lifecycle ownership.

        The watchdog lock is held only for state validation and the short
        authority claim callback.  Transcript scanning and recovery side
        effects happen outside this lock.
        """
        with self._lock:
            if self._stopped or self._fired_reason is not None:
                return False
            if self._activity_generation != probe_generation:
                return False
            if self._probed_generation != probe_generation:
                return False
            if not claim_lifecycle():
                return False
            self._stopped = True
            return True

    def note_activity(self):
        with self._lock:
            if self._stopped or self._fired_reason is not None:
                return
            self._last_activity_at = time.monotonic()
            self._activity_generation += 1

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

    def _try_probe(self):
        if self._probe_timeout is None or self._on_probe is None:
            return False
        with self._lock:
            if self._stopped or self._fired_reason is not None:
                return False
            if time.monotonic() - self._last_activity_at < self._probe_timeout:
                return False
            generation = self._activity_generation
            if self._probed_generation == generation:
                return False
            # Consume this inactivity epoch before invoking user code.  The
            # callback is unlocked so provider activity can arrive while the
            # bounded transcript probe is running; that activity increments
            # the generation and arms a later epoch without probe storms.
            self._probed_generation = generation
        try:
            consumed = bool(self._on_probe(generation))
        except Exception:
            consumed = False
        if consumed:
            with self._lock:
                self._stopped = True
        return consumed

    def _loop(self):
        while True:
            with self._lock:
                if self._stopped:
                    return
                now = time.monotonic()
                stall_left = self.stall_timeout - (now - self._last_activity_at)
                hard_left = self.hard_timeout - (now - self._started_at)
                probe_left = (
                    float('inf') if self._probe_timeout is None
                    else self._probe_timeout - (now - self._last_activity_at)
                )
            if hard_left <= 0:
                if self._try_fire('hard'):
                    return
            elif probe_left <= 0:
                if self._try_probe():
                    return
            elif stall_left <= 0:
                if self._try_fire('stall'):
                    return
            wait = self._poll_cap_sec
            if hard_left > 0:
                wait = min(wait, hard_left)
            if stall_left > 0:
                wait = min(wait, stall_left)
            if probe_left > 0:
                wait = min(wait, probe_left)
            time.sleep(max(0.01, wait))


class TerminalRecoveryController:
    """Intermediate post-completion recovery; it never chooses terminal state."""

    POST_COMPLETION_RECOVERY = 'POST_COMPLETION_RECOVERY'
    EOF_DRAIN = 'EOF_DRAIN'
    SIGTERM_DRAIN = 'SIGTERM_DRAIN'
    RECOVERED_AFTER_STDIN_EOF = 'RECOVERED_AFTER_STDIN_EOF'
    RECOVERED_AFTER_SIGTERM = 'RECOVERED_AFTER_SIGTERM'
    RESULT_MISSING = 'CLI_TERMINAL_RESULT_MISSING'
    HARD_TIMEOUT = 'HARD_TIMEOUT'

    def __init__(
        self,
        *,
        eof_grace,
        sigterm_grace,
        hard_deadline,
        close_stdin,
        send_sigterm,
        submit_hard_timeout,
        submit_result_missing,
    ):
        self.eof_grace = max(0.1, float(eof_grace))
        self.sigterm_grace = max(0.1, float(sigterm_grace))
        self.hard_deadline = float(hard_deadline)
        self._close_stdin = close_stdin
        self._send_sigterm = send_sigterm
        self._submit_hard_timeout = submit_hard_timeout
        self._submit_result_missing = submit_result_missing
        self._lock = threading.Lock()
        self._active = False
        self._stage = None
        self._started_at = None
        self._stdin_closed_at = None
        self._sigterm_sent_at = None
        self._first_post_eof_event_at = None
        self._first_post_sigterm_event_at = None
        self._recovered_result_at = None
        self._terminal_outcome = None
        self._events_after_eof = []
        self._events_after_sigterm = []
        self._proof = {}
        self._terminal_event = threading.Event()
        self._done = threading.Event()
        self._thread = None

    @property
    def active(self):
        with self._lock:
            return bool(self._active)

    @property
    def stage(self):
        with self._lock:
            return self._stage

    def start(self, proof):
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._stage = self.POST_COMPLETION_RECOVERY
            self._started_at = time.time()
            self._proof = dict(proof or {})
        # Stage 1 is synchronous with the accepted probe: close only stdin
        # before returning to the reader. The process object and stdout stay
        # intact for the bounded drain.
        try:
            self._close_stdin()
        finally:
            with self._lock:
                self._stdin_closed_at = time.time()
                self._stage = self.EOF_DRAIN
        self._thread = threading.Thread(
            target=self._run,
            name='cc-terminal-recovery',
            daemon=True,
        )
        try:
            self._thread.start()
        except RuntimeError:
            # Thread.start() can fail after stdin has already been closed.
            # Fail closed through the existing recovery terminal path and
            # always release wait_for_final(), so the generator cannot hang.
            with self._lock:
                self._stage = self.RESULT_MISSING
                self._terminal_outcome = self.RESULT_MISSING
            try:
                self._submit_result_missing()
            finally:
                self._done.set()
            return True
        return True

    @staticmethod
    def _safe_event(event):
        if not isinstance(event, dict):
            return None
        event_type = str(event.get('type') or '').strip()[:80]
        subtype = str(event.get('subtype') or '').strip()[:80]
        stop_reason = str(event.get('stop_reason') or '').strip()[:80]
        if event_type == 'assistant':
            message = event.get('message')
            if isinstance(message, dict):
                stop_reason = str(
                    message.get('stop_reason') or stop_reason or '',
                )[:80]
        if not event_type:
            return None
        return {
            'event_type': event_type,
            'subtype': subtype or None,
            'stop_reason': stop_reason or None,
        }

    def note_event(self, event):
        safe = self._safe_event(event)
        if safe is None:
            return
        now = time.time()
        with self._lock:
            if not self._active:
                return
            if self._stage == self.EOF_DRAIN:
                if self._first_post_eof_event_at is None:
                    self._first_post_eof_event_at = now
                if len(self._events_after_eof) < 32:
                    self._events_after_eof.append(safe)
            elif self._stage == self.SIGTERM_DRAIN:
                if self._first_post_sigterm_event_at is None:
                    self._first_post_sigterm_event_at = now
                if len(self._events_after_sigterm) < 32:
                    self._events_after_sigterm.append(safe)

    def mark_terminal(self, outcome):
        now = time.time()
        with self._lock:
            if not self._active:
                return
            self._terminal_outcome = str(outcome or '')[:80]
            if outcome == TurnTerminalAuthority.SUCCESS:
                self._recovered_result_at = now
                if self._stage == self.EOF_DRAIN:
                    self._stage = self.RECOVERED_AFTER_STDIN_EOF
                elif self._stage == self.SIGTERM_DRAIN:
                    self._stage = self.RECOVERED_AFTER_SIGTERM
            self._terminal_event.set()

    def wait_for_final(self):
        self._done.wait()

    def _remaining_hard_time(self):
        return self.hard_deadline - time.monotonic()

    def _wait_stage(self, grace):
        remaining = min(float(grace), self._remaining_hard_time())
        if remaining <= 0:
            return False
        self._terminal_event.wait(remaining)
        return self._terminal_event.is_set()

    def _run(self):
        try:
            if self._wait_stage(self.eof_grace):
                return
            if self._remaining_hard_time() <= 0:
                with self._lock:
                    self._stage = self.HARD_TIMEOUT
                self._submit_hard_timeout()
                return

            with self._lock:
                self._stage = self.SIGTERM_DRAIN
            self._send_sigterm()
            with self._lock:
                self._sigterm_sent_at = time.time()
            if self._wait_stage(self.sigterm_grace):
                return
            if self._remaining_hard_time() <= 0:
                with self._lock:
                    self._stage = self.HARD_TIMEOUT
                self._submit_hard_timeout()
                return

            with self._lock:
                self._stage = self.RESULT_MISSING
            self._submit_result_missing()
        finally:
            with self._lock:
                if self._terminal_outcome is None:
                    self._terminal_outcome = 'RUNNING'
            self._done.set()

    def snapshot(self, *, final_outcome=None):
        with self._lock:
            started_at = self._started_at
            finished_at = time.time() if self._done.is_set() else None
            return {
                'active': bool(self._active),
                'recovery_stage': self._stage,
                'durable_end_turn_detected_at': self._proof.get(
                    'detected_at',
                ),
                'provider_last_activity_at': self._proof.get(
                    'provider_last_activity_at',
                ),
                'recovery_started_at': started_at,
                'stdin_closed_at': self._stdin_closed_at,
                'first_post_eof_event_at': self._first_post_eof_event_at,
                'sigterm_sent_at': self._sigterm_sent_at,
                'first_post_sigterm_event_at': self._first_post_sigterm_event_at,
                'recovered_result_at': self._recovered_result_at,
                'events_after_eof': list(self._events_after_eof),
                'events_after_sigterm': list(self._events_after_sigterm),
                'total_recovery_latency_ms': (
                    max(0, int((finished_at - started_at) * 1000))
                    if started_at is not None and finished_at is not None else None
                ),
                'final_outcome': str(
                    final_outcome or self._terminal_outcome or '',
                )[:80] or None,
                'proof': dict(self._proof),
            }


_PROCESS_FAILURE_CODES = frozenset({
    'stdin_write_failed',
    'provider_stall_timeout',
    'provider_hard_timeout',
    'result_missing_after_end_turn',
    'cli_terminal_result_missing',
    'result_missing_before_terminal',
    'claude_runtime_post_send_failure',
})
_RUNTIME_FAILURE_CODES = frozenset({
    'claude_runtime_below_minimum',
    'claude_runtime_invalid',
    'claude_runtime_version_mismatch',
    'claude_runtime_unavailable',
    'claude_runtime_startup_failed',
})
_RUNTIME_CLASS_CODES = frozenset({
    *_RUNTIME_FAILURE_CODES,
    'claude_runtime_rollback_pending',
    'CC_MODEL_RUNTIME_INCOMPATIBLE',
})
_TURN_FAILURE_CODES = frozenset({
    'provider_refusal',
    'invalid_request',
    'rate_limit',
    'provider_error',
})


def resident_failure_class(error_code):
    code = str(error_code or '')
    if code in _PROCESS_FAILURE_CODES:
        return 'PROCESS_LEVEL'
    if code in _RUNTIME_CLASS_CODES:
        return 'RUNTIME_LEVEL'
    if code in _TURN_FAILURE_CODES:
        return 'TURN_LEVEL'
    return None


def classify_provider_error_event(event, *, refusal_marker=False):
    """Reduce a Claude stream error to safe, stable public metadata.

    Raw provider result text is inspected only for known classification tokens;
    it is never copied into the exception message or SSE payload.
    """
    event = event if isinstance(event, dict) else {}
    message = event.get('message') if isinstance(event.get('message'), dict) else {}
    error_value = event.get('error')
    error_type = ''
    if isinstance(error_value, dict):
        error_type = str(
            error_value.get('type')
            or error_value.get('code')
            or error_value.get('name')
            or ''
        )
    elif isinstance(error_value, str):
        error_type = error_value
    stop_details = event.get('stop_details')
    stop_details = stop_details if isinstance(stop_details, dict) else {}
    category = str(
        event.get('error_category')
        or stop_details.get('category')
        or ''
    ).strip().lower()
    parts = [
        error_type,
        str(event.get('subtype') or ''),
        str(event.get('stop_reason') or ''),
        str(event.get('result') or ''),
        category,
    ]
    for block in (message.get('content') or []):
        if isinstance(block, dict) and block.get('type') == 'text':
            parts.append(str(block.get('text') or ''))
    searchable = ' '.join(parts).lower().replace('-', '_').replace(' ', '_')
    safe_type = error_type.strip().lower().replace('-', '_').replace(' ', '_')
    known_types = {
        'invalid_request', 'invalid_request_error', 'rate_limit',
        'rate_limit_error', 'overloaded_error', 'authentication_error',
        'permission_error', 'connection_error', 'timeout_error',
        'model_refusal_no_fallback',
    }
    if safe_type not in known_types:
        safe_type = 'provider_api_error'

    is_reasoning_refusal = 'reasoning_extraction' in searchable
    is_refusal = (
        refusal_marker
        or 'model_refusal_no_fallback' in searchable
        or 'refusal' in searchable
        or 'safety' in searchable
    )
    is_rate_limit = 'rate_limit' in searchable or 'too_many_requests' in searchable
    is_overload = 'overloaded_error' in searchable
    is_transport = any(token in searchable for token in (
        'connection_error', 'timeout_error', 'broken_pipe', 'transport_error',
        'connection_reset', 'connection_closed',
    ))

    if is_reasoning_refusal:
        failure_code = 'provider_refusal'
        failure_category = 'reasoning_extraction'
        public_message = 'Claude 拒绝了本轮请求，请调整内容后再试。'
        failure_class = 'TURN_LEVEL'
        retryable = False
    elif is_refusal:
        failure_code = 'provider_refusal'
        failure_category = 'content_safety_refusal'
        public_message = 'Claude 拒绝了本轮请求，请调整内容后再试。'
        failure_class = 'TURN_LEVEL'
        retryable = False
    elif is_rate_limit:
        failure_code = 'rate_limit'
        failure_category = 'rate_limit'
        public_message = 'Claude 请求暂时受限，请稍后再试。'
        failure_class = 'TURN_LEVEL'
        retryable = True
    elif 'invalid_request' in searchable:
        failure_code = 'invalid_request'
        failure_category = 'invalid_request'
        public_message = 'Claude 无法处理本轮请求。'
        failure_class = 'TURN_LEVEL'
        retryable = False
    elif is_transport:
        failure_code = 'provider_error'
        failure_category = 'transport'
        public_message = 'Claude Code 本轮连接异常，当前生成已停止。'
        failure_class = 'PROCESS_LEVEL'
        retryable = True
    else:
        failure_code = 'provider_error'
        failure_category = 'provider_request_failure'
        public_message = 'Claude provider 请求失败；本轮未自动重试。'
        failure_class = 'TURN_LEVEL'
        retryable = is_overload

    return {
        'error_code': failure_code,
        'provider_error_type': safe_type,
        'provider_error_category': failure_category,
        'turn_failure_class': failure_class,
        'retryable': retryable,
        'public_message': public_message,
    }


class ResidentError(RuntimeError):
    def __init__(
        self,
        message,
        *,
        usage=None,
        diagnostics=None,
        error_code=None,
        provider_error_type=None,
        provider_error_category=None,
        turn_failure_class=None,
        retryable=None,
    ):
        super().__init__(message)
        self.usage = usage or empty_usage()
        self.diagnostics = dict(diagnostics or {})
        self.error_code = error_code
        self.provider_error_type = provider_error_type
        self.provider_error_category = provider_error_category
        self.turn_failure_class = (
            turn_failure_class or resident_failure_class(error_code)
        )
        self.retryable = retryable


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


def compose_spawn_argv(
    runtime_version,
    *,
    system_text,
    tool_flags,
    model_args=(),
    effort_args=(),
    session_args=(),
    max_turns='5',
):
    """Build the provider-visible Claude argv used by main-chat spawn paths.

    ``_spawn``, ``spawn_resumable``, and ``spawn_fresh_named`` share this
    helper so resume/fork callers cannot drift onto a second prompt surface.
    ``session_args`` is ``()``, ``('--resume', sid)``, or ``('--session-id', sid)``.
    """
    from chat.cc_runtime import claude_cmd_for_version
    extra_session = [str(part) for part in session_args]
    return claude_cmd_for_version(
        runtime_version,
        '-p',
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--system-prompt', system_text,
        '--max-turns', str(max_turns),
        '--tools', tool_flags['tools'],
        '--thinking-display', 'summarized',
        '--exclude-dynamic-system-prompt-sections',
        *extra_session,
    ) + list(model_args) + list(effort_args) + list(tool_flags.get('extra') or ())


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
        self._turn_state_lock = threading.Lock()
        self._turn_active = False
        self._turn_write_started = False
        self._turn_stdin_flushed = False
        self._last_turn_stdin_write_started = False
        self._last_turn_stdin_flushed = False
        self._runtime_identity = None
        self._runtime_rollback_pending = None
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
        """Bind the service home and resident-owned lease path for each spawn."""
        prepared = dict(os.environ if env is None else env)
        if not str(prepared.get('HOME') or '').strip():
            from chat.cc_runtime import service_home
            prepared['HOME'] = str(service_home(prepared))
        if self._tool_profile == TOOL_PROFILE_UH_A0:
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
        # Provider cache freshness is generation-scoped and is committed only
        # by route-specific success boundaries, never by send_turn itself.
        self._last_cache_refresh_at = None
        self._last_cache_refresh_monotonic = None
        self._tool_surface_snapshot = {}

    @staticmethod
    def _managed_runtime_identity(version):
        from chat.cc_runtime import MINIMUM_CLAUDE_CODE_VERSION, version_tuple
        try:
            if version_tuple(version) < version_tuple(MINIMUM_CLAUDE_CODE_VERSION):
                raise ResidentError('Claude Code runtime is below the supported minimum',
                                    error_code='claude_runtime_below_minimum')
        except ResidentError:
            raise
        except Exception as exc:
            raise ResidentError('Claude Code runtime version is invalid',
                                error_code='claude_runtime_invalid') from exc
        return 'claude-code:%s' % version

    def _build_spawn_tool_flags(self, *, env=None, write_mcp_config=True, write_settings=True):
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
                write_mcp_config=write_mcp_config,
                write_settings=write_settings,
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
        from chat.cc_runtime import ClaudeRuntimeError, require_managed_claude_runtime
        with self._turn_state_lock:
            if self._turn_active:
                raise ResidentError('resident_turn_in_progress')
        self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
        env = self._prepare_spawn_env(env)
        try:
            runtime_version = require_managed_claude_runtime(env=env, cwd=self._cwd)
        except ClaudeRuntimeError as exc:
            message = str(exc)
            code = (
                'claude_runtime_below_minimum' if 'below minimum' in message
                else 'claude_runtime_version_mismatch' if 'mismatch' in message
                else 'claude_runtime_unavailable'
            )
            raise ResidentError('Claude Code runtime is unavailable', error_code=code) from exc
        _model, model_identity, model_args = cc_model_snapshot()
        effort, effort_identity, effort_args = cc_effort_snapshot()
        if _model:
            from chat.cc_model import cc_model_runtime_compatibility, CC_MODEL_RUNTIME_INCOMPATIBLE
            compatible, requirement = cc_model_runtime_compatibility(_model)
            if not compatible:
                raise ResidentError(
                    '当前 Claude Code 版本不支持这个模型',
                    error_code=CC_MODEL_RUNTIME_INCOMPATIBLE,
                )
        tool_flags = self._build_spawn_tool_flags(env=env)
        surface_fingerprint = self._require_spawn_surface_fingerprint(tool_flags)
        self._kill(quiet=True)
        args = compose_spawn_argv(
            runtime_version,
            system_text=system_text,
            tool_flags=tool_flags,
            model_args=model_args,
            effort_args=effort_args,
        )
        try:
            self._proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, cwd=self._cwd, env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._proc = None
            raise ResidentError(
                'Claude Code 启动失败',
                error_code='claude_runtime_startup_failed',
            ) from exc
        time.sleep(0.1)
        if self._proc.poll() is not None:
            self._kill(quiet=True)
            raise ResidentError(
                'Claude Code 启动失败',
                error_code='claude_runtime_startup_failed',
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
        self._runtime_identity = self._managed_runtime_identity(runtime_version)
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

    def _decide_respawn_reason(
        self,
        system_text,
        *,
        tool_profile=TOOL_PROFILE_LEGACY,
        allow_stale_cache_guard=False,
    ):
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
        stored_runtime_identity = getattr(self, '_runtime_identity', None)
        if stored_runtime_identity is not None:
            try:
                from chat.cc_runtime import active_claude_version
                active_runtime_identity = 'claude-code:%s' % active_claude_version()
            except Exception as exc:
                raise ResidentError(
                    'Claude Code runtime is unavailable',
                    error_code='claude_runtime_unavailable',
                ) from exc
            if active_runtime_identity != stored_runtime_identity:
                return 'runtime_changed'
        # Never-used residents (_last_used == 0) have no idle age — peek_idle_seconds
        # returns None. Only reap after a real successful use older than IDLE_REAP.
        idle_seconds = self.peek_idle_seconds()
        if idle_seconds is not None and idle_seconds > IDLE_REAP_SECONDS:
            return 'idle'
        if system_text != self._system_text:
            return 'system_changed'

        hard = _cfg_int('CC_CONTEXT_HARD_LIMIT', 180_000)
        soft = _cfg_int('CC_CONTEXT_SOFT_LIMIT', 150_000)
        max_turns = _cfg_int('CC_MAX_RESIDENT_TURNS', 45)
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

        if allow_stale_cache_guard and self._stale_cache_guard_due():
            return 'stale_cache_guard'
        return None

    def ensure_alive(self, system_text, env, *, tool_profile=TOOL_PROFILE_LEGACY):
        with self._lock:
            with self._turn_state_lock:
                if self._turn_active:
                    raise ResidentError('resident_turn_in_progress')
            pending_runtime = getattr(self, '_runtime_rollback_pending', None)
            if pending_runtime:
                from chat.cc_runtime import active_claude_version
                try:
                    active_runtime = active_claude_version()
                except Exception as exc:
                    raise ResidentError(
                        'Claude Code runtime is unavailable',
                        error_code='claude_runtime_rollback_pending',
                    ) from exc
                if active_runtime == pending_runtime:
                    try:
                        from tools.claude_runtime_updater import rollback_active_runtime
                        rolled_back = rollback_active_runtime(
                            expected_active=pending_runtime,
                            reason='runtime_failure_after_stdin',
                        )
                    except Exception:
                        rolled_back = None
                    if not rolled_back:
                        raise ResidentError(
                            'Claude Code runtime is unhealthy; rollback is waiting for the lifecycle lock.',
                            error_code='claude_runtime_rollback_pending',
                        )
                    self._next_spawn_reason = 'runtime_changed'
                self._runtime_rollback_pending = None
            reason = self._decide_respawn_reason(system_text, tool_profile=tool_profile)
            if not reason:
                return self._cold
            if reason == 'history_rewrite':
                self._system_text = None
                self._session_id = None
                self._model_identity = None
                self._effort_identity = None
                self._effort_value = None
                self._cold = True
                self._next_spawn_reason = 'history_rewrite'
            try:
                self._spawn(system_text, env, reason=reason, tool_profile=tool_profile)
            except ResidentError as exc:
                if exc.error_code not in _RUNTIME_FAILURE_CODES:
                    raise
                from chat.cc_runtime import active_claude_version
                expected_active = active_claude_version()
                try:
                    from tools.claude_runtime_updater import rollback_active_runtime
                    rolled_back = rollback_active_runtime(
                        expected_active=expected_active,
                        reason='runtime_startup_failed',
                    )
                except Exception:
                    rolled_back = None
                if not rolled_back:
                    self._runtime_rollback_pending = expected_active
                    raise
                self._runtime_rollback_pending = None
                if self._alive() and self._runtime_identity == 'claude-code:%s' % rolled_back:
                    self._next_spawn_reason = None
                    return self._cold
                self._spawn(
                    system_text, env, reason='runtime_changed',
                    tool_profile=tool_profile,
                )
            return self._cold

    def ensure_stale_cache_guard(
        self,
        system_text,
        env,
        *,
        tool_profile=TOOL_PROFILE_LEGACY,
    ):
        """Replace only when no stronger pre-send respawn reason exists."""
        with self._lock:
            reason = self._decide_respawn_reason(
                system_text,
                tool_profile=tool_profile,
                allow_stale_cache_guard=True,
            )
            if reason != 'stale_cache_guard':
                return False
            self._spawn(
                system_text,
                env,
                reason='stale_cache_guard',
                tool_profile=tool_profile,
            )
            return True

    def peek_respawn_reason(
        self,
        system_text,
        *,
        tool_profile=TOOL_PROFILE_LEGACY,
        allow_stale_cache_guard=False,
    ):
        """Read-only: same reason as ``_decide_respawn_reason``, or None.

        Does not spawn, kill, change generation, or write stdin.
        """
        with self._lock:
            return self._decide_respawn_reason(
                system_text,
                tool_profile=tool_profile,
                allow_stale_cache_guard=allow_stale_cache_guard,
            )

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
        from chat.cc_runtime import ClaudeRuntimeError, require_managed_claude_runtime
        with self._lock:
            if self._alive():
                raise ResidentError('staged spawn on live session')
            self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
            env = self._prepare_spawn_env(env)
            try:
                runtime_version = require_managed_claude_runtime(env=env, cwd=self._cwd)
            except ClaudeRuntimeError as exc:
                message = str(exc)
                code = (
                    'claude_runtime_below_minimum' if 'below minimum' in message
                    else 'claude_runtime_version_mismatch' if 'mismatch' in message
                    else 'claude_runtime_unavailable'
                )
                raise ResidentError('Claude Code runtime is unavailable', error_code=code) from exc
            _model, model_identity, model_args = cc_model_snapshot()
            effort, effort_identity, effort_args = cc_effort_snapshot()
            tool_flags = self._build_spawn_tool_flags(env=env)
            surface_fingerprint = self._require_spawn_surface_fingerprint(tool_flags)
            args = compose_spawn_argv(
                runtime_version,
                system_text=system_text,
                tool_flags=tool_flags,
                model_args=model_args,
                effort_args=effort_args,
                session_args=('--resume', resume_session_id),
            )
            try:
                self._proc = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, cwd=self._cwd, env=env,
                )
                self._runtime_identity = self._managed_runtime_identity(runtime_version)
            except Exception as exc:
                self._proc = None
                raise ResidentError(
                    'Claude Code 启动失败',
                    error_code='claude_runtime_startup_failed',
                ) from exc
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
        from chat.cc_runtime import ClaudeRuntimeError, require_managed_claude_runtime
        with self._lock:
            if self._alive():
                raise ResidentError('staged spawn on live session')
            self._tool_profile = str(tool_profile or TOOL_PROFILE_LEGACY)
            env = self._prepare_spawn_env(env)
            try:
                runtime_version = require_managed_claude_runtime(env=env, cwd=self._cwd)
            except ClaudeRuntimeError as exc:
                message = str(exc)
                code = (
                    'claude_runtime_below_minimum' if 'below minimum' in message
                    else 'claude_runtime_version_mismatch' if 'mismatch' in message
                    else 'claude_runtime_unavailable'
                )
                raise ResidentError('Claude Code runtime is unavailable', error_code=code) from exc
            _model, model_identity, model_args = cc_model_snapshot()
            effort, effort_identity, effort_args = cc_effort_snapshot()
            tool_flags = self._build_spawn_tool_flags(env=env)
            surface_fingerprint = self._require_spawn_surface_fingerprint(tool_flags)
            args = compose_spawn_argv(
                runtime_version,
                system_text=system_text,
                tool_flags=tool_flags,
                model_args=model_args,
                effort_args=effort_args,
                session_args=('--session-id', session_id),
            )
            if '--resume' in args:
                raise ResidentError('fresh_named must not carry --resume')
            try:
                self._proc = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, cwd=self._cwd, env=env,
                )
                self._runtime_identity = self._managed_runtime_identity(runtime_version)
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

    def _stale_cache_guard_due(self):
        if self._last_round_context <= STALE_CACHE_CONTEXT_THRESHOLD:
            return False
        refreshed_at = self._last_cache_refresh_monotonic
        if refreshed_at is None:
            return False
        try:
            age = time.monotonic() - float(refreshed_at)
        except (TypeError, ValueError):
            return False
        return math.isfinite(age) and age >= STALE_CACHE_MAX_AGE_SECONDS

    def commit_cache_freshness(self, *, wall_at, monotonic_at):
        """Atomically commit one proven provider-request start timestamp.

        The caller must invoke this only after its route-specific terminal and
        transcript/finality proof. Candidate capture in send_turn is not a
        commit and never mutates these fields.
        """
        try:
            wall_value = float(wall_at)
            monotonic_value = float(monotonic_at)
        except (TypeError, ValueError):
            return False
        if (
            not math.isfinite(wall_value)
            or not math.isfinite(monotonic_value)
            or wall_value <= 0.0
            or monotonic_value < 0.0
        ):
            return False
        with self._lock:
            self._last_cache_refresh_at = wall_value
            self._last_cache_refresh_monotonic = monotonic_value
        return True

    @property
    def last_cache_refresh_at(self):
        return self._last_cache_refresh_at

    @property
    def last_cache_refresh_monotonic(self):
        return self._last_cache_refresh_monotonic

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

    def send_turn(self, *args, **kwargs):
        """Hold the lifecycle lock for a turn so no spawn can kill it mid-stream."""
        with self._lock:
            with self._turn_state_lock:
                if self._turn_active:
                    raise ResidentError('resident_turn_in_progress')
                self._turn_active = True
                self._turn_write_started = False
                self._turn_stdin_flushed = False
            try:
                yield from self._send_turn_impl(*args, **kwargs)
            except BaseException as exc:
                write_started = bool(self._turn_write_started)
                if write_started:
                    runtime_identity = str(getattr(self, '_runtime_identity', '') or '')
                    if (
                        isinstance(exc, ResidentError)
                        and exc.turn_failure_class == 'RUNTIME_LEVEL'
                        and exc.error_code in _RUNTIME_FAILURE_CODES
                        and runtime_identity.startswith('claude-code:')
                    ):
                        expected_active = runtime_identity.split(':', 1)[1]
                        rolled_back = None
                        try:
                            from tools.claude_runtime_updater import rollback_active_runtime
                            rolled_back = rollback_active_runtime(
                                expected_active=expected_active,
                                reason='runtime_failure_after_stdin',
                            )
                        except Exception:
                            pass
                        self._runtime_rollback_pending = (
                            None if rolled_back else expected_active
                        )
                        self._next_spawn_reason = (
                            'runtime_changed' if rolled_back else 'process_dead'
                        )
                    else:
                        # After any attempted stdin write, the current resident
                        # session is not reusable and the same input is never replayed.
                        self._runtime_rollback_pending = None
                        self._next_spawn_reason = 'process_dead'
                    authority = getattr(
                        self, '_active_terminal_authority', None,
                    )
                    if authority is None:
                        self._kill(quiet=True)
                    else:
                        if not authority.is_terminal():
                            authority.accept_disconnected('send_turn_exception')
                        if authority.begin_cleanup('send_turn_exception'):
                            process_present = self._proc is not None
                            try:
                                self._kill(quiet=True)
                            finally:
                                authority.record_resident_kill(
                                    'send_turn_exception',
                                    process_present=process_present,
                                )
                raise
            finally:
                with self._turn_state_lock:
                    self._last_turn_stdin_write_started = bool(self._turn_write_started)
                    self._last_turn_stdin_flushed = bool(self._turn_stdin_flushed)
                    self._turn_active = False
                self._active_terminal_authority = None

    def _send_turn_impl(
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

        # Bind one authority before any turn-side effect. The outer
        # send_turn exception path uses this same object for bounded cleanup.
        stall_timeout = _cfg_int('CC_STREAM_TIMEOUT', CC_STREAM_TIMEOUT)
        hard_timeout = _cfg_int('CC_STREAM_HARD_TIMEOUT', CC_STREAM_HARD_TIMEOUT)
        result_grace = _cfg_int('CC_STREAM_RESULT_GRACE', CC_STREAM_RESULT_GRACE)
        recovery_probe = _cfg_int(
            'CC_TERMINAL_RECOVERY_PROBE', CC_TERMINAL_RECOVERY_PROBE,
        )
        recovery_eof_grace = _cfg_int(
            'CC_TERMINAL_RECOVERY_EOF_GRACE', CC_TERMINAL_RECOVERY_EOF_GRACE,
        )
        recovery_sigterm_grace = _cfg_int(
            'CC_TERMINAL_RECOVERY_SIGTERM_GRACE',
            CC_TERMINAL_RECOVERY_SIGTERM_GRACE,
        )
        terminal = ProviderTerminalTracker(result_grace)
        authority = TurnTerminalAuthority(turn_identity=terminal.turn_identity)
        self._active_terminal_authority = authority
        turn_process_generation = int(self._generation)
        turn_session_id = self._session_id

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
        # This is a local candidate only. It is deliberately captured before
        # stdin.write and committed only by the route-specific success proof.
        candidate_cache_refresh_at = time.time()
        candidate_cache_refresh_monotonic = time.monotonic()
        self._turn_write_started = True
        try:
            proc.stdin.write(payload + NL)
            proc.stdin.flush()
            self._turn_stdin_flushed = True
        except (BrokenPipeError, OSError) as e:
            if uh_a0_runtime is not None:
                uh_a0_runtime.abort_turn(turn_id=uh_a0_turn_id)
            raise ResidentError('Claude Code stdin write failed', error_code='stdin_write_failed') from e

        if on_stdin_flushed is not None:
            try:
                on_stdin_flushed()
            except BaseException:
                # Message already entered the pipe; outer turn cleanup owns the kill.
                if uh_a0_runtime is not None:
                    uh_a0_runtime.abort_turn(turn_id=uh_a0_turn_id)
                raise

        # 信已塞进门缝：立刻提交 resident 游标（即使后续流中断也不重复塞）
        try:
            self._commit_sent_context(commit_meta)
        except BaseException:
            if uh_a0_runtime is not None:
                uh_a0_runtime.abort_turn(turn_id=uh_a0_turn_id)
            raise

        think_acc, text_acc = [], []
        # Claude's terminal assistant rows can contain the complete text even
        # when one or more stream deltas were lost. Keep this separate from
        # the live accumulator; Daily canonicalization still reads transcript.
        provider_text_acc = []
        rounds = []
        current_round = None
        provider_error = None
        provider_refusal_seen = False
        saw_result = False
        terminal_receipt = None
        recovery = None
        recovery_success_nonreusable = False
        # Serializes the short lifecycle handoff between an authority
        # recovery claim, controller start, and a result that arrives in the
        # same window.  It is not held while scanning JSONL or by watchdog
        # lock code.
        recovery_start_lock = threading.Lock()

        reader_state_lock = threading.Lock()
        reader_state_data = {
            'reader_state': 'IDLE/UNKNOWN',
            'reader_state_entered_at': time.time(),
        }
        timeout_diagnostic_lock = threading.Lock()

        def _set_reader_state(state):
            now = time.time()
            with reader_state_lock:
                if reader_state_data['reader_state'] != state:
                    reader_state_data['reader_state'] = str(state)
                    reader_state_data['reader_state_entered_at'] = now

        def _reader_state_snapshot():
            now = time.time()
            with reader_state_lock:
                entered_at = float(reader_state_data['reader_state_entered_at'])
                return {
                    'reader_state': reader_state_data['reader_state'],
                    'reader_state_entered_at': entered_at,
                    'reader_state_duration_ms': max(
                        0, int((now - entered_at) * 1000),
                    ),
                }

        def _downstream_yield(value):
            _set_reader_state('DOWNSTREAM_YIELD')
            try:
                yield value
            finally:
                _set_reader_state('PROCESS_EVENT')

        def _transcript_timeout_snapshot():
            """Inspect only bounded, current-cursor JSONL metadata."""
            cursor = dict(jsonl_cursor or {})
            path = str(cursor.get('path') or '')
            start_offset = _diagnostic_int(cursor.get('offset'))
            start_offset = max(0, start_offset or 0)
            result = {
                'transcript_path_sha256': _diagnostic_path_hash(path),
                'current_turn_start_offset': start_offset if path else None,
                'observed_end_offset': None,
                'last_current_turn_event_type': None,
                'last_current_turn_timestamp': None,
                'last_current_turn_assistant_stop_reason': None,
                'current_turn_durable_assistant_end_turn': False,
                'transcript_scan_truncated': False,
            }
            if not path:
                return result
            try:
                observed_end = int(os.path.getsize(path))
                result['observed_end_offset'] = observed_end
                if start_offset > observed_end:
                    start_offset = observed_end
                scan_start = max(
                    start_offset,
                    observed_end - _TIMEOUT_DIAGNOSTIC_TRANSCRIPT_SCAN_BYTES,
                )
                result['transcript_scan_truncated'] = scan_start > start_offset
                with open(path, 'rb') as handle:
                    handle.seek(scan_start)
                    raw = handle.read(_TIMEOUT_DIAGNOSTIC_TRANSCRIPT_SCAN_BYTES)
                for line in raw.splitlines():
                    try:
                        row = json.loads(line.decode('utf-8', errors='replace'))
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    event_type = str(row.get('type') or '').strip()[:80]
                    subtype = str(row.get('subtype') or '').strip()[:80]
                    if not event_type:
                        continue
                    label = event_type
                    if subtype:
                        label += ':' + subtype
                    result['last_current_turn_event_type'] = label[:180]
                    timestamp = row.get('timestamp')
                    if timestamp is not None:
                        result['last_current_turn_timestamp'] = str(timestamp)[:80]
                    message = row.get('message')
                    stop_reason = row.get('stop_reason')
                    if isinstance(message, dict):
                        stop_reason = message.get('stop_reason') or stop_reason
                    if event_type == 'assistant' and stop_reason:
                        result['last_current_turn_assistant_stop_reason'] = str(
                            stop_reason,
                        )[:80]
                        if stop_reason == 'end_turn':
                            result['current_turn_durable_assistant_end_turn'] = True
                return result
            except Exception:
                result['transcript_scan_truncated'] = True
                return result

        def _capture_timeout_diagnostics(candidate_type):
            """Capture one safe timeout sample before its cleanup/kill."""
            if not timeout_diagnostic_lock.acquire(blocking=False):
                return
            try:
                state = _reader_state_snapshot()
                authority_snapshot = authority.snapshot()
                pid = getattr(proc, 'pid', None)
                try:
                    proc_poll = proc.poll()
                except Exception:
                    proc_poll = 'unavailable'
                diagnostics = {
                    'terminal_outcome': authority_snapshot['terminal_outcome'],
                    'timeout_candidate': str(candidate_type or '')[:80],
                    'turn_identity': terminal.turn_identity,
                    'claude_session_id': str(self._session_id or '')[:200] or None,
                    'process_generation': int(self._generation),
                    'resident_pid': _diagnostic_int(pid),
                    'proc_poll': proc_poll,
                    **state,
                    'thread_dump': _diagnostic_thread_dump(),
                    'child_wchan': _diagnostic_proc_read(pid, 'wchan'),
                    'child_stack': _diagnostic_proc_read(pid, 'stack'),
                }
                diagnostics.update(_transcript_timeout_snapshot())
                authority.record_timeout_diagnostics(diagnostics)
            except Exception as exc:
                # Diagnostics are strictly best effort and must never block kill.
                authority.record_timeout_diagnostics({
                    'terminal_outcome': authority.outcome,
                    'timeout_candidate': str(candidate_type or '')[:80],
                    'turn_identity': terminal.turn_identity,
                    'claude_session_id': str(self._session_id or '')[:200] or None,
                    'process_generation': int(self._generation),
                    'diagnostics_error': type(exc).__name__,
                })
            finally:
                timeout_diagnostic_lock.release()

        def _durable_current_turn_end_turn_proof():
            """Fail closed unless the current session/cursor proves this turn."""
            session_id = str(turn_session_id or self._session_id or '').strip()
            if not session_id or int(self._generation) != turn_process_generation:
                return None
            cursor = dict(jsonl_cursor or {})
            path = str(cursor.get('path') or '')
            start_offset = _diagnostic_int(cursor.get('offset'))
            if not path or start_offset is None or start_offset < 0:
                return None
            try:
                from tools.cc_jsonl_usage import session_jsonl_path
                expected_path = session_jsonl_path(self._cwd, session_id)
                if expected_path is None or os.path.abspath(str(expected_path)) != os.path.abspath(path):
                    return None
                observed_end = int(os.path.getsize(path))
                if observed_end <= int(start_offset):
                    return None
                span = observed_end - int(start_offset)
                scan_start = int(start_offset)
                if span > _TIMEOUT_DIAGNOSTIC_TRANSCRIPT_SCAN_BYTES:
                    # Keep the proof bounded for long thinking/tool turns.
                    # The bounded window may begin in the middle of a JSONL
                    # record, so discard that partial first line rather than
                    # interpreting it as current-turn evidence.
                    scan_start = max(
                        int(start_offset),
                        observed_end - _TIMEOUT_DIAGNOSTIC_TRANSCRIPT_SCAN_BYTES,
                    )
                with open(path, 'rb') as handle:
                    handle.seek(scan_start)
                    raw = handle.read(observed_end - scan_start)
                if scan_start > int(start_offset):
                    first_newline = raw.find(b'\n')
                    if first_newline < 0:
                        return None
                    scan_start += first_newline + 1
                    raw = raw[first_newline + 1:]
                offset = scan_start
                for line in raw.splitlines(keepends=True):
                    event_offset = offset
                    offset += len(line)
                    try:
                        row = json.loads(line.decode('utf-8', errors='replace'))
                    except Exception:
                        continue
                    if not isinstance(row, dict) or row.get('type') != 'assistant':
                        continue
                    message = row.get('message')
                    stop_reason = row.get('stop_reason')
                    if isinstance(message, dict):
                        stop_reason = message.get('stop_reason') or stop_reason
                    if stop_reason != 'end_turn' or event_offset <= int(start_offset):
                        continue
                    return {
                        'current_session_id': session_id[:200],
                        'process_generation': turn_process_generation,
                        'turn_identity': terminal.turn_identity,
                        'transcript_path_sha256': _diagnostic_path_hash(path),
                        'current_turn_start_offset': int(start_offset),
                        'event_offset': int(event_offset),
                        'observed_end_offset': observed_end,
                        'assistant_stop_reason': 'end_turn',
                        'detected_at': time.time(),
                        'provider_last_activity_at': terminal.last_provider_activity_at,
                    }
            except Exception:
                return None
            return None

        def _close_stdin_for_terminal_recovery():
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

        def _terminate_after_terminal_recovery():
            try:
                proc.terminate()
            except Exception:
                pass

        def _claim_terminal_provider_event(event):
            """Linearize a terminal provider event before observation refresh."""
            nonlocal provider_refusal_seen
            if not isinstance(event, dict):
                return None
            event_type = event.get('type')
            if event_type == 'result':
                if event.get('is_error'):
                    provider_error_value = classify_provider_error_event(
                        event,
                        refusal_marker=provider_refusal_seen,
                    )
                    return {
                        'kind': 'provider_error',
                        'provider_error': provider_error_value,
                        'accepted': authority.accept_provider_error(
                            provider_error_value,
                        ),
                    }
                if event.get('stop_reason') == 'end_turn':
                    receipt = ProviderTerminalReceipt.from_result_event(
                        event,
                        turn_identity=terminal.turn_identity,
                        process_generation=self._generation,
                        claude_session_id=self._session_id,
                    )
                    return {
                        'kind': 'provider_result',
                        'receipt': receipt,
                        'accepted': authority.accept_provider_result(receipt),
                    }
                return None
            if (
                event_type == 'system'
                and event.get('subtype') == 'model_refusal_no_fallback'
            ):
                provider_refusal_seen = True
                provider_error_value = classify_provider_error_event(
                    event,
                    refusal_marker=True,
                )
                return {
                    'kind': 'provider_error',
                    'provider_error': provider_error_value,
                    'accepted': authority.accept_provider_error(
                        provider_error_value,
                    ),
                }
            if event_type == 'assistant' and event.get('isApiErrorMessage') is True:
                provider_error_value = classify_provider_error_event(
                    event,
                    refusal_marker=provider_refusal_seen,
                )
                return {
                    'kind': 'provider_error',
                    'provider_error': provider_error_value,
                    'accepted': authority.accept_provider_error(
                        provider_error_value,
                    ),
                }
            return None

        def _cleanup_after_terminal(reason, *, allow_success=False):
            if not authority.begin_cleanup(reason, allow_success=allow_success):
                return False
            process_present = self._proc is not None
            try:
                self._kill(quiet=True)
            finally:
                authority.record_resident_kill(
                    reason,
                    process_present=process_present,
                )
            return True

        def _submit_recovery_result_missing():
            if not authority.submit_timeout_candidate('cli_terminal_result_missing'):
                return False
            _capture_timeout_diagnostics('cli_terminal_result_missing')
            _cleanup_after_terminal('cli_terminal_result_missing')
            return True

        def _submit_recovery_hard_timeout():
            return _submit_timeout_candidate('hard')

        def _start_terminal_recovery(probe_generation=None):
            nonlocal recovery
            if recovery is not None and recovery.active:
                return True
            proof = _durable_current_turn_end_turn_proof()
            if proof is None:
                return False
            # This revalidates watchdog state/generation and claims the
            # non-terminal lifecycle gate atomically under the authority lock.
            # No transcript scan or process side effect occurs in either lock.
            if not watchdog.claim_recovery(
                probe_generation,
                lambda: authority.claim_terminal_recovery(probe_generation),
            ):
                return False
            # A live result may win immediately after the lifecycle claim.
            # Serialize that result's teardown decision against controller
            # start so late recovery can never close stdin after a normal
            # SUCCESS that won before the claim.
            with recovery_start_lock:
                if authority.outcome != TurnTerminalAuthority.RUNNING:
                    return True
                recovery = TerminalRecoveryController(
                    eof_grace=recovery_eof_grace,
                    sigterm_grace=recovery_sigterm_grace,
                    hard_deadline=watchdog.hard_deadline,
                    close_stdin=_close_stdin_for_terminal_recovery,
                    send_sigterm=_terminate_after_terminal_recovery,
                    submit_hard_timeout=_submit_recovery_hard_timeout,
                    submit_result_missing=_submit_recovery_result_missing,
                )
                return recovery.start(proof)

        def _finish_recovered_success():
            """Serialize recovered SUCCESS teardown after the lifecycle claim."""
            nonlocal recovery_success_nonreusable
            if not authority.recovery_claimed:
                return False
            with recovery_start_lock:
                if recovery is not None and recovery.active:
                    recovery.mark_terminal(TurnTerminalAuthority.SUCCESS)
                cleaned = _cleanup_after_terminal(
                    'terminal_recovery_nonreusable',
                    allow_success=True,
                )
                recovery_success_nonreusable = True
                self._cold = True
                self._next_spawn_reason = 'terminal_recovery'
                return cleaned

        def _on_recovery_probe(probe_generation=None):
            """Probe only; no durable proof means ordinary stall remains later."""
            return _start_terminal_recovery(probe_generation)

        # Watchdog timeout ownership is linearized by the same authority as
        # provider terminal events. Only the winner is allowed to capture
        # diagnostics and kill; outer exception/finalizer paths can only
        # observe the already-consumed cleanup token.
        def _submit_timeout_candidate(reason):
            if not authority.submit_timeout_candidate(reason):
                return False
            _capture_timeout_diagnostics(reason)
            _cleanup_after_terminal('watchdog:%s' % str(reason or 'timeout'))
            return True

        watchdog = StreamWatchdog(
            stall_timeout=stall_timeout,
            hard_timeout=hard_timeout,
            on_timeout=_submit_timeout_candidate,
            is_proc_alive=lambda: proc.poll() is None,
            probe_timeout=recovery_probe,
            on_probe=_on_recovery_probe,
        )
        watchdog.start()

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
                    if authority.is_terminal():
                        break
                    if terminal.grace_expired():
                        authority.submit_timeout_candidate(
                            'result_missing_after_end_turn',
                        )
                        break
                    ready = True
                    _set_reader_state('WAIT_SELECT')
                    try:
                        poll_interval = heartbeat_interval if use_idle_heartbeat else 0.2
                        ready, _, _ = select.select(
                            [proc.stdout], [], [], poll_interval,
                        )
                    except (AttributeError, OSError, TypeError, ValueError):
                        ready = True
                    if not ready:
                        if proc.poll() is not None:
                            authority.accept_disconnected('process_exit')
                            break
                        if authority.is_terminal():
                            break
                        if terminal.grace_expired():
                            authority.submit_timeout_candidate(
                                'result_missing_after_end_turn',
                            )
                            break
                        if use_idle_heartbeat:
                            yield from _downstream_yield(('heartbeat', None))
                        continue
                    _set_reader_state('READLINE')
                    raw_line = proc.stdout.readline()
                    if raw_line == '':
                        _set_reader_state('TERMINAL_DRAIN')
                        if recovery is not None and recovery.active:
                            recovery.wait_for_final()
                            if authority.is_terminal():
                                break
                        authority.accept_disconnected('stdout_eof')
                        break
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    _set_reader_state('PROCESS_EVENT')
                    # A terminal provider event must claim the sole authority
                    # before observation bookkeeping or watchdog refresh. This
                    # closes the deterministic result-vs-stall window without
                    # holding a lock across the yielding stream paths below.
                    claimed_terminal = _claim_terminal_provider_event(d)
                    if recovery is not None and recovery.active:
                        recovery.note_event(d)
                        if claimed_terminal is not None and claimed_terminal.get('accepted'):
                            recovery.mark_terminal(authority.outcome)
                    terminal.observe(d)
                    if _claude_event_is_activity(d):
                        watchdog.note_activity()
                    self._maybe_set_session_id(d)
                    if turn_session_id is None and self._session_id:
                        turn_session_id = self._session_id
                    if jsonl_cursor is None and self._session_id:
                        try:
                            from tools.cc_jsonl_usage import snapshot_session_jsonl
                            jsonl_cursor = snapshot_session_jsonl(
                                self._cwd, self._session_id,
                            )
                            if jsonl_cursor is not None and self._cold:
                                jsonl_cursor = dict(jsonl_cursor)
                                jsonl_cursor['offset'] = 0
                        except Exception:
                            pass
                    t = d.get('type')
                    if t == 'system' and d.get('subtype') == 'init':
                        self._session_id = d.get('session_id') or self._session_id
                    elif t == 'system' and d.get('subtype') == 'model_refusal_no_fallback':
                        provider_refusal_seen = True
                        provider_error = claimed_terminal['provider_error']
                        break
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
                                    yield from _downstream_yield(('text', chunk))
                            elif delta.get('type') == 'thinking_delta':
                                chunk = delta.get('thinking', '')
                                if chunk:
                                    think_acc.append(chunk)
                                    yield from _downstream_yield(('think', chunk))
                    elif t == 'assistant':
                        if d.get('isApiErrorMessage') is True:
                            provider_error = claimed_terminal['provider_error']
                            break
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
                            if isinstance(b, dict) and b.get('type') == 'text':
                                block_text = str(b.get('text') or '')
                                if block_text:
                                    provider_text_acc.append(block_text)
                            elif isinstance(b, dict) and b.get('type') == 'tool_use':
                                # Tool use is not a provider-request boundary.
                                # Keep this round open: Claude may emit the final
                                # message_delta usage for the same request after
                                # the assistant tool_use event.
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
                                yield from _downstream_yield(('tool_use', tool_payload))
                    elif t == 'user':
                        for b in ((d.get('message') or {}).get('content') or []):
                            if isinstance(b, dict) and b.get('type') == 'tool_result':
                                rc = b.get('content')
                                if isinstance(rc, list):
                                    rc = ''.join(x.get('text', '') for x in rc if isinstance(x, dict))
                                yield from _downstream_yield(('tool_result', {
                                    'tool_use_id': b.get('tool_use_id'),
                                    'result': str(rc or ''),
                                    'is_error': bool(b.get('is_error')),
                                }))
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
                            authority.accept_deferred()
                            _cleanup_after_terminal('provider_tool_deferred')
                            yield from _downstream_yield(('tool_use', deferred_payload))
                            break
                        if d.get('is_error'):
                            provider_error = claimed_terminal['provider_error']
                        elif d.get('stop_reason') == 'end_turn':
                            if (
                                claimed_terminal is not None
                                and claimed_terminal['kind'] == 'provider_result'
                                and claimed_terminal['accepted']
                            ):
                                terminal_receipt = claimed_terminal['receipt']
                                _finish_recovered_success()
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
                authority.accept_disconnected('generator_exit')
                if recovery is not None and recovery.active:
                    recovery.mark_terminal(authority.outcome)
                _cleanup_after_terminal('client_disconnect')
                raise
            except BaseException:
                if not authority.is_terminal():
                    authority.accept_disconnected('stream_exception')
                if recovery is not None and recovery.active:
                    recovery.mark_terminal(authority.outcome)
                _cleanup_after_terminal('stream_exception')
                raise
        finally:
            watchdog.stop()
            if uh_a0_runtime is not None:
                uh_a0_runtime.end_turn(turn_id=uh_a0_turn_id)
            if recovery is not None and recovery.active:
                recovery.wait_for_final()

        if current_round is not None:
            _close_round(current_round, 'loop_finalizer', complete=False)
            current_round = None

        if not authority.is_terminal():
            authority.accept_disconnected('stdout_eof')
        outcome = authority.outcome
        if outcome != TurnTerminalAuthority.SUCCESS:
            _cleanup_after_terminal(authority.reason or 'terminal_failure')
        authority_obs = authority.snapshot()
        recovery_obs = (
            recovery.snapshot(final_outcome=authority_obs['terminal_outcome'])
            if recovery is not None and recovery.active else None
        )

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
        usage['_obs_runtime_identity'] = self._runtime_identity
        usage['_obs_last_provider_event_type'] = terminal.last_provider_event_type
        usage['_obs_last_provider_activity_at'] = terminal.last_provider_activity_at
        usage['_obs_end_turn_seen'] = bool(terminal.end_turn_seen)
        usage['_obs_end_turn_seen_at'] = terminal.end_turn_seen_at
        usage['_obs_result_seen'] = bool(terminal.result_seen)
        usage['_obs_terminal_outcome'] = authority_obs['terminal_outcome']
        usage['_obs_terminal_reason'] = authority_obs['terminal_reason']
        usage['_obs_timeout_candidate_type'] = authority_obs['timeout_candidate_type']
        usage['_obs_timeout_candidate_received_at'] = authority_obs[
            'timeout_candidate_received_at'
        ]
        usage['_obs_result_accepted_at'] = authority_obs['result_accepted_at']
        usage['_obs_terminal_linearization_source'] = authority_obs[
            'terminal_linearization_source'
        ]
        usage['_obs_resident_killed'] = authority_obs['resident_killed']
        usage['_obs_resident_kill_reason'] = authority_obs[
            'resident_kill_reason'
        ]
        usage['_obs_stale_stall_rejected_count'] = authority_obs[
            'stale_stall_rejected_count'
        ]
        usage['_obs_actual_stall_accepted_count'] = authority_obs[
            'actual_stall_accepted_count'
        ]
        usage['_obs_late_result_rejected_count'] = authority_obs[
            'late_result_rejected_count'
        ]
        usage['_obs_duplicate_terminal_signal_count'] = authority_obs[
            'duplicate_terminal_signal_count'
        ]
        usage['_obs_timeout_diagnostics'] = authority_obs.get(
            'timeout_diagnostics',
        ) or None
        usage['_obs_terminal_recovery'] = recovery_obs
        usage['_obs_cleanup_count'] = authority_obs['cleanup_count']
        # Compatibility telemetry from Phase A remains, while root-path
        # stale-stall rejection is measured separately above. This is legacy
        # compatibility output only; no terminal reconciliation reads it.
        usage['_obs_stall_result_race_recovered'] = False
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

        if outcome == TurnTerminalAuthority.DEFERRED:
            return
        if outcome == TurnTerminalAuthority.PROVIDER_ERROR:
            if provider_error is None:
                provider_error = {
                    'public_message': 'Claude provider 请求失败；本轮未自动重试。',
                    'error_code': 'provider_error',
                    'provider_error_type': 'provider_api_error',
                    'provider_error_category': 'provider_request_failure',
                    'turn_failure_class': 'TURN_LEVEL',
                    'retryable': False,
                }
            raise ResidentError(
                provider_error['public_message'],
                usage=usage,
                diagnostics=authority_obs,
                error_code=provider_error['error_code'],
                provider_error_type=provider_error['provider_error_type'],
                provider_error_category=provider_error['provider_error_category'],
                turn_failure_class=provider_error['turn_failure_class'],
                retryable=provider_error['retryable'],
            )
        if outcome == TurnTerminalAuthority.STALL:
            raise ResidentError(
                'claude code 长时间无活动 (%ds)，resident 进程已重启' % stall_timeout,
                usage=usage,
                diagnostics=authority_obs,
                error_code='provider_stall_timeout',
            )
        if outcome == TurnTerminalAuthority.HARD_TIMEOUT:
            raise ResidentError(
                'claude code 单轮超过绝对上限 (%ds)，resident 进程已重启' % hard_timeout,
                usage=usage,
                diagnostics=authority_obs,
                error_code='provider_hard_timeout',
            )
        if outcome == TurnTerminalAuthority.RESULT_MISSING_AFTER_END_TURN:
            raise ResidentError(
                '回复收尾异常：provider 已报告 end_turn，但 result 未到达',
                usage=usage,
                diagnostics=authority_obs,
                error_code='result_missing_after_end_turn',
            )
        if outcome == TurnTerminalAuthority.CLI_TERMINAL_RESULT_MISSING:
            raise ResidentError(
                'Claude Code 已持久化完成回复，但 live terminal/result 未到达',
                usage=usage,
                diagnostics=authority_obs,
                error_code='cli_terminal_result_missing',
            )
        if outcome == TurnTerminalAuthority.DISCONNECTED:
            raise ResidentError(
                'resident 进程在本轮回复完成前退出',
                usage=usage,
                diagnostics=authority_obs,
                error_code='result_missing_before_terminal',
            )
        if outcome != TurnTerminalAuthority.SUCCESS or terminal_receipt is None:
            raise ResidentError(
                'resident 进程在本轮回复完成前退出',
                usage=usage,
                diagnostics=authority_obs,
                error_code='result_missing_before_terminal',
            )
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
        usage = ResidentTurnUsage(
            usage,
            terminal_receipt=terminal_receipt,
            candidate_cache_refresh_at=candidate_cache_refresh_at,
            candidate_cache_refresh_monotonic=candidate_cache_refresh_monotonic,
        )

        self._cold = False
        if recovery_success_nonreusable:
            self._cold = True
        self._last_used = time.time()
        self._resident_turn_count += 1
        self._turns_since_respawn += 1
        self._pending_respawn_reason = None
        self._last_round_context = int(usage.get('last_round_context') or 0)
        self._max_round_context = max(self._max_round_context, int(usage.get('max_round_context') or 0))
        usage['resident_turn_count'] = self._resident_turn_count
        usage['max_round_context'] = self._max_round_context
        # claims 只经 done 内部回传，不得写入公开 cache_info
        provider_text = ''.join(provider_text_acc).strip()
        final_text = provider_text or ''.join(text_acc).strip()
        yield ('done', (final_text, ''.join(think_acc), usage, one_shot_claims))

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
