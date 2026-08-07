"""Pinned Claude Code runtime contract tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
import sys
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import cc_runtime


class ClaudeRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        cc_runtime.clear_version_cache()
        self._old = {
            cc_runtime.ARGV_OVERRIDE_ENV: os.environ.get(cc_runtime.ARGV_OVERRIDE_ENV),
            cc_runtime.SKIP_VERSION_PROBE_ENV: os.environ.get(cc_runtime.SKIP_VERSION_PROBE_ENV),
        }

    def tearDown(self):
        for key, val in self._old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        cc_runtime.clear_version_cache()

    def test_expected_version_frozen(self):
        self.assertEqual(cc_runtime.EXPECTED_CLAUDE_CODE_VERSION, '2.1.220')
        self.assertEqual(
            cc_runtime.CLAUDE_CODE_NPM_SPEC,
            '@anthropic-ai/claude-code@2.1.220',
        )

    def test_argv_never_bare_path_claude(self):
        os.environ.pop(cc_runtime.ARGV_OVERRIDE_ENV, None)
        with mock.patch.object(cc_runtime, 'local_claude_bin', return_value=None):
            prefix = cc_runtime.claude_argv_prefix()
        self.assertNotEqual(prefix, ['claude'])
        self.assertEqual(prefix[0], 'npx')
        self.assertIn('@anthropic-ai/claude-code@2.1.220', prefix)

    def test_local_bin_preferred(self):
        os.environ.pop(cc_runtime.ARGV_OVERRIDE_ENV, None)
        fake = Path('/tmp/fake-claude-bin-for-test')
        with mock.patch.object(cc_runtime, 'local_claude_bin', return_value=fake):
            prefix = cc_runtime.claude_argv_prefix()
        self.assertEqual(prefix, [str(fake)])

    def test_claude_cmd_prefixes_flags(self):
        os.environ[cc_runtime.ARGV_OVERRIDE_ENV] = json.dumps(['/opt/pin/claude'])
        args = cc_runtime.claude_cmd('-p', '--resume', 'sid')
        self.assertEqual(args, ['/opt/pin/claude', '-p', '--resume', 'sid'])

    def test_version_mismatch_fail_closed(self):
        os.environ.pop(cc_runtime.SKIP_VERSION_PROBE_ENV, None)
        os.environ[cc_runtime.ARGV_OVERRIDE_ENV] = json.dumps(['/opt/pin/claude'])
        with mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.186'):
            with self.assertRaises(cc_runtime.ClaudeRuntimeError) as ctx:
                cc_runtime.require_pinned_claude_version()
        self.assertIn('2.1.220', str(ctx.exception))
        self.assertIn('2.1.186', str(ctx.exception))

    def test_version_match_ok(self):
        os.environ.pop(cc_runtime.SKIP_VERSION_PROBE_ENV, None)
        os.environ[cc_runtime.ARGV_OVERRIDE_ENV] = json.dumps(['/opt/pin/claude'])
        with mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.220'):
            actual = cc_runtime.require_pinned_claude_version()
        self.assertEqual(actual, '2.1.220')

    def test_parse_version_text(self):
        self.assertEqual(
            cc_runtime.parse_claude_version_text('2.1.220 (Claude Code)'),
            '2.1.220',
        )

    def test_resident_spawn_uses_contract(self):
        """Fresh/_spawn/resumable/fresh_named must call require + claude_cmd."""
        import cc_resident

        os.environ[cc_runtime.ARGV_OVERRIDE_ENV] = json.dumps(['/opt/pin/claude'])
        os.environ[cc_runtime.SKIP_VERSION_PROBE_ENV] = '1'
        session = cc_resident.ResidentSession('/tmp', '', '/tmp/mcp.json')
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with mock.patch('cc_resident.subprocess.Popen', return_value=fake_proc) as popen, \
             mock.patch('chat.cc_model.cc_model_snapshot', return_value=('m', 'id', [])), \
             mock.patch.object(session, '_kill'), \
             mock.patch.object(session, '_alive', return_value=False), \
             mock.patch.object(session, '_reset_session_meta'):
            session._spawn('sys', {'HOME': '/tmp'}, reason='test')
            args = popen.call_args[0][0]
            self.assertEqual(args[0], '/opt/pin/claude')
            self.assertNotIn('claude', args[0:1] if args[0] == 'claude' else [])
            self.assertEqual(args[0:2], ['/opt/pin/claude', '-p'])

            popen.reset_mock()
            session.spawn_resumable('sys', {'HOME': '/tmp'}, resume_session_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
            args = popen.call_args[0][0]
            self.assertEqual(args[0], '/opt/pin/claude')
            self.assertIn('--resume', args)

            popen.reset_mock()
            session.spawn_fresh_named(
                'sys', {'HOME': '/tmp'},
                session_id='11111111-2222-3333-4444-555555555555',
            )
            args = popen.call_args[0][0]
            self.assertEqual(args[0], '/opt/pin/claude')
            self.assertIn('--session-id', args)
            self.assertNotIn('--resume', args)

    def test_resident_mismatch_raises(self):
        import cc_resident

        os.environ.pop(cc_runtime.SKIP_VERSION_PROBE_ENV, None)
        os.environ[cc_runtime.ARGV_OVERRIDE_ENV] = json.dumps(['/opt/pin/claude'])
        session = cc_resident.ResidentSession('/tmp', '', '/tmp/mcp.json')
        with mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.186'), \
             mock.patch.object(session, '_kill'), \
             mock.patch('chat.cc_model.cc_model_snapshot', return_value=('m', 'id', [])):
            with self.assertRaises(cc_resident.ResidentError) as ctx:
                session._spawn('sys', {'HOME': '/tmp'})
        self.assertIn('claude_runtime', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
