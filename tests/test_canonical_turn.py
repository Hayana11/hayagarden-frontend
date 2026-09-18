import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The clean baseline imports a broad chat package eagerly. Load the pure
# projection through a package shell so this focused test stays isolated.
if 'chat' not in sys.modules:
    chat_package = types.ModuleType('chat')
    chat_package.__path__ = [str(Path(__file__).resolve().parents[1] / 'chat')]
    sys.modules['chat'] = chat_package

from chat.canonical_turn import CanonicalTurnError, build_canonical_turn, projection_hash
from cc_resident import ProviderTerminalReceipt


def _row(row_type, *, sid='session-1', **fields):
    value = {'type': row_type, 'sessionId': sid}
    value.update(fields)
    return value


def _write(rows):
    handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False)
    try:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        path = Path(handle.name)
    finally:
        handle.close()
    return path


class CanonicalTurnTests(unittest.TestCase):
    def _build(
        self,
        rows,
        mode='auto',
        *,
        terminal_receipt=None,
        resident_generation=None,
    ):
        path = _write(rows)
        try:
            end = path.stat().st_size
            return build_canonical_turn(
                str(path),
                start_offset=0,
                end_offset=end,
                session_id='session-1',
                mode=mode,
                resident_generation=resident_generation,
                terminal_receipt=terminal_receipt,
            )
        finally:
            path.unlink(missing_ok=True)

    def test_transcript_wins_over_short_stream_prefix(self):
        turn = self._build([
            _row('user', message={'role': 'user', 'content': 'question'}),
            _row('assistant', uuid='a1', message={
                'role': 'assistant',
                'content': [{'type': 'text', 'text': '完整正文，而不是流前缀。'}],
            }),
            _row('result', stop_reason='end_turn'),
        ])
        self.assertEqual(turn.content, '完整正文，而不是流前缀。')
        self.assertEqual(turn.projection_hash, projection_hash(turn.content, turn.display_segments))
        self.assertEqual(json.loads(turn.display_segments)[0]['text'], turn.content)

    def test_multiple_text_blocks_and_tool_rounds_keep_order(self):
        turn = self._build([
            _row('user', message={'role': 'user', 'content': 'question'}),
            _row('assistant', uuid='a1', message={'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'text A'},
                {'type': 'tool_use', 'id': 'tool-1', 'name': 'read', 'input': {'path': 'a'}},
            ]}),
            _row('user', message={'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'result A'},
            ]}),
            _row('assistant', uuid='a2', message={'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'text B'},
                {'type': 'tool_use', 'id': 'tool-2', 'name': 'read', 'input': {'path': 'b'}},
            ]}),
            _row('user', message={'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tool-2', 'content': 'result B'},
            ]}),
            _row('assistant', uuid='a3', message={'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'text C'},
            ]}),
            _row('result', stop_reason='end_turn'),
        ])
        kinds = [(item['type'], item.get('text'), item.get('tool_index'))
                 for item in json.loads(turn.display_segments)]
        self.assertEqual(
            kinds,
            [('text', 'text A', None), ('tool', None, 0),
             ('text', 'text B', None), ('tool', None, 1),
             ('text', 'text C', None)],
        )
        self.assertEqual([call['result'] for call in turn.tool_calls], ['result A', 'result B'])
        self.assertEqual([row['index'] for row in turn.provider_rounds], [1, 2, 3])

    def test_native_and_authored_thinking_have_one_owner(self):
        rows = [
            _row('user', message={'role': 'user', 'content': 'question'}),
            _row('assistant', uuid='a1', message={'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'native thought'},
                {'type': 'text', 'text': '<思绪>authored duplicate</思绪>formal'},
            ]}),
            _row('result', stop_reason='end_turn'),
        ]
        native = self._build(rows, mode='native')
        authored = self._build(rows, mode='authored')
        auto = self._build(rows, mode='auto')
        self.assertEqual(native.thinking, 'native thought')
        self.assertEqual(native.content, 'formal')
        self.assertEqual(authored.thinking, 'authored duplicate')
        self.assertEqual(authored.content, 'formal')
        self.assertEqual(auto.thinking, 'native thought')
        self.assertEqual(auto.content, 'formal')

    def test_missing_final_provider_data_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as missing_result:
            self._build([
                _row('user', message={'role': 'user', 'content': 'question'}),
                _row('assistant', uuid='a1', message={
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': 'stream prefix'}],
                }),
            ])
        self.assertEqual(missing_result.exception.error_code, 'provider_result_missing')

        with self.assertRaises(CanonicalTurnError) as missing_assistant:
            self._build([
                _row('user', message={'role': 'user', 'content': 'question'}),
                _row('result', stop_reason='end_turn'),
            ])
        self.assertEqual(missing_assistant.exception.error_code, 'canonical_assistant_missing')

    def _receipt(
        self,
        *,
        generation=7,
        session_id='session-1',
        source='resident_live_stdout',
        stop_reason='end_turn',
        result_is_error=False,
    ):
        return ProviderTerminalReceipt(
            terminal_kind='provider_result',
            source=source,
            turn_identity='turn-1',
            resident_generation=generation,
            claude_session_id=session_id,
            result_is_error=result_is_error,
            result_stop_reason=stop_reason,
        )

    def _receipt_rows(self, *, stop_reason='end_turn', with_tool=False):
        content = []
        if with_tool:
            content.append({
                'type': 'tool_use',
                'id': 'tool-1',
                'name': 'read',
                'input': {},
            })
        content.append({'type': 'text', 'text': 'done'})
        rows = [
            _row('assistant', uuid='a1', message={
                'role': 'assistant',
                'stop_reason': stop_reason,
                'content': content,
            }),
        ]
        if with_tool:
            rows.append(_row('user', message={'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'ok'},
            ]}))
        return rows

    def test_live_receipt_replaces_only_missing_transcript_result(self):
        turn = self._build(
            self._receipt_rows(),
            terminal_receipt=self._receipt(),
            resident_generation=7,
        )
        self.assertEqual(turn.content, 'done')
        self.assertEqual(turn.terminal_state, 'confirmed')

    def test_live_receipt_closes_r2_tool_round(self):
        turn = self._build(
            self._receipt_rows(with_tool=True),
            terminal_receipt=self._receipt(),
            resident_generation=7,
        )
        self.assertEqual(turn.tool_calls[0]['result'], 'ok')

    def test_receipt_absent_keeps_missing_result_failure(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(self._receipt_rows())
        self.assertEqual(raised.exception.error_code, 'provider_result_missing')

    def test_receipt_generation_mismatch_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                self._receipt_rows(),
                terminal_receipt=self._receipt(generation=8),
                resident_generation=7,
            )
        self.assertEqual(
            raised.exception.error_code,
            'provider_terminal_receipt_generation_mismatch',
        )

    def test_receipt_session_mismatch_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                self._receipt_rows(),
                terminal_receipt=self._receipt(session_id='other'),
                resident_generation=7,
            )
        self.assertEqual(
            raised.exception.error_code,
            'provider_terminal_receipt_session_mismatch',
        )

    def test_receipt_source_mismatch_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                self._receipt_rows(),
                terminal_receipt=self._receipt(source='synthetic'),
                resident_generation=7,
            )
        self.assertEqual(raised.exception.error_code, 'provider_terminal_receipt_invalid')

    def test_receipt_stop_reason_mismatch_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                self._receipt_rows(),
                terminal_receipt=self._receipt(stop_reason='tool_deferred'),
            )
        self.assertEqual(
            raised.exception.error_code,
            'provider_terminal_receipt_invalid',
        )

    def test_receipt_pending_tool_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                [
                    _row('assistant', uuid='a1', message={
                        'role': 'assistant',
                        'stop_reason': 'end_turn',
                        'content': [{
                            'type': 'tool_use',
                            'id': 'tool-1',
                            'name': 'read',
                            'input': {},
                        }],
                    }),
                ],
                terminal_receipt=self._receipt(),
                resident_generation=7,
            )
        self.assertEqual(raised.exception.error_code, 'tool_result_missing')

    def test_receipt_unmatched_tool_result_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                [
                    _row('user', message={'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'unknown', 'content': 'x'},
                    ]}),
                    _row('assistant', uuid='a1', message={
                        'role': 'assistant',
                        'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': 'done'}],
                    }),
                ],
                terminal_receipt=self._receipt(),
                resident_generation=7,
            )
        self.assertEqual(raised.exception.error_code, 'tool_result_unmatched')

    def test_receipt_duplicate_tool_result_fails_closed(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                [
                    _row('assistant', uuid='a1', message={
                        'role': 'assistant',
                        'stop_reason': 'end_turn',
                        'content': [{
                            'type': 'tool_use',
                            'id': 'tool-1',
                            'name': 'read',
                            'input': {},
                        }],
                    }),
                    _row('user', message={'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'x'},
                    ]}),
                    _row('user', message={'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'x'},
                    ]}),
                ],
                terminal_receipt=self._receipt(),
                resident_generation=7,
            )
        self.assertEqual(raised.exception.error_code, 'tool_result_duplicate')

    def test_receipt_requires_assistant_end_turn(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                [
                    _row('assistant', uuid='a1', message={
                        'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'done'}],
                    }),
                ],
                terminal_receipt=self._receipt(),
                resident_generation=7,
            )
        self.assertEqual(raised.exception.error_code, 'canonical_stop_reason_invalid')

    def test_receipt_and_transcript_result_must_agree(self):
        turn = self._build(
            self._receipt_rows(),
            terminal_receipt=self._receipt(),
            resident_generation=7,
        )
        self.assertEqual(turn.stop_reason, 'end_turn')
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build(
                [
                    _row('assistant', uuid='a1', message={
                        'role': 'assistant',
                        'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': 'done'}],
                    }),
                    _row('result', stop_reason='tool_deferred'),
                ],
                terminal_receipt=self._receipt(),
                resident_generation=7,
            )
        self.assertEqual(raised.exception.error_code, 'provider_terminal_receipt_conflict')

    def test_end_turn_without_live_result_or_transcript_result_still_fails(self):
        with self.assertRaises(CanonicalTurnError) as raised:
            self._build([
                _row('assistant', uuid='a1', message={
                    'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': 'done'}],
                }),
            ])
        self.assertEqual(raised.exception.error_code, 'provider_result_missing')

    def test_transcript_result_remains_compatible_without_receipt(self):
        turn = self._build([
            _row('assistant', uuid='a1', message={
                'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': 'done'}],
            }),
            _row('result', stop_reason='end_turn'),
        ])
        self.assertEqual(turn.content, 'done')

    def test_projection_hash_covers_complete_normalized_projection(self):
        tool = {
            'id': 'tool-1',
            'name': 'read',
            'args': {'path': 'a', 'options': {'z': 1, 'a': True}},
            'result': 'result A',
            'success': True,
        }
        exact_left = projection_hash(
            'same text',
            '[{"type":"text","text":"same text"}]',
            thinking='same thinking',
            tool_calls=[tool],
            choices=['A', 'B'],
        )
        exact_right = projection_hash(
            'same text',
            '[{"text":"same text","type":"text"}]',
            thinking='same thinking',
            tool_calls=[dict(tool)],
            choices=['A', 'B'],
        )
        self.assertEqual(exact_left, exact_right)
        self.assertNotEqual(
            exact_left,
            projection_hash(
                'same text',
                '[{"type":"text","text":"same text"}]',
                thinking='same thinking',
                tool_calls=[dict(tool, result='result B')],
                choices=['A', 'B'],
            ),
        )
        self.assertNotEqual(
            exact_left,
            projection_hash(
                'same text',
                '[{"type":"text","text":"same text"}]',
                thinking='same thinking',
                tool_calls=[tool],
                choices=['A', 'C'],
            ),
        )

    def test_tool_result_pairing_is_exact_and_fail_closed(self):
        with self.assertRaises(CanonicalTurnError) as missing:
            self._build([
                _row('assistant', uuid='a1', message={'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': 'tool-1', 'name': 'read', 'input': {}},
                    {'type': 'text', 'text': 'done'},
                ]}),
                _row('result', stop_reason='end_turn'),
            ])
        self.assertEqual(missing.exception.error_code, 'tool_result_missing')

        with self.assertRaises(CanonicalTurnError) as unmatched:
            self._build([
                _row('user', message={'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'unknown', 'content': 'x'},
                ]}),
                _row('assistant', uuid='a1', message={'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'done'},
                ]}),
                _row('result', stop_reason='end_turn'),
            ])
        self.assertEqual(unmatched.exception.error_code, 'tool_result_unmatched')

        with self.assertRaises(CanonicalTurnError) as duplicate:
            self._build([
                _row('assistant', uuid='a1', message={'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': 'tool-1', 'name': 'read', 'input': {}},
                ]}),
                _row('user', message={'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'x'},
                ]}),
                _row('user', message={'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'x'},
                ]}),
                _row('assistant', uuid='a2', message={'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'done'},
                ]}),
                _row('result', stop_reason='end_turn'),
            ])
        self.assertEqual(duplicate.exception.error_code, 'tool_result_duplicate')


if __name__ == '__main__':
    unittest.main()
