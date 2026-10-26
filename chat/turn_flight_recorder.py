"""Observation-only lifecycle recorder for one logical chat turn.

The recorder is deliberately not an authority: it never acquires/releases a
generation lock, changes leases, starts/stops a resident, or decides terminal
state. It only records bounded lifecycle evidence supplied by existing
authoritative paths and produces one terminal snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import threading
import time
import uuid
from typing import Any


_SAFE_FIELDS = frozenset({
    'event',
    'stage',
    'observed_at',
    'turn_observation_id',
    'user_message_id',
    'assistant_message_id',
    'turn_key',
    'request_id',
    'context_id',
    'context_epoch',
    'resident_generation',
    'resident_turn_sequence',
    'process_generation',
    'resident_key_hash',
    'claude_session_id_hash',
    'stream_instance_id',
    'stream_role',
    'terminal_reason',
    'terminal_status',
    'error_code',
    'cleanup_action',
    'provider_event_seen',
    'provider_request_id_seen',
    'stdin_write_started',
    'stdin_flushed',
    'assistant_persisted',
    'mapping_committed',
    'reconcile_applied',
    'lock_age_sec',
    'owner_alive',
    'resident_active',
    'turn_terminal',
    'unlock_applied',
    'unlock_skipped',
    'reason',
    'source',
    'tool_name',
    'success',
    'latency_ms',
    'same_turn_collisions',
    'different_turn_collisions',
    'provider_request_count',
    'stream_count',
    'last_completed_stage',
    'next_missing_stage',
})

_BOOL_FIELDS = frozenset({
    'provider_event_seen',
    'provider_request_id_seen',
    'stdin_write_started',
    'stdin_flushed',
    'assistant_persisted',
    'mapping_committed',
    'reconcile_applied',
    'owner_alive',
    'resident_active',
    'turn_terminal',
    'unlock_applied',
    'unlock_skipped',
    'success',
})

_INT_FIELDS = frozenset({
    'user_message_id',
    'assistant_message_id',
    'context_id',
    'context_epoch',
    'resident_generation',
    'resident_turn_sequence',
    'process_generation',
    'lock_age_sec',
    'latency_ms',
    'same_turn_collisions',
    'different_turn_collisions',
    'provider_request_count',
    'stream_count',
})


_PROGRESS_STAGES = frozenset({
    'turn_created',
    'user_message_persisted',
    'generation_lock_acquired',
    'resident_selected',
    'resident_turn_started',
    'stdin_begin',
    'stdin_flushed',
    'claude_init_seen',
    'provider_started',
    'provider_request_observed',
    'first_output',
    'provider_terminal',
    'assistant_persisted',
    'terminal_mapping_committed',
    'turn_reconciled',
    'generation_lock_released',
})

_NEXT_MISSING_STAGE = {
    'turn_created': 'user_message_persisted',
    'user_message_persisted': 'generation_lock_acquired',
    'generation_lock_acquired': 'resident_selected',
    'resident_selected': 'resident_turn_started',
    'resident_turn_started': 'stdin_begin',
    'stdin_begin': 'stdin_flushed',
    'stdin_flushed': 'provider_started',
    'claude_init_seen': 'provider_started',
    'provider_started': 'first_output',
    'provider_request_observed': 'first_output',
    'first_output': 'provider_terminal',
    'provider_terminal': 'assistant_persisted',
    'assistant_persisted': 'terminal_mapping_committed',
}


def hash_identity(value: Any) -> str | None:
    """Return a stable hash for a resident/session identity, never the raw value."""
    text = str(value or '').strip()
    return hashlib.sha256(text.encode('utf-8')).hexdigest() if text else None


class TurnFlightRecorder:
    """A bounded, thread-safe recorder for one logical turn observation."""

    def __init__(
        self,
        *,
        user_message_id: Any = None,
        assistant_message_id: Any = None,
        turn_key: Any = None,
        request_id: Any = None,
        context_id: Any = None,
        context_epoch: Any = None,
        resident_generation: Any = None,
        resident_turn_sequence: Any = None,
        process_generation: Any = None,
        resident_key_hash: Any = None,
        claude_session_id_hash: Any = None,
        stream_instance_id: str | None = None,
        stream_role: str = 'original',
        turn_observation_id: str | None = None,
    ):
        self._lock = threading.RLock()
        self.turn_observation_id = str(turn_observation_id or uuid.uuid4().hex)
        self.stream_instance_id = str(stream_instance_id or uuid.uuid4().hex)
        self.stream_role = str(stream_role or 'original')
        self._identity: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._dedupe_keys: set[tuple[str, str]] = set()
        self._terminal_snapshot: dict[str, Any] | None = None
        self._provider_request_hashes: set[str] = set()
        self._terminal_emitted = False
        self._bind_locked(
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            turn_key=turn_key,
            request_id=request_id,
            context_id=context_id,
            context_epoch=context_epoch,
            resident_generation=resident_generation,
            resident_turn_sequence=resident_turn_sequence,
            process_generation=process_generation,
            resident_key_hash=resident_key_hash,
            claude_session_id_hash=claude_session_id_hash,
        )

    def _bind_locked(self, **fields: Any) -> None:
        for key, value in fields.items():
            if key not in _SAFE_FIELDS or value in (None, ''):
                continue
            self._identity[key] = self._normalize_value(key, value)

    @staticmethod
    def _normalize_value(key: str, value: Any) -> Any:
        if key in _BOOL_FIELDS:
            return bool(value)
        if key in _INT_FIELDS:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        if isinstance(value, (dict, list, tuple, set)):
            return None
        return str(value)[:240]

    def bind(self, **fields: Any) -> None:
        with self._lock:
            self._bind_locked(**fields)

    def logical_identity(self) -> dict[str, Any]:
        """Return the logical-turn identity without exposing provider details."""
        with self._lock:
            return {
                key: self._identity[key]
                for key in ('user_message_id', 'turn_key')
                if self._identity.get(key) not in (None, '')
            }

    def set_stream_role(self, stream_role: str) -> None:
        with self._lock:
            self.stream_role = str(stream_role or self.stream_role)

    def _event_envelope_locked(
        self,
        stage: str,
        event: str,
        *,
        dedupe: bool,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        stream_id = str(fields.get('stream_instance_id') or self.stream_instance_id)
        dedupe_key = (event, stream_id)
        if dedupe and dedupe_key in self._dedupe_keys:
            return None
        if dedupe:
            self._dedupe_keys.add(dedupe_key)
        envelope: dict[str, Any] = {
            **self._identity,
            'event': str(event),
            'stage': str(stage),
            'observed_at': round(time.time(), 6),
            'turn_observation_id': self.turn_observation_id,
            'stream_instance_id': stream_id,
            'stream_role': str(fields.get('stream_role') or self.stream_role),
        }
        for key, value in fields.items():
            if key not in _SAFE_FIELDS or key in envelope or value in (None, ''):
                continue
            normalized = self._normalize_value(key, value)
            if normalized is not None:
                envelope[key] = normalized
        return envelope

    def _record_provider_request_locked(
        self,
        request_identity: Any,
    ) -> dict[str, Any] | None:
        """Count one new provider request without retaining its raw ID."""
        request_hash = hash_identity(request_identity)
        if not request_hash or request_hash in self._provider_request_hashes:
            return None
        self._provider_request_hashes.add(request_hash)
        return self._event_envelope_locked(
            'provider_request_observed',
            'provider_request_observed',
            dedupe=False,
            fields={
                'provider_request_count': len(self._provider_request_hashes),
            },
        )

    def record(
        self,
        stage: str,
        *,
        event: str | None = None,
        dedupe: bool = False,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Append a bounded event; never makes a lifecycle decision."""
        with self._lock:
            if stage == 'provider_request_observed':
                return_value = self._record_provider_request_locked(
                    fields.pop('_request_identity', None),
                )
                if return_value is not None:
                    self._events.append(return_value)
                return copy.deepcopy(return_value)
            envelope = self._event_envelope_locked(
                stage,
                str(event or stage),
                dedupe=dedupe,
                fields=fields,
            )
            if envelope is None:
                return None
            self._events.append(envelope)
            return copy.deepcopy(envelope)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._events)

    def _summary_locked(self, *, terminal_status: str, **fields: Any) -> dict[str, Any]:
        streams = {
            str(item.get('stream_instance_id'))
            for item in self._events
            if item.get('stream_instance_id')
        }
        streams.add(self.stream_instance_id)
        same_turn = sum(
            1 for item in self._events
            if item.get('event') == 'same_turn_reconnect_detected'
        )
        different_turn = sum(
            1 for item in self._events
            if item.get('event') == 'different_turn_lock_collision'
        )
        last_event = self._events[-1] if self._events else None
        last_completed_stage = last_event.get('stage') if last_event else None
        snapshot: dict[str, Any] = {
            **self._identity,
            'event': 'turn_closed',
            'stage': 'turn_closed',
            'observed_at': round(time.time(), 6),
            'turn_observation_id': self.turn_observation_id,
            'terminal_status': str(terminal_status),
            'stream_instance_id': self.stream_instance_id,
            'stream_role': self.stream_role,
            'stream_count': len(streams),
            'same_turn_collisions': same_turn,
            'different_turn_collisions': different_turn,
            'provider_request_count': len(self._provider_request_hashes),
            'last_completed_stage': last_completed_stage,
            'next_missing_stage': _NEXT_MISSING_STAGE.get(last_completed_stage),
            'events': copy.deepcopy(self._events),
        }
        for key, value in fields.items():
            if key not in _SAFE_FIELDS or value in (None, ''):
                continue
            normalized = self._normalize_value(key, value)
            if normalized is not None:
                snapshot[key] = normalized
        return snapshot

    def terminal(self, *, terminal_status: str = 'unknown', **fields: Any) -> dict[str, Any]:
        """Return one terminal snapshot; repeated calls return the same snapshot."""
        with self._lock:
            if self._terminal_snapshot is not None:
                return copy.deepcopy(self._terminal_snapshot)
            self.record(
                'turn_closed',
                event='turn_closed',
                dedupe=True,
                terminal_status=terminal_status,
                **fields,
            )
            self._terminal_snapshot = self._summary_locked(
                terminal_status=terminal_status,
                **fields,
            )
            return copy.deepcopy(self._terminal_snapshot)

    def claim_terminal_emission(self) -> bool:
        """Claim the durable terminal sink exactly once for this turn."""
        with self._lock:
            if self._terminal_emitted:
                return False
            self._terminal_emitted = True
            return True

    @property
    def terminal_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._terminal_snapshot)


def recorder_from(plan: Any) -> TurnFlightRecorder | None:
    recorder = getattr(plan, '_turn_flight_recorder', None) if plan is not None else None
    return recorder if isinstance(recorder, TurnFlightRecorder) else None
