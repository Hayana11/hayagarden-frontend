#!/usr/bin/env python3
"""Step 8-B0 Spike: validate CAPACITY_BOUNDARY SYSTEM_SUFFIX_V1 as non-user Boundary.

Boundary is NOT written into forged JSONL. It is appended to system prompt only:

  effective_system = normal_spike_persona + CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1

A1: spawn_resumable(candidate) + one isolated user
A2: same live process, identical system+suffix, second isolated user

Does not modify Step 8-A, Manual Forge, DB schema, frontend, or production flags.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.capacity_swap import (  # noqa: E402
    CapacitySwapStatus,
    prepare_capacity_swap_candidate,
)
from chat.claude_transcript_reader import read_transcript  # noqa: E402
from chat.context_window_forge import atomic_write_jsonl_fsync  # noqa: E402
from cc_resident import (  # noqa: E402
    ResidentSession,
    TOOL_PROFILE_TEXT_ONLY,
)
from tools.cc_jsonl_usage import session_jsonl_path  # noqa: E402
from tools.claude_forge_core import sha256_file  # noqa: E402

# Frozen Boundary representation under test.
CAPACITY_BOUNDARY_REPRESENTATION = 'SYSTEM_SUFFIX_V1'
CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1 = (
    '\n\n[容量边界]\n'
    '当前 Claude session 未包含本窗口更早的部分对话。'
    '不要假装精确记得缺失原话；'
    '用户引用缺失内容而无法确认时，应自然请求补充。'
)

# Spike-only persona stand-in (does not edit production persona files).
NORMAL_SPIKE_PERSONA = (
    '你是费奥多尔。用简体中文自然、简短地回复。'
    '不要调用工具。不要编造未给出的对话细节。'
)

FIXTURE = ROOT / 'tests' / 'fixtures' / 'claude_transcript' / 'plain_two_rounds.jsonl'
ARTIFACT_DIR = ROOT / 'artifacts' / 'spike-capacity-boundary-system-suffix'

USER_A1 = '边界刺探A1：请只回复确认词 BOUNDARY_A1_OK'
USER_A2 = '边界刺探A2：请只回复确认词 BOUNDARY_A2_OK'


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _build_effective_system() -> str:
    return NORMAL_SPIKE_PERSONA + CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1


def _auth_env(claude_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env['CLAUDE_CONFIG_DIR'] = str(claude_home)
    env['DISABLE_AUTOUPDATER'] = '1'
    # Match production CC path: prefer OAuth, drop API key override.
    if env.get('CLAUDE_CODE_OAUTH_TOKEN', '').strip():
        env.pop('ANTHROPIC_API_KEY', None)
    return env


def _jsonl_stats(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {'exists': False, 'bytes': 0, 'lines': 0, 'sha256': None}
    raw = path.read_bytes()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    return {
        'exists': True,
        'bytes': len(raw),
        'lines': len(lines),
        'sha256': sha256_file(path),
    }


def _prepare_candidate(cwd: str) -> dict[str, Any]:
    graph = read_transcript(FIXTURE)
    mapping: dict[str, Any] = {}
    event_to_mid: dict[str, int] = {}
    mid_to_event: dict[int, str] = {}
    formal: list[dict[str, Any]] = []
    mid = 1
    for rnd in graph.candidate_rounds:
        uid = rnd.candidate_user_event_uuid
        evt = graph.by_uuid[uid]
        content = evt.raw.get('message', {}).get('content')
        if not isinstance(content, str):
            continue
        mapping[uid] = content
        event_to_mid[uid] = mid
        mid_to_event[mid] = uid
        formal.append({'id': mid, 'author': 'hayana', 'content': content, 'image_url': ''})
        mid += 1
        formal.append({
            'id': mid,
            'author': 'assistant',
            'content': '（spike fixture assistant）',
            'image_url': '',
        })
        mid += 1

    result = prepare_capacity_swap_candidate(
        graph=graph,
        trigger_reason='soft_context',
        source_context_id=1,
        source_context_epoch=1,
        source_resident_generation=1,
        source_claude_session_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        source_transcript_path=str(FIXTURE),
        source_scan_offset=max(1, FIXTURE.stat().st_size),
        source_sha256=sha256_file(FIXTURE),
        formal_messages=formal,
        user_canonical_by_event_uuid=mapping,
        mapping_event_uuid_by_message_id=mid_to_event,
        mapping_message_id_by_event_uuid=event_to_mid,
        retained_transcript_token_budget=50_000,
        anchor_token_budget=50_000,
        cwd=cwd,
    )
    if result.status != CapacitySwapStatus.READY or result.candidate is None:
        raise RuntimeError(f'candidate_not_ready:{result.status}:{result.warnings}')
    cand = result.candidate
    # Hard contract: forged JSONL must not contain fake boundary / fake user markers.
    blob = cand.serialized_jsonl
    for forbidden in (
        '[context-window-boundary]',
        '[容量边界]',
        'BOUNDARY_A1_OK',
        'BOUNDARY_A2_OK',
        USER_A1,
        USER_A2,
    ):
        if forbidden in blob:
            raise RuntimeError(f'forbidden_in_candidate:{forbidden}')
    return {
        'candidate_session_id': cand.candidate_session_id,
        'output_sha256': cand.output_sha256,
        'event_count': cand.event_count,
        'serialized_jsonl': cand.serialized_jsonl,
        'boundary_required': cand.boundary_required,
        'selected_round_count': cand.selected_round_count,
        'anchor_status': cand.anchor_status.value,
    }


def _consume_turn(resident: ResidentSession, user_text: str) -> dict[str, Any]:
    send_count = 0
    flushed = {'ok': False}

    def _on_flush():
        flushed['ok'] = True

    texts: list[str] = []
    thinks: list[str] = []
    first_delta: Optional[str] = None
    result_ok = False
    result_subtype = None
    err = None
    process_started = resident._alive()  # noqa: SLF001 — spike evidence only
    try:
        for kind, payload in resident.send_turn(user_text, on_stdin_flushed=_on_flush):
            if kind == 'text':
                chunk = str(payload or '')
                texts.append(chunk)
                if first_delta is None and chunk:
                    first_delta = chunk[:80]
            elif kind == 'think':
                thinks.append(str(payload or ''))
            elif kind == 'done':
                # done = (text, thinking, usage_dict, one_shot_claims)
                if isinstance(payload, tuple) and len(payload) >= 3:
                    usage = payload[2] if isinstance(payload[2], dict) else {}
                    result_ok = True
                    result_subtype = usage.get('result_subtype') or usage.get('subtype') or 'done'
                else:
                    result_ok = True
                    result_subtype = 'done'
        send_count = 1 if flushed['ok'] else 0
    except Exception as exc:
        err = f'{type(exc).__name__}:{exc}'
        send_count = 1 if flushed['ok'] else 0
    return {
        'process_started': bool(process_started),
        'stdin_flushed': bool(flushed['ok']),
        'user_send_count': send_count,
        'first_delta': first_delta,
        'text': ''.join(texts),
        'thinking_chars': sum(len(t) for t in thinks),
        'result_ok': result_ok,
        'result_subtype': result_subtype,
        'error': err,
        'alive_after': bool(resident._alive()),  # noqa: SLF001
    }


def _is_auth_failure(err: str) -> bool:
    text = str(err or '').lower()
    markers = (
        '401',
        'invalid bearer token',
        'invalid api key',
        'failed to authenticate',
        'authentication',
        'unauthorized',
    )
    return any(m in text for m in markers)


def _boundary_observation(text_a1: str, text_a2: str) -> dict[str, Any]:
    joined = (text_a1 or '') + '\n' + (text_a2 or '')
    announce_markers = (
        '容量边界',
        '上下文被裁剪',
        '更早的部分对话',
        '未包含本窗口',
        '缺失原话',
        'context was trimmed',
        'earlier conversation',
    )
    announced = [m for m in announce_markers if m in joined]
    # Crude persona-drift probes: refusing identity / claiming to be a generic assistant.
    drift_markers = (
        '我是Claude',
        '我是 Claude',
        'I am Claude',
        '我是人工智能助手',
        '作为一个AI助手',
        '作为人工智能',
    )
    drift_hits = [m for m in drift_markers if m in joined]
    return {
        'spontaneous_boundary_announce': bool(announced),
        'announce_markers_hit': announced,
        'obvious_persona_drift': bool(drift_hits),
        'drift_markers_hit': drift_hits,
        'a1_text_preview': (text_a1 or '')[:240],
        'a2_text_preview': (text_a2 or '')[:240],
    }


def main() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        'spike': 'Step-8-B0-CAPACITY-BOUNDARY-SYSTEM_SUFFIX_V1',
        'started_at': _utc_now(),
        'tested_base': os.popen('git rev-parse HEAD').read().strip(),
        'representation': CAPACITY_BOUNDARY_REPRESENTATION,
        'boundary_suffix': CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1.strip(),
        'verdict': 'ENVIRONMENT_BLOCKED',
    }

    oauth = bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '').strip())
    api_key = bool(os.environ.get('ANTHROPIC_API_KEY', '').strip())
    report['auth'] = {'oauth_present': oauth, 'api_key_present': api_key}
    if not oauth and not api_key:
        report['error'] = 'no_claude_credentials'
        _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    work_root = Path(tempfile.mkdtemp(prefix='capacity-boundary-spike-', dir='/tmp'))
    isolated_cwd = work_root / 'isolated-project'
    claude_home = work_root / 'claude-home'
    isolated_cwd.mkdir(parents=True)
    claude_home.mkdir(parents=True)
    report['work_root'] = str(work_root)

    try:
        cand = _prepare_candidate(str(isolated_cwd))
        report['candidate'] = {
            k: cand[k] for k in (
                'candidate_session_id', 'output_sha256', 'event_count',
                'boundary_required', 'selected_round_count', 'anchor_status',
            )
        }
        events = [
            json.loads(line)
            for line in cand['serialized_jsonl'].splitlines()
            if line.strip()
        ]
        # Ensure no non-user first event / no system boundary event.
        if not events or events[0].get('type') != 'user':
            raise RuntimeError('candidate_first_not_user')
        if any(e.get('type') == 'system' for e in events):
            raise RuntimeError('candidate_contains_system_event')

        sid = cand['candidate_session_id']
        jsonl_path = session_jsonl_path(
            str(isolated_cwd), sid, claude_home=str(claude_home),
        )
        assert jsonl_path is not None
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        written_sha = atomic_write_jsonl_fsync(jsonl_path, events)
        if written_sha != cand['output_sha256']:
            # sort_keys / separators may differ between serialize_events and forge writer.
            # Recompute expected from written events for publish proof.
            file_sha = sha256_file(jsonl_path)
            report['publish'] = {
                'path': str(jsonl_path),
                'written_sha': written_sha,
                'file_sha': file_sha,
                'candidate_sha': cand['output_sha256'],
                'sha_note': 'writer_sha_vs_candidate_sha',
            }
            if file_sha != written_sha:
                raise RuntimeError('publish_sha_mismatch')
        else:
            report['publish'] = {
                'path': str(jsonl_path),
                'file_sha': written_sha,
                'event_count': len(events),
            }

        effective_system = _build_effective_system()
        env = _auth_env(claude_home)
        mcp_stub = work_root / 'mcp-stub.json'
        mcp_stub.write_text('{"mcpServers":{}}\n', encoding='utf-8')

        resident = ResidentSession(str(isolated_cwd), '', str(mcp_stub))
        before_a1 = _jsonl_stats(jsonl_path)
        resident.spawn_resumable(
            effective_system,
            env,
            resume_session_id=sid,
            tool_profile=TOOL_PROFILE_TEXT_ONLY,
            reason='capacity_swap_spike',
        )
        resident.wait_staged_health(
            health_ms=2500,
            jsonl_path=jsonl_path,
            expected_sha256=sha256_file(jsonl_path),
        )

        a1 = _consume_turn(resident, USER_A1)
        after_a1 = _jsonl_stats(jsonl_path)
        a1['jsonl_before'] = before_a1
        a1['jsonl_after'] = after_a1
        a1['jsonl_grew'] = after_a1['bytes'] > before_a1['bytes']
        report['A1'] = a1

        # A2: same process, identical system+suffix; must not system_changed / respawn.
        peek = resident.peek_respawn_reason(
            effective_system, tool_profile=TOOL_PROFILE_TEXT_ONLY,
        )
        gen_before_a2 = int(getattr(resident, 'generation', 0) or resident._generation)  # noqa: SLF001
        before_a2 = _jsonl_stats(jsonl_path)
        a2 = _consume_turn(resident, USER_A2)
        after_a2 = _jsonl_stats(jsonl_path)
        gen_after_a2 = int(getattr(resident, 'generation', 0) or resident._generation)  # noqa: SLF001
        a2.update({
            'same_session': True,
            'peek_respawn_reason': peek,
            'system_changed': peek == 'system_changed',
            'extra_respawn': gen_after_a2 != gen_before_a2,
            'generation_before': gen_before_a2,
            'generation_after': gen_after_a2,
            'jsonl_before': before_a2,
            'jsonl_after': after_a2,
            'jsonl_grew': after_a2['bytes'] > before_a2['bytes'],
        })
        report['A2'] = a2
        report['boundary_observation'] = _boundary_observation(
            a1.get('text') or '', a2.get('text') or '',
        )

        a1_pass = bool(
            a1.get('process_started')
            and a1.get('first_delta')
            and a1.get('result_ok')
            and a1.get('jsonl_grew')
            and a1.get('user_send_count') == 1
            and not a1.get('error')
        )
        a2_pass = bool(
            a2.get('same_session')
            and not a2.get('system_changed')
            and not a2.get('extra_respawn')
            and a2.get('result_ok')
            and a2.get('jsonl_grew')
            and a2.get('user_send_count') == 1
            and not a2.get('error')
        )
        report['A1_pass'] = a1_pass
        report['A2_pass'] = a2_pass

        auth_fail = _is_auth_failure(a1.get('error') or '') or _is_auth_failure(a2.get('error') or '')
        if auth_fail:
            report['verdict'] = 'ENVIRONMENT_BLOCKED'
            report['error'] = 'claude_auth_rejected:' + str(a1.get('error') or a2.get('error'))
        elif a1_pass and a2_pass:
            report['verdict'] = 'PASS'
            report['CAPACITY_BOUNDARY_REPRESENTATION'] = CAPACITY_BOUNDARY_REPRESENTATION
        else:
            report['verdict'] = 'FAIL'

        try:
            resident._kill(quiet=True)  # noqa: SLF001
        except Exception:
            pass
    except Exception as exc:
        report['verdict'] = 'FAIL'
        report['error'] = f'{type(exc).__name__}:{exc}'
        report['traceback'] = traceback.format_exc()[-4000:]

    report['finished_at'] = _utc_now()
    _write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('verdict') == 'PASS' else 1


def _write_report(report: dict[str, Any]) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out = ARTIFACT_DIR / 'results.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
