import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = __import__('importlib.util').util.spec_from_file_location(
    'chat_display_segments_under_test', ROOT / 'chat' / 'display_segments.py',
)
_module = __import__('importlib.util').util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
DisplaySegmentAccumulator = _module.DisplaySegmentAccumulator


class ChatDurableOrderTests(unittest.TestCase):
    def test_t1_exact_interleaving_order(self):
        acc = DisplaySegmentAccumulator()
        acc.append_thinking('thinking A')
        acc.append_text('text B')
        acc.append_tool(0)
        acc.append_text('text C')
        acc.append_tool(1)
        acc.append_text('text D')
        self.assertEqual(acc.as_list(), [
            {'type': 'thinking', 'text': 'thinking A'},
            {'type': 'text', 'text': 'text B'},
            {'type': 'tool', 'tool_index': 0},
            {'type': 'text', 'text': 'text C'},
            {'type': 'tool', 'tool_index': 1},
            {'type': 'text', 'text': 'text D'},
        ])

    def test_t2_adjacent_deltas_merge_non_adjacent_split(self):
        acc = DisplaySegmentAccumulator()
        acc.append_text('a')
        acc.append_text('b')
        acc.append_thinking('x')
        acc.append_text('c')
        acc.append_text('d')
        self.assertEqual(acc.as_list(), [
            {'type': 'text', 'text': 'ab'},
            {'type': 'thinking', 'text': 'x'},
            {'type': 'text', 'text': 'cd'},
        ])

    def test_t3_tool_result_does_not_change_order(self):
        acc = DisplaySegmentAccumulator()
        acc.append_text('before')
        acc.append_tool(0)
        before = acc.as_list()
        acc.apply_tool_result(0)
        self.assertEqual(acc.as_list(), before)

    def test_t7_high_frequency_delta_accumulation(self):
        acc = DisplaySegmentAccumulator()
        for _ in range(5000):
            acc.append_text('x')
        self.assertEqual(acc.as_list(), [{'type': 'text', 'text': 'x' * 5000}])
        self.assertTrue(acc.to_json())

    def test_t8_staging_candidate_contract(self):
        staging = (ROOT / 'chat' / 'rewrite_staging.py').read_text(encoding='utf-8')
        self.assertIn('candidate_display_segments TEXT', staging)
        self.assertIn('candidate_display_segments=display_segments or', staging)
        self.assertIn("'display_segments': new_branch['display_segments']", staging)
        self.assertIn("updates['display_segments']", staging)
        self.assertIn("('display_segments', row.get('candidate_display_segments') or '')", staging)

    def test_t9_additive_migration(self):
        app = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertRegex(app, r"'display_segments'\s*:\s*['\"]ALTER TABLE chat_messages ADD COLUMN display_segments TEXT DEFAULT ''")
        migration = re.search(r"def _migrate_chat_columns\(\):([\s\S]*?)\n\n_migrate_chat_columns", app)
        self.assertIsNotNone(migration)
        body = migration.group(1)
        self.assertNotIn('DROP TABLE', body)
        self.assertNotRegex(body, r'UPDATE\s+chat_messages', re.I)

    def test_t10_all_persistence_paths_carry_display_segments(self):
        gateway = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('from chat.display_segments import DisplaySegmentAccumulator', gateway)
        self.assertGreaterEqual(gateway.count('display_segments.to_json()'), 5)
        self.assertIn('display_segments_json=display_segments.to_json()', gateway)
        self.assertIn('display_segments=display_segments_json', gateway)
        for marker in ('_stream_cc_deferred_confirmation', '_stream_cc_first_turn',
                       '_stream_cc_daily_soft_window', "phase = 'resident_stream'",
                       'think_acc, text_acc, tool_calls_acc'):
            self.assertIn(marker, gateway)

    def test_t11_live_handoff_contract(self):
        screen = (ROOT / 'app' / 'src' / 'screens' / 'ChatScreen.tsx').read_text(encoding='utf-8')
        self.assertIn('const clearLivePresentation = useCallback', screen)
        self.assertIn('if (!res.ok || res.deferredTool) clearLivePresentation()', screen)
        self.assertGreaterEqual(screen.count('await refetchLatest();\n    clearLivePresentation();'), 2)

    def test_t4_json_is_compact_and_parseable(self):
        acc = DisplaySegmentAccumulator()
        acc.append_thinking('t')
        acc.append_tool(3)
        payload = acc.to_json()
        self.assertEqual(json.loads(payload), acc.as_list())
        self.assertNotIn(' ', payload)


if __name__ == '__main__':
    unittest.main()
