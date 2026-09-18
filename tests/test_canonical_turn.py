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
    def _build(self, rows, mode='auto'):
        path = _write(rows)
        try:
            end = path.stat().st_size
            return build_canonical_turn(
                str(path),
                start_offset=0,
                end_offset=end,
                session_id='session-1',
                mode=mode,
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


if __name__ == '__main__':
    unittest.main()
