"""UH-A1 shared-resident transcript watermark guard.

A normal Wake Renderer turn is intentionally part of the same Claude session,
but it is not a formal application Chat round and therefore has no
``daily_message_contexts`` user/assistant pair.  The Session Registry scan
watermark must explicitly skip that provider-only range, otherwise the next
formal Chat mapping pass sees an extra complete transcript round and blocks.

This module only advances an already-READY registry row from the exact JSONL
size observed immediately before the shared Wake turn to the exact size after
that turn.  It never creates sessions, takes ownership, respawns residents, or
invents mappings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

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

    try:
        end_offset = int(os.path.getsize(watermark.transcript_path))
    except OSError as exc:
        raise RuntimeError('uh_a1_watermark_transcript_unreadable') from exc
    if end_offset <= int(watermark.expected_offset):
        raise RuntimeError('uh_a1_watermark_transcript_not_grown')

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
