"""Relay history hysteresis and cache prefix stability tests."""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from chat.history_assembly import assemble_history_from_rows, strip_internal_metadata
from chat.history_boundary import (
    effective_trimmed_up_to_id,
    has_relay_prior_trim,
    rolling_summary_covers_boundary,
    set_relay_history_trimmed_up_to_id,
    should_inject_rolling_summary,
)
from chat.rolling_summary_store import get_summary, save_summary


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

    def test_relay_delayed_summary_four_round_sequence(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(tmp.name) / 'memories.db')
        store = {}
        def _get_int(key, default=0):
            if key == 'RELAY_HISTORY_TRIMMED_UP_TO_ID':
                return store.get('trim', 0)
            if key == 'RELAY_HISTORY_HEAD_ID':
                return store.get('head', 0)
            return default

        def _set(key, val):
            if key == 'RELAY_HISTORY_TRIMMED_UP_TO_ID':
                store['trim'] = int(val)
            elif key == 'RELAY_HISTORY_HEAD_ID':
                store['head'] = int(val)

        with mock.patch('config_store.get_int', side_effect=_get_int), \
             mock.patch('config_store.set', side_effect=_set), \
             mock.patch('chat.rolling_summary_store._db_path', return_value=db_path):
            rows = [_row(i, 'x' * 200) for i in range(1, 11)]
            msgs1, stats1 = assemble_history_from_rows(
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
            self.assertTrue(stats1.conversation_content_trimmed)
            trimmed_up_to = effective_trimmed_up_to_id(
                current_trimmed_up_to_id=stats1.trimmed_up_to_id,
                history_mode='relay_hysteresis',
            )
            inject1 = should_inject_rolling_summary(
                conversation_content_trimmed=stats1.conversation_content_trimmed,
                history_mode='relay_hysteresis',
            )
            self.assertTrue(inject1)
            self.assertFalse(rolling_summary_covers_boundary(0, trimmed_up_to))

            # Round 2: no re-trim, summary still missing
            msgs2, stats2 = assemble_history_from_rows(
                rows,
                available_count=10,
                history_mode='relay_hysteresis',
                relay_high_water=800,
                relay_low_water=500,
                relay_head_id=store.get('head', 1),
                static_dir='/tmp',
                read_file_fn=lambda *_a, **_k: None,
                img_block_fn=lambda *_a, **_k: None,
                is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
                estimate_tokens=_estimate,
            )
            self.assertFalse(stats2.conversation_content_trimmed)
            self.assertTrue(has_relay_prior_trim())
            inject2 = should_inject_rolling_summary(
                conversation_content_trimmed=stats2.conversation_content_trimmed,
                history_mode='relay_hysteresis',
            )
            self.assertTrue(inject2)
            self.assertFalse(rolling_summary_covers_boundary(0, trimmed_up_to))

            # Background summary written
            save_summary('relay_hysteresis', summary='早前聊过。', up_to_id=trimmed_up_to)

            # Round 3: inject summary without moving head
            inject3 = should_inject_rolling_summary(
                conversation_content_trimmed=False,
                history_mode='relay_hysteresis',
            )
            self.assertTrue(inject3)
            rolling = get_summary('relay_hysteresis')
            self.assertTrue(rolling_summary_covers_boundary(rolling['up_to_id'], trimmed_up_to))

            # Round 4: still inject, head stable
            head_before = store.get('head', 0)
            msgs4, stats4 = assemble_history_from_rows(
                rows,
                available_count=10,
                history_mode='relay_hysteresis',
                relay_high_water=800,
                relay_low_water=500,
                relay_head_id=head_before or 1,
                static_dir='/tmp',
                read_file_fn=lambda *_a, **_k: None,
                img_block_fn=lambda *_a, **_k: None,
                is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
                estimate_tokens=_estimate,
            )
            self.assertFalse(stats4.conversation_content_trimmed)
            self.assertEqual(store.get('head', 0), head_before)
            inject4 = should_inject_rolling_summary(
                conversation_content_trimmed=False,
                history_mode='relay_hysteresis',
            )
            self.assertTrue(inject4)
            _ = msgs1, msgs2, msgs4
        tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
