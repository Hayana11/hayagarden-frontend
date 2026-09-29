"""Thin Wake Read Observation on the canonical hot Chat resident.

Derives the observation capability set from the shared Chat/Wake default
auto lease, then keeps only read_auto + side_effect none.  Observation is a
provider-only internal turn: it never bootstraps a resident, never creates a
Wake resident, and never maps into formal Chat user/assistant rows.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Optional

from tools.capability_manifest import get_capability
from tools.lease_signer import (
    default_allowed_capabilities,
    issue_default_policy_subset_lease,
)

_LOG = logging.getLogger('wake_read_observation')

_FACT_LIMIT = 400

_OBSERVATION_PROMPT = (
    '这是一次内部 Wake Read Observation。只读取、不写入，不要对用户说话，'
    '不要决定 Action。需要时调用当前可用的只读工具，然后只返回一个 JSON：'
    '{"observations":[{"source":"capability_id","fact":"短事实"}]}。'
    'fact 尽量短，不要粘贴大段原文。没有值得记下的事实时返回 '
    '{"observations":[]}。'
)


def empty_observation_bundle(*, status: str = 'empty') -> dict[str, Any]:
    return {'observations': (), 'status': str(status or 'empty')}


def observation_bundle_payload(bundle: Any) -> dict[str, Any]:
    """JSON-safe ObservationBundle for Planner extra_context."""
    if not isinstance(bundle, Mapping):
        return {'observations': [], 'status': 'empty'}
    rows: list[dict[str, str]] = []
    for row in bundle.get('observations') or ():
        if not isinstance(row, Mapping):
            continue
        source = str(row.get('source') or '').strip()
        fact = str(row.get('fact') or '').strip()
        if not source or not fact:
            continue
        rows.append({'source': source, 'fact': fact})
    return {
        'observations': rows,
        'status': str(bundle.get('status') or 'empty'),
    }


def format_observation_trigger_appendix(bundle: Any) -> str:
    payload = observation_bundle_payload(bundle)
    rows = payload.get('observations') or []
    if not rows:
        return ''
    lines = ['【Read Observation】']
    for row in rows:
        lines.append('- %s: %s' % (row['source'], row['fact']))
    return '\n'.join(lines)


def wake_observation_capabilities() -> tuple[str, ...]:
    """Read-only observation caps derived from the shared Wake default set."""
    out: list[str] = []
    for capability_id in default_allowed_capabilities('wake'):
        entry = get_capability(capability_id) or {}
        if entry.get('autonomy_mode') != 'read_auto':
            continue
        if entry.get('side_effect') != 'none':
            continue
        out.append(capability_id)
    return tuple(out)


def observation_turn_lease(*, wake_run_id: str, issued_at: str | None = None) -> dict[str, Any]:
    return issue_default_policy_subset_lease(
        turn_id='wake-read-obs:' + str(wake_run_id or 'normal').strip(),
        turn_mode='wake',
        allowed_capabilities=wake_observation_capabilities(),
        issued_at=issued_at,
    )


def _truncate_fact(value: Any) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) <= _FACT_LIMIT:
        return text
    return text[: _FACT_LIMIT - 1].rstrip() + '…'


def _parse_observation_json(text: str) -> list[dict[str, str]]:
    raw = str(text or '').strip()
    if not raw:
        return []
    obj = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            obj = parsed
    except json.JSONDecodeError:
        obj = None
    if obj is None:
        fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if fence:
            try:
                parsed = json.loads(fence.group(1))
                if isinstance(parsed, dict):
                    obj = parsed
            except json.JSONDecodeError:
                obj = None
    if obj is None:
        start = raw.find('{')
        end = raw.rfind('}')
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start:end + 1])
                if isinstance(parsed, dict):
                    obj = parsed
            except json.JSONDecodeError:
                obj = None
    rows = obj.get('observations') if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source = str(row.get('source') or '').strip()
        fact = _truncate_fact(row.get('fact'))
        if not source or not fact:
            continue
        out.append({'source': source, 'fact': fact})
    return out


def _facts_from_tool_events(tool_calls: list[dict[str, Any]]) -> list[dict[str, str]]:
    from tools.execution_fence import capability_for_tool

    out: list[dict[str, str]] = []
    for call in tool_calls:
        name = str(call.get('name') or '').strip()
        source = capability_for_tool(name) or name
        fact = _truncate_fact(call.get('result'))
        if not source or not fact:
            continue
        out.append({'source': source, 'fact': fact})
    return out


def collect_wake_read_observation(
    *,
    wake_run_id: str,
    resident: Any = None,
    db_path: Optional[str] = None,
    gateway_module: Any = None,
) -> dict[str, Any]:
    """Run one hot-only read observation turn, or skip before stdin.

    Pre-turn unavailability returns ``status=unavailable`` with an empty
    bundle so Planner can continue.  After the resident turn starts, failures
    return ``status=failed`` and ``started=True`` so Wake must fail closed.
    """
    result = {
        'status': 'unavailable',
        'reason': 'resident_or_db_unavailable',
        'started': False,
        'bundle': empty_observation_bundle(status='unavailable'),
        'lease': None,
        'watermark_skip': None,
    }
    try:
        from chat.behavior_authority_b3 import _hot_chat_resident_ready
        from chat.cc_history_rewrite import guard_cc_generation
        from chat.unified_heartbeat_a1 import (
            begin_shared_wake_delivery_fence,
            commit_shared_transcript_watermark,
            prepare_shared_transcript_watermark,
        )
    except Exception as exc:
        result['reason'] = 'observation_import_unavailable:' + type(exc).__name__
        return result

    if gateway_module is None:
        import gateway as gateway_module
    if resident is None:
        resident = getattr(gateway_module, '_CC_RESIDENT', None)
    if db_path is None:
        db_path = str(getattr(gateway_module, 'DB_PATH', '') or '')

    if resident is None or not db_path:
        return result

    ready, reason = _hot_chat_resident_ready(resident, db_path=db_path)
    if not ready:
        result['reason'] = reason or 'resident_not_hot'
        return result

    acquired = False
    started = False
    watermark = None
    delivery_fence = None
    try:
        mode, _ = gateway_module._gen_acquire_or_wait(wait_timeout=0)
        if mode != 'own':
            result['reason'] = 'generation_lock_unavailable'
            return result
        acquired = True

        ready, reason = _hot_chat_resident_ready(resident, db_path=db_path)
        if not ready:
            result['reason'] = reason or 'resident_not_hot'
            return result

        watermark, reason = prepare_shared_transcript_watermark(
            resident,
            db_path=db_path,
        )
        if watermark is None:
            result['reason'] = reason or 'watermark_unavailable'
            return result

        lease = observation_turn_lease(wake_run_id=wake_run_id)

        def guarded_events():
            nonlocal delivery_fence, started
            ready_now, reason_now = _hot_chat_resident_ready(
                resident, db_path=db_path,
            )
            if not ready_now:
                raise RuntimeError(reason_now or 'resident_not_hot')
            delivery_fence = begin_shared_wake_delivery_fence(
                gateway=gateway_module,
                resident=resident,
            )
            started = True
            yield from resident.send_turn(
                _OBSERVATION_PROMPT,
                turn_lease=dict(lease),
            )

        text = ''
        usage: dict[str, Any] = {}
        tool_calls: list[dict[str, Any]] = []
        saw_done = False
        for evt, payload in guard_cc_generation(guarded_events()):
            if evt == 'tool_use' and isinstance(payload, dict):
                tool_calls.append({
                    'id': payload.get('id'),
                    'name': payload.get('name'),
                    'args': payload.get('args'),
                    'result': '',
                    'success': True,
                })
            elif evt == 'tool_result' and isinstance(payload, dict):
                index = next(
                    (
                        i for i in range(len(tool_calls) - 1, -1, -1)
                        if tool_calls[i].get('id') == payload.get('tool_use_id')
                    ),
                    -1,
                )
                if index >= 0:
                    tool_calls[index]['result'] = payload.get('result', '')
                    tool_calls[index]['success'] = not payload.get('is_error')
            elif evt == 'done':
                saw_done = True
                if isinstance(payload, tuple) and len(payload) >= 3:
                    text = str(payload[0] or '')
                    usage_obj = payload[2] if isinstance(payload[2], dict) else {}
                    usage = dict(usage_obj)
                else:
                    text = str(payload or '')

        if not saw_done:
            raise RuntimeError('wake_read_observation_missing_done')
        respawn_reason = str(usage.get('respawn_reason') or '').strip()
        if respawn_reason:
            raise RuntimeError('wake_read_observation_respawned:' + respawn_reason)
        jsonl_finality = usage.get('jsonl_usage')
        if not isinstance(jsonl_finality, Mapping):
            raise RuntimeError('wake_read_observation_jsonl_finality_missing')
        if jsonl_finality.get('stream_totals_match') is not True:
            raise RuntimeError('wake_read_observation_jsonl_not_final')

        skip = commit_shared_transcript_watermark(
            watermark,
            resident,
            db_path=db_path,
            jsonl_finality=jsonl_finality,
        )
        observations = _parse_observation_json(text)
        if not observations:
            observations = _facts_from_tool_events(tool_calls)
        bundle = {
            'observations': tuple(observations),
            'status': 'ok',
        }
        if delivery_fence is not None:
            delivery_fence.finish(True, cache_info=usage, reason='delivery_succeeded')
        return {
            'status': 'ok',
            'reason': '',
            'started': True,
            'bundle': bundle,
            'lease': lease,
            'watermark_skip': skip,
        }
    except Exception as exc:
        _LOG.info('Wake read observation failed: %s', exc)
        if not acquired and '上一轮回复仍在生成中' in str(exc):
            result['reason'] = 'generation_lock_busy'
            return result
        if started and delivery_fence is not None:
            delivery_fence.finish(
                False,
                cache_info={'provider': 'claude_code', 'source': 'wake'},
                reason='delivery_failed',
            )
        return {
            'status': 'failed' if started else 'unavailable',
            'reason': str(exc)[:300],
            'started': started,
            'bundle': empty_observation_bundle(
                status='failed' if started else 'unavailable'
            ),
            'lease': None,
            'watermark_skip': None,
        }
    finally:
        if acquired and delivery_fence is None:
            release = getattr(gateway_module, '_gen_release', None)
            if callable(release):
                release(None)
