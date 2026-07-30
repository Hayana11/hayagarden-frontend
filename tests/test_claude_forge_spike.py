"""Unit tests for Claude forge spike (structural; no live API)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.claude_forge_core import (
    ForgeOptions,
    collect_event_uuids,
    forge_transcript,
    load_jsonl,
    new_uuid,
)
from tools.claude_forge_validator import (
    FORGE_LEGACY_INJECTION,
    FORGE_TOOL_PAIR,
    validate_forged_transcript,
)


FIXTURE_ROOT = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'


class ClaudeForgeSpikeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE_ROOT.is_dir():
            subprocess.run(
                [sys.executable, str(ROOT / 'scripts' / 'build_claude_forge_fixtures.py')],
                check=True,
            )

    def test_case1_forge_structure(self) -> None:
        src = load_jsonl(FIXTURE_ROOT / 'case1_plain_two_rounds.jsonl')
        new_sid = new_uuid()
        forged = forge_transcript(
            src,
            ForgeOptions(new_session_id=new_sid, cwd='/tmp/claude-forge-spike-project'),
        )
        old_uuids = collect_event_uuids(src)
        result = validate_forged_transcript(
            forged.events,
            session_id=new_sid,
            old_uuids=old_uuids,
        )
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(forged.events[0]['parentUuid'], None)
        self.assertEqual(forged.events[0]['message']['content'], '测试消息 A')

    def test_case5_sidechain_excluded(self) -> None:
        src = load_jsonl(FIXTURE_ROOT / 'case5_sidechain.jsonl')
        forged = forge_transcript(
            src,
            ForgeOptions(new_session_id=new_uuid(), cwd='/tmp/x', exclude_sidechain=True),
        )
        self.assertEqual(len(forged.events), 1)
        self.assertEqual(forged.events[0]['message']['content'], '主链消息')

    def test_real_metadata_filtered_from_kept_events(self) -> None:
        sid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        u_uuid = '11111111-1111-1111-1111-111111111111'
        a_uuid = '22222222-2222-2222-2222-222222222222'
        src = [
            {
                'type': 'queue-operation',
                'uuid': '00000000-0000-0000-0000-000000000001',
                'sessionId': sid,
                'operation': 'enqueue',
            },
            {
                'type': 'user',
                'uuid': u_uuid,
                'parentUuid': None,
                'timestamp': '2026-07-29T10:00:00.000Z',
                'sessionId': sid,
                'cwd': '/tmp/clean-shadow',
                'version': '2.1.220',
                'message': {'role': 'user', 'content': '测试消息 A'},
            },
            {
                'type': 'assistant',
                'uuid': a_uuid,
                'parentUuid': u_uuid,
                'timestamp': '2026-07-29T10:00:01.000Z',
                'sessionId': sid,
                'cwd': '/tmp/clean-shadow',
                'version': '2.1.220',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': '测试回复 A'}],
                },
            },
            {
                'type': 'last-prompt',
                'uuid': '33333333-3333-3333-3333-333333333333',
                'sessionId': sid,
                'prompt': '测试消息 A',
            },
        ]
        src_snapshot = json.loads(json.dumps(src))
        new_sid = new_uuid()
        forged = forge_transcript(
            src,
            ForgeOptions(new_session_id=new_sid, cwd='/tmp/clean-shadow'),
        )
        self.assertEqual(src, src_snapshot)
        types = [e.get('type') for e in forged.events]
        self.assertEqual(types, ['user', 'assistant'])
        self.assertEqual(forged.events[0]['type'], 'user')
        self.assertEqual(forged.events[0]['parentUuid'], None)
        self.assertEqual(forged.events[1]['parentUuid'], forged.events[0]['uuid'])
        self.assertTrue(all(e.get('sessionId') == new_sid for e in forged.events))
        old_uuids = collect_event_uuids(src)
        result = validate_forged_transcript(
            forged.events,
            session_id=new_sid,
            old_uuids=old_uuids,
        )
        self.assertTrue(result.ok, result.errors)

    def test_case6_summary_removed(self) -> None:
        src = load_jsonl(FIXTURE_ROOT / 'case6_summary_uuid_refs.jsonl')
        forged = forge_transcript(
            src,
            ForgeOptions(new_session_id=new_uuid(), cwd='/tmp/x'),
        )
        types = [e.get('type') for e in forged.events]
        self.assertNotIn('summary', types)

    def test_case7_injection_stripped(self) -> None:
        src = load_jsonl(FIXTURE_ROOT / 'case7_legacy_injection.jsonl')
        canon = json.loads((FIXTURE_ROOT / 'app_db_canonical_messages.json').read_text(encoding='utf-8'))
        inj_uuid = next(str(e.get('uuid')) for e in src if e.get('type') == 'user')
        forged = forge_transcript(
            src,
            ForgeOptions(
                new_session_id=new_uuid(),
                cwd='/tmp/x',
                user_canonical_by_event_uuid={
                    inj_uuid: canon['by_claude_event_uuid'][inj_uuid],
                },
            ),
        )
        user_text = forged.events[0]['message']['content']
        self.assertEqual(user_text, '今天天气怎么样')
        result = validate_forged_transcript(forged.events, session_id=forged.events[0]['sessionId'])
        self.assertTrue(result.ok, result.errors)

    def test_negative_orphan_tool_rejected(self) -> None:
        sid = new_uuid()
        u, a = new_uuid(), new_uuid()
        events = [
            {'type': 'user', 'uuid': u, 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': 'x'}},
            {'type': 'assistant', 'uuid': a, 'parentUuid': u, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'toolu_x', 'name': 'Read', 'input': {}},
             ]}},
        ]
        result = validate_forged_transcript(events, session_id=sid)
        self.assertFalse(result.ok)
        self.assertTrue(any(FORGE_TOOL_PAIR in e for e in result.errors))

    def test_legacy_injection_detected(self) -> None:
        sid = new_uuid()
        events = [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': '【昨日延续对话】\n真实话'}},
        ]
        result = validate_forged_transcript(events, session_id=sid)
        self.assertFalse(result.ok)
        self.assertTrue(any(FORGE_LEGACY_INJECTION in e for e in result.errors))

    def test_spike_script_structural_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / 'results.json'
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'scripts' / 'spike_claude_forge_resume.py'),
                    '--structural-only',
                    '--work-root', tmp,
                    '--report', str(report_path),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                check=False,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            data = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertTrue(data['structural_only'])
            self.assertFalse(data['auth_available'])
            self.assertEqual(data['live_probe_status'], 'NOT_RUN_NO_CREDENTIALS')
            self.assertEqual(data['verdict'], 'NO-GO')
            self.assertIn('tested_tree_sha', data)
            self.assertFalse(data['ci_verified'])
            self.assertFalse(data['touched_production'])
            case1 = next(c for c in data['cases'] if c['case_id'] == '1')
            self.assertTrue(case1['structure_ok'])
            self.assertEqual(case1['state'], 'STRUCTURE_ONLY')


if __name__ == '__main__':
    unittest.main()
