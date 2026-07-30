"""Synthetic Claude JSONL fixtures for forge spike (no private content)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.claude_forge_core import new_uuid

FIXTURE_ROOT = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'


def _write_jsonl(name: str, events: list[dict]) -> Path:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_ROOT / name
    path.write_text(
        '\n'.join(json.dumps(e, ensure_ascii=False) for e in events) + '\n',
        encoding='utf-8',
    )
    return path


def _write_json(name: str, payload: dict) -> Path:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_ROOT / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


def build_all() -> None:
    sid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    cwd = '/tmp/claude-forge-spike-project'
    u1, a1, u2, a2 = new_uuid(), new_uuid(), new_uuid(), new_uuid()
    _write_jsonl('case1_plain_two_rounds.jsonl', [
        {'type': 'user', 'uuid': u1, 'parentUuid': None, 'timestamp': '2026-07-29T10:00:00.000Z',
         'sessionId': sid, 'cwd': cwd, 'version': '2.1.220', 'message': {'role': 'user', 'content': '测试消息 A'}},
        {'type': 'assistant', 'uuid': a1, 'parentUuid': u1, 'timestamp': '2026-07-29T10:00:01.000Z',
         'sessionId': sid, 'cwd': cwd, 'version': '2.1.220', 'requestId': 'req-old-1',
         'message': {'role': 'assistant', 'model': 'claude-sonnet-4-6', 'content': [{'type': 'text', 'text': '测试回复 A'}],
                     'usage': {'input_tokens': 10, 'output_tokens': 5}}},
        {'type': 'user', 'uuid': u2, 'parentUuid': a1, 'timestamp': '2026-07-29T10:00:02.000Z',
         'sessionId': sid, 'cwd': cwd, 'version': '2.1.220', 'message': {'role': 'user', 'content': '测试消息 B'}},
        {'type': 'assistant', 'uuid': a2, 'parentUuid': u2, 'timestamp': '2026-07-29T10:00:03.000Z',
         'sessionId': sid, 'cwd': cwd, 'version': '2.1.220', 'requestId': 'req-old-2',
         'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '测试回复 B'}],
                     'usage': {'input_tokens': 12, 'output_tokens': 6}}},
    ])

    think_uuid = new_uuid()
    _write_jsonl('case2a_signed_thinking.jsonl', [
        {'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'timestamp': '2026-07-29T11:00:00.000Z',
         'sessionId': sid, 'cwd': cwd, 'message': {'role': 'user', 'content': '想一个问题'}},
        {'type': 'assistant', 'uuid': think_uuid, 'parentUuid': None, 'timestamp': '2026-07-29T11:00:01.000Z',
         'sessionId': sid, 'cwd': cwd,
         'message': {'role': 'assistant', 'content': [
             {'type': 'thinking', 'thinking': '合成思考：先分析测试问题。', 'signature': 'sig_synthetic_base64_payload_AAAA'},
             {'type': 'text', 'text': '合成回复带思考'},
         ]}},
    ])

    tu = 'toolu_spike_success_001'
    au, tu_evt, tr_evt, aa = new_uuid(), new_uuid(), new_uuid(), new_uuid()
    _write_jsonl('case3_tool_success.jsonl', [
        {'type': 'user', 'uuid': au, 'parentUuid': None, 'timestamp': '2026-07-29T12:00:00.000Z',
         'sessionId': sid, 'cwd': cwd, 'message': {'role': 'user', 'content': '读取测试文件'}},
        {'type': 'assistant', 'uuid': tu_evt, 'parentUuid': au, 'timestamp': '2026-07-29T12:00:01.000Z',
         'sessionId': sid, 'cwd': cwd,
         'message': {'role': 'assistant', 'content': [
             {'type': 'tool_use', 'id': tu, 'name': 'Read', 'input': {'file_path': '/tmp/spike-test.txt'}},
         ]}},
        {'type': 'user', 'uuid': tr_evt, 'parentUuid': tu_evt, 'timestamp': '2026-07-29T12:00:02.000Z',
         'sessionId': sid, 'cwd': cwd, 'sourceToolUseID': tu,
         'message': {'role': 'user', 'content': [
             {'type': 'tool_result', 'tool_use_id': tu, 'content': 'hello spike', 'is_error': False},
         ]}},
        {'type': 'assistant', 'uuid': aa, 'parentUuid': tr_evt, 'timestamp': '2026-07-29T12:00:03.000Z',
         'sessionId': sid, 'cwd': cwd,
         'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '文件内容是 hello spike'}]}},
    ])

    side_u, side_a = new_uuid(), new_uuid()
    main_u = new_uuid()
    _write_jsonl('case5_sidechain.jsonl', [
        {'type': 'user', 'uuid': main_u, 'parentUuid': None, 'timestamp': '2026-07-29T13:00:00.000Z',
         'sessionId': sid, 'cwd': cwd, 'message': {'role': 'user', 'content': '主链消息'}},
        {'type': 'assistant', 'uuid': side_a, 'parentUuid': main_u, 'timestamp': '2026-07-29T13:00:01.000Z',
         'sessionId': sid, 'cwd': cwd, 'isSidechain': True,
         'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '子代理回复'}]}},
        {'type': 'user', 'uuid': side_u, 'parentUuid': side_a, 'timestamp': '2026-07-29T13:00:02.000Z',
         'sessionId': sid, 'cwd': cwd, 'isSidechain': True,
         'message': {'role': 'user', 'content': '子代理输入'}},
    ])

    leaf = new_uuid()
    sum_uuid = new_uuid()
    _write_jsonl('case6_summary_uuid_refs.jsonl', [
        {'type': 'user', 'uuid': leaf, 'parentUuid': None, 'timestamp': '2026-07-29T14:00:00.000Z',
         'sessionId': sid, 'cwd': cwd, 'message': {'role': 'user', 'content': '摘要前消息'}},
        {'type': 'summary', 'uuid': sum_uuid, 'parentUuid': leaf, 'timestamp': '2026-07-29T14:00:01.000Z',
         'sessionId': sid, 'cwd': cwd, 'leafUuid': leaf, 'summary': '合成摘要行'},
    ])

    inj_uuid = new_uuid()
    injected = (
        '【昨日延续对话】\n[用户] 旧carryover\n\n'
        '以下是本聊天日内的正式对话记录：\n[用户] 旧history\n\n'
        '【渐变脑快照】state=v0\n\n'
        '真实用户原话：今天天气怎么样'
    )
    _write_jsonl('case7_legacy_injection.jsonl', [
        {'type': 'user', 'uuid': inj_uuid, 'parentUuid': None, 'timestamp': '2026-07-29T15:00:00.000Z',
         'sessionId': sid, 'cwd': cwd, 'message': {'role': 'user', 'content': injected}},
        {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': inj_uuid, 'timestamp': '2026-07-29T15:00:01.000Z',
         'sessionId': sid, 'cwd': cwd,
         'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '助手从 JSONL 恢复'}]}},
    ])

    _write_json('app_db_canonical_messages.json', {
        'by_claude_event_uuid': {
            inj_uuid: '今天天气怎么样',
        },
    })


if __name__ == '__main__':
    build_all()
    print('fixtures built at', FIXTURE_ROOT)
