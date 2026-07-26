"""Relay history hysteresis and cache prefix stability tests."""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from chat.history_assembly import assemble_history_from_rows, strip_internal_metadata


def _row(mid, content):
    return SimpleNamespace(
        id=mid, author='hayana' if mid % 2 else 'assistant', content=content,
        image_url='', created_at='2026-07-26 12:00:00', tool_calls='',
        file_url='', file_name='',
    )


def _estimate(text):
    return len(text or '')


class RelayHistoryCacheTests(unittest.TestCase):
    def test_below_high_water_keeps_prefix_stable(self):
        rows = [_row(i, 'm%d' % i) for i in range(1, 6)]
        msgs1, stats1 = assemble_history_from_rows(
            rows,
            available_count=5,
            history_mode='relay_hysteresis',
            relay_high_water=1000,
            relay_low_water=500,
            relay_head_id=1,
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            estimate_tokens=_estimate,
        )
        rows2 = rows + [_row(6, 'm6')]
        msgs2, stats2 = assemble_history_from_rows(
            rows2,
            available_count=6,
            history_mode='relay_hysteresis',
            relay_high_water=1000,
            relay_low_water=500,
            relay_head_id=1,
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            estimate_tokens=_estimate,
        )
        self.assertFalse(stats1.conversation_content_trimmed)
        self.assertFalse(stats2.conversation_content_trimmed)
        self.assertEqual(
            strip_internal_metadata(msgs1)[0]['content'],
            strip_internal_metadata(msgs2)[0]['content'],
        )

    def test_above_high_water_trims_once_to_low(self):
        rows = [_row(i, 'x' * 200) for i in range(1, 11)]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=10,
            history_mode='relay_hysteresis',
            relay_high_water=800,
            relay_low_water=500,
            relay_head_id=1,
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            estimate_tokens=_estimate,
        )
        self.assertTrue(stats.conversation_content_trimmed)
        tokens = sum(_estimate(m.get('content') if isinstance(m.get('content'), str) else json.dumps(m.get('content'))) for m in msgs)
        self.assertLessEqual(tokens, 800)

    def test_cache_breakpoint_on_penultimate_user(self):
        from tests.test_cc_context_dedup import _import_gateway
        _apply = _import_gateway()._apply_rolling_cache_control
        msgs = [
            {'role': 'user', 'content': 'old'},
            {'role': 'assistant', 'content': 'a'},
            {'role': 'user', 'content': 'prev'},
            {'role': 'assistant', 'content': 'b'},
            {'role': 'user', 'content': 'latest'},
        ]
        out = _apply(copy.deepcopy(msgs))
        user_idxs = [i for i, m in enumerate(out) if m.get('role') == 'user']
        penultimate = out[user_idxs[-2]]
        content = penultimate.get('content')
        if isinstance(content, list):
            self.assertTrue(any(isinstance(b, dict) and b.get('cache_control') for b in content))
        else:
            self.assertIn('cache_control', json.dumps(out[user_idxs[-2]]))


if __name__ == '__main__':
    unittest.main()
