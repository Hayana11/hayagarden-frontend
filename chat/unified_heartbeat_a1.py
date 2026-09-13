"""UH-A1 shared-resident transcript and delivery guards.

A normal Wake Renderer turn is intentionally part of the same Claude session,
but it is not a formal application Chat round and therefore has no
``daily_message_contexts`` user/assistant pair. The Session Registry scan
watermark must explicitly skip that provider-only range, otherwise the next
formal Chat mapping pass sees an extra complete transcript round and blocks.

If the application executor later cannot deliver/settle that generated Wake,
the hot resident must not keep an undelivered assistant turn as conversational
truth. In that failure case we retire only the resident that still belongs to
the same frozen window identity, and only while no real Chat generation owns
the shared generation lock. A newer/busy resident is never killed.

No function here creates sessions, takes ownership, respawns a resident,
creates a second cursor, or invents message mappings.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from chat import daily_runtime as dr
from chat.session_registry import (
    SCAN_STATUS_READY,
    cas_advance_scan_offset,
    get_context_claude_session,
)


@dataclass(frozen=True)
class SharedTranscriptWatermark:
    context_id: int
    context_epoch: int
    resident_generation: int
    resident_key: str
    claude_session_id: str
    transcript_path: str
    expected_offset: int
    process_generation: int




def _resident_pid(resident):
    pid = getattr(resident, 'resident_pid', None)
    if pid is not None:
        return pid
    proc = getattr(resident, '_proc', None)
    return getattr(proc, 'pid', None) if proc is not None else None


def _resident_alive(resident):
    try:
        alive = getattr(resident, '_alive', None)
        if callable(alive):
            return bool(alive())
    except Exception:
        pass
    proc = getattr(resident, '_proc', None)
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False


_CLEANUP_REASON_ALLOWLIST = frozenset({
    'delivery_failed',
    'normal_wake_shared_unavailable',
    'normal_wake_main_chat_failed',
    'normal_wake_main_chat_jsonl_not_final',
    'normal_wake_main_chat_delivery_failed',
    'delivery_succeeded',
})


def _safe_cleanup_reason(reason):
    value = str(reason or '').strip()
    return value if value in _CLEANUP_REASON_ALLOWLIST else 'unknown'


def _log_resident_cleanup(stage, *, resident_generation, resident_pid=None,
                           local_binding_present=None, reason=None,
                           resident_alive=None, close_return=None):
    payload = {
        'stage': str(stage),
    }
    if resident_generation is not None:
        payload['resident_generation'] = resident_generation
    if resident_pid is not None:
        payload['resident_pid'] = resident_pid
    if local_binding_present is not None:
        payload['local_binding_present'] = bool(local_binding_present)
    if reason is not None:
        payload['reason'] = _safe_cleanup_reason(reason)
    if resident_alive is not None:
        payload['resident_alive'] = bool(resident_alive)
    if close_return is not None:
        payload['close_return'] = bool(close_return)
    logging.getLogger(__name__).info(
        '[WAKE-LIVE] %s',
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


class SharedWakeDeliveryFence:
    """Keep the Chat generation owner until Wake delivery is final."""

    def __init__(self, *, gateway: Any, resident: Any):
        self.gateway = gateway
        self.resident = resident
        self.token = object()
        self._finished = False

    def finish(
        self,
        delivered: bool,
        *,
        cache_info: Any = None,
        window_identity: Any = None,
        reason: str = 'delivery_failed',
    ) -> None:
        if self._finished:
            return
        try:
            if not delivered:
                retire_shared_resident_after_failed_delivery(
                    cache_info=cache_info,
                    window_identity=window_identity,
                    delivery_token=self.token,
                    reason=reason,
                )
        finally:
            self.gateway._gen_release(
                None,
                expected_pending_token=self.token,
            )
            self._finished = True


def begin_shared_wake_delivery_fence(*, gateway: Any, resident: Any) -> SharedWakeDeliveryFence:
    fence = SharedWakeDeliveryFence(gateway=gateway, resident=resident)
    gateway._gen_mark_pending_delivery(fence.token)
    return fence


def _read_complete_shared_transcript_end_offset(
    watermark: SharedTranscriptWatermark,
) -> int:
    """Return EOF only after Reader proves one canonical terminal round."""
    from chat.claude_event_mapping import _assert_complete_terminal_round
    from chat.claude_transcript_reader import read_transcript_range

    last_error = 'uh_a1_watermark_transcript_not_final'
    for delay in (0.0, 0.05, 0.15, 0.35):
        if delay:
            time.sleep(delay)
        try:
            end_offset = int(os.path.getsize(watermark.transcript_path))
        except OSError as exc:
            last_error = 'uh_a1_watermark_transcript_unreadable'
            continue
        if end_offset <= int(watermark.expected_offset):
            last_error = 'uh_a1_watermark_transcript_not_grown'
            continue
        try:
            graph = read_transcript_range(
                watermark.transcript_path,
                int(watermark.expected_offset),
                end_offset,
            )
            session_ids = {
                str(event.session_id or '').strip()
                for event in graph.events
            }
            if session_ids != {str(watermark.claude_session_id)}:
                last_error = 'uh_a1_watermark_transcript_session_mismatch'
                continue
            if len(graph.candidate_rounds) != 1:
                last_error = 'uh_a1_watermark_transcript_round_shape'
                continue
            _assert_complete_terminal_round(graph, graph.candidate_rounds[0])
        except Exception:
            # The reader/mapping standard is deliberately fail-closed while
            # Claude may still be appending the canonical assistant row.
            last_error = 'uh_a1_watermark_transcript_round_incomplete'
            continue
        return end_offset
    raise RuntimeError(last_error)


def prepare_shared_transcript_watermark(
    resident: Any,
    *,
    db_path: str,
) -> tuple[Optional[SharedTranscriptWatermark], str]:
    """Return exact pre-turn watermark only when Registry is fully caught up."""
    try:
        binding = dr.get_local_binding()
        if binding is None:
            return None, 'no_local_binding'

        registry = get_context_claude_session(
            int(binding.context_id),
            int(binding.resident_generation),
            db_path=db_path,
        )
        if registry is None:
            return None, 'registry_missing'
        if str(registry.get('scan_status') or '') != SCAN_STATUS_READY:
            return None, 'registry_not_ready'

        live_sid = str(getattr(resident, 'session_id', None) or '').strip()
        reg_sid = str(registry.get('claude_session_id') or '').strip()
        if not live_sid or live_sid != reg_sid:
            return None, 'registry_session_mismatch'

        process_generation = int(getattr(resident, 'generation', 0) or 0)
        reg_process_generation = registry.get('process_generation')
        if (
            reg_process_generation is not None
            and int(reg_process_generation or 0) != process_generation
        ):
            return None, 'registry_process_generation_mismatch'

        path = str(registry.get('transcript_path') or '').strip()
        if not path:
            return None, 'registry_transcript_path_missing'
        try:
            size = int(os.path.getsize(path))
        except OSError:
            return None, 'registry_transcript_unreadable'

        offset = int(registry.get('scan_offset') or 0)
        if offset != size:
            # Do not hide an older mapping backlog under a Wake skip.
            return None, 'registry_not_caught_up'

        return SharedTranscriptWatermark(
            context_id=int(binding.context_id),
            context_epoch=int(binding.context_epoch),
            resident_generation=int(binding.resident_generation),
            resident_key=str(binding.resident_key),
            claude_session_id=live_sid,
            transcript_path=path,
            expected_offset=offset,
            process_generation=process_generation,
        ), 'ok'
    except Exception:
        return None, 'watermark_prepare_error'


def commit_shared_transcript_watermark(
    watermark: SharedTranscriptWatermark,
    resident: Any,
    *,
    db_path: str,
    jsonl_finality: Any,
) -> dict[str, Any]:
    """CAS-skip exactly one completed provider-only Wake transcript range."""
    binding = dr.get_local_binding()
    if binding is None:
        raise RuntimeError('uh_a1_watermark_binding_missing')
    if str(binding.resident_key) != str(watermark.resident_key):
        raise RuntimeError('uh_a1_watermark_binding_changed')
    if int(binding.context_id) != int(watermark.context_id):
        raise RuntimeError('uh_a1_watermark_context_changed')
    if int(binding.context_epoch) != int(watermark.context_epoch):
        raise RuntimeError('uh_a1_watermark_epoch_changed')
    if int(binding.resident_generation) != int(watermark.resident_generation):
        raise RuntimeError('uh_a1_watermark_generation_changed')

    live_sid = str(getattr(resident, 'session_id', None) or '').strip()
    if live_sid != str(watermark.claude_session_id):
        raise RuntimeError('uh_a1_watermark_session_changed')
    if int(getattr(resident, 'generation', 0) or 0) != int(
        watermark.process_generation
    ):
        raise RuntimeError('uh_a1_watermark_process_generation_changed')

    if not isinstance(jsonl_finality, Mapping):
        raise RuntimeError('uh_a1_watermark_jsonl_finality_missing')
    if jsonl_finality.get('stream_totals_match') is not True:
        # cc_resident already performs the bounded JSONL replay retry. A
        # non-matching proof means the provider tail is still incomplete, so
        # never advance the mapping cursor to a guessed file size.
        raise RuntimeError('uh_a1_watermark_jsonl_not_final')

    end_offset = _read_complete_shared_transcript_end_offset(watermark)

    updated = cas_advance_scan_offset(
        context_id=int(watermark.context_id),
        resident_generation=int(watermark.resident_generation),
        expected_offset=int(watermark.expected_offset),
        new_offset=end_offset,
        last_mapped_message_id=None,
        scan_status=SCAN_STATUS_READY,
        scan_error_code=None,
        db_path=db_path,
    )
    if int(updated.get('scan_offset') or -1) != end_offset:
        raise RuntimeError('uh_a1_watermark_commit_mismatch')
    return {
        'context_id': int(watermark.context_id),
        'resident_generation': int(watermark.resident_generation),
        'start_offset': int(watermark.expected_offset),
        'end_offset': end_offset,
        'skipped_provider_round': True,
    }


def _is_shared_b3_cache_info(cache_info: Any) -> bool:
    if not isinstance(cache_info, Mapping):
        return False
    return (
        str(cache_info.get('provider') or '').strip() == 'claude_code'
        and str(cache_info.get('source') or '').strip() == 'wake'
        and bool(cache_info.get('b3_authority'))
    )


def retire_shared_resident_after_failed_delivery(
    *,
    cache_info: Any,
    window_identity: Any,
    delivery_token: Any = None,
    reason: str = 'delivery_failed',
) -> bool:
    """Retire only an idle, still-bound resident after undelivered B3 output."""
    if not _is_shared_b3_cache_info(cache_info):
        return False
    try:
        import config_store
        if not config_store.get_bool('UNIFIED_NORMAL_WAKE_ENABLED', default=False):
            return False
    except Exception:
        return False

    if not isinstance(window_identity, Mapping):
        return False
    try:
        want_context = int(window_identity.get('context_id'))
        want_epoch = int(window_identity.get('context_epoch'))
        want_generation = int(window_identity.get('resident_generation'))
    except (TypeError, ValueError):
        return False

    binding = dr.get_local_binding()
    if binding is None:
        return False
    if int(binding.context_id) != want_context:
        return False
    if int(binding.context_epoch) != want_epoch:
        return False
    if int(binding.resident_generation) != want_generation:
        return False

    try:
        import gateway
        resident = getattr(gateway, '_CC_RESIDENT', None)
        gen_cond = getattr(gateway, '_gen_cond', None)
        if resident is None or gen_cond is None:
            return False
        # Atomic with the same condition lock used by Chat generation acquire:
        # never kill a resident already serving a real Chat, and prevent a new
        # Chat from grabbing it between our busy check and close.
        with gen_cond:
            pending_token = getattr(gateway, '_gen_pending_delivery', None)
            if bool(getattr(gateway, '_gen_busy', False)) and (
                delivery_token is None or pending_token is not delivery_token
            ):
                return False
            current = dr.get_local_binding()
            if current is None or str(current.resident_key) != str(binding.resident_key):
                return False
            _log_resident_cleanup(
                'RESIDENT_CLEANUP_BEGIN',
                resident_generation=getattr(resident, 'generation', None),
                resident_pid=_resident_pid(resident),
                local_binding_present=True,
                reason=reason,
            )
            close_return = dr.close_local_resident_if_bound(
                resident,
                expected_key=str(binding.resident_key),
            )
            _log_resident_cleanup(
                'RESIDENT_CLEANUP_END',
                resident_generation=getattr(resident, 'generation', None),
                resident_alive=_resident_alive(resident),
                local_binding_present=(dr.get_local_binding() is not None),
                close_return=close_return,
            )
            return bool(close_return)
    except Exception:
        return False
