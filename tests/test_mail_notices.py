from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from chat.mail_notice_store import (
    ensure_schema,
    peek_pending_mail_notice_ids,
    record_pending_mail_notice,
)
from chat.system_builder import (
    _cc_collect_one_shot,
    consume_cc_one_shot_claims,
    format_one_shot,
)
from cc_resident import ResidentError, ResidentSession


class FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(''.join(line + '\n' for line in lines))
        self.stderr = io.StringIO()
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0

    def kill(self):
        self._code = -9

    def wait(self, timeout=None):
        return self._code or 0


class PendingMailNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')

        def get_db():
            return sqlite3.connect(self.db_path)

        self.get_db = get_db
        ensure_schema(self.get_db)

    def tearDown(self):
        self.tmp.cleanup()

    def _collect(self, *, include_wake=False):
        fake_commands = types.SimpleNamespace(peek_feedback=lambda: ([], []))
        with mock.patch.dict(sys.modules, {'command_store': fake_commands}), mock.patch(
            'chat.system_builder.peek_dream_one_shot', return_value=('', None),
        ):
            return _cc_collect_one_shot(self.get_db, include_wake=include_wake)

    def test_message_id_is_idempotent(self):
        self.assertTrue(record_pending_mail_notice(self.get_db, 'gmail_1'))
        self.assertFalse(record_pending_mail_notice(self.get_db, 'gmail_1'))
        self.assertEqual(peek_pending_mail_notice_ids(self.get_db), ['gmail_1'])

    def test_multiple_pending_ids_are_one_peek_only_notice(self):
        record_pending_mail_notice(self.get_db, 'gmail_1')
        record_pending_mail_notice(self.get_db, 'gmail_2')

        one_shot = self._collect()
        prompt = format_one_shot(one_shot)

        self.assertEqual(one_shot['pending_mail_ids'], ['gmail_1', 'gmail_2'])
        self.assertIn('有 2 封新的邮件', prompt)
        self.assertEqual(prompt.count('message id:'), 2)
        self.assertIn('gmail_1', prompt)
        self.assertIn('gmail_2', prompt)
        self.assertIn('需要时可以使用邮箱工具读取。', prompt)
        self.assertEqual(peek_pending_mail_notice_ids(self.get_db), ['gmail_1', 'gmail_2'])

    def test_failed_turn_leaves_notice_for_the_next_one_shot(self):
        record_pending_mail_notice(self.get_db, 'gmail_1')

        failed_turn_claim = self._collect()['pending_mail_ids']
        next_turn = self._collect()

        self.assertEqual(failed_turn_claim, ['gmail_1'])
        self.assertEqual(next_turn['pending_mail_ids'], ['gmail_1'])

    def test_model_terminal_error_keeps_mail_notice_pending(self):
        record_pending_mail_notice(self.get_db, 'gmail_1')
        error_result = json.dumps({
            'type': 'result',
            'is_error': True,
            'result': 'temporary provider error',
            'usage': {},
        })
        resident = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        resident._proc = FakeProc([error_result])
        resident._cold = False

        with self.assertRaises(ResidentError):
            list(resident.send_turn('hi', commit_meta={
                'pending_mail_ids': ['gmail_1'],
                'state_snapshot': {},
            }))

        self.assertEqual(peek_pending_mail_notice_ids(self.get_db), ['gmail_1'])

    def test_terminal_without_assistant_commit_keeps_notice_pending(self):
        record_pending_mail_notice(self.get_db, 'gmail_1')
        success_result = json.dumps({
            'type': 'result',
            'is_error': False,
            'result': 'ok',
            'stop_reason': 'end_turn',
            'usage': {},
        })
        resident = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        resident._proc = FakeProc([success_result])
        resident._cold = False

        events = list(resident.send_turn('hi', commit_meta={
            'pending_mail_ids': ['gmail_1'],
            'state_snapshot': {},
        }))
        claims = events[-1][1][3]

        self.assertEqual(claims['pending_mail_ids'], ['gmail_1'])
        # No assistant commit/mapping means the gateway has not consumed this claim.
        self.assertEqual(peek_pending_mail_notice_ids(self.get_db), ['gmail_1'])

    def test_success_consumes_only_ids_carried_by_resident_claims(self):
        record_pending_mail_notice(self.get_db, 'gmail_1')
        record_pending_mail_notice(self.get_db, 'gmail_2')
        claims = ResidentSession._extract_one_shot_claims({
            'pending_mail_ids': ['gmail_1'],
        })

        self.assertEqual(claims['pending_mail_ids'], ['gmail_1'])
        consume_cc_one_shot_claims(self.get_db, claims)
        self.assertEqual(peek_pending_mail_notice_ids(self.get_db), ['gmail_2'])

    def test_missing_success_consume_keeps_notice_pending(self):
        record_pending_mail_notice(self.get_db, 'gmail_1')

        self.assertEqual(peek_pending_mail_notice_ids(self.get_db), ['gmail_1'])
        self.assertEqual(peek_pending_mail_notice_ids(self.get_db), ['gmail_1'])


if __name__ == '__main__':
    unittest.main()
