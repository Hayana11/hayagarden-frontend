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
        result = validate_forged_transcript(forged.events, session_id=new_sid)
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
            self.assertFalse(data['auth_available'])
            self.assertEqual(data['verdict'], 'NO-GO')
            self.assertFalse(data['touched_production'])
            case1 = next(c for c in data['cases'] if c['case_id'] == '1')
            self.assertTrue(case1['structure_ok'])


if __name__ == '__main__':
    unittest.main()
