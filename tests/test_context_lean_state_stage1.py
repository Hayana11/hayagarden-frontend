"""Stage 1: State delta / re-anchor — fixtures, parity, and observation contracts."""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from chat.context_budget import merge_cumulative_state_send, normalize_state_dict
from chat.context_lean_state import (
    STATE_SCHEMA_VERSION,
    assemble_cc_state_context,
    compute_state_version,
    contains_style_instruction,
    evaluate_reanchor_reason,
    format_structured_state_delta,
    format_structured_state_anchor,
)
from chat.system_builder import (
    build_cc_static_system,
    format_state_diff,
    format_state_snapshot,
)
from cc_resident import ResidentSession
from tools.cc_usage_observability import sha256_text


def _resident_stub(**overrides):
    base = {
        'generation': 1,
        'last_state_snapshot': {},
        'last_state_send_snapshot': {},
        'last_successful_lean_state': False,
        'last_state_anchor_generation': -1,
        'last_state_schema_version': None,
        'turns_since_state_anchor': 0,
        'state_delta_chars_since_anchor': 0,
        'last_state_anchor_version': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


RAW_V1 = normalize_state_dict({
    'time_bucket': '当前时间段：上午 左右',
    'emotion': 'valence=0.60 arousal=0.30 mood=平静 pa=0.50 na=0.20 longing=0.35 desire_p=0.10 desire_i=0.30 desire_c=0.70',
    'drive': 'attachment=0.55 curiosity=0.20',
    'lights': '（灯·当前状态：主灯 关）',
})


class FlagZeroParityTests(unittest.TestCase):
    def test_static_system_hash_unchanged(self):
        with mock.patch('config_store.get_bool', return_value=False):
            h1 = sha256_text(build_cc_static_system())
            h2 = sha256_text(build_cc_static_system())
        self.assertEqual(h1, h2)

    @mock.patch('chat.context_lean.lean_state_enabled', return_value=False)
    def test_legacy_state_path_byte_stable(self, _lean):
        raw = {
            'time_bucket': '当前时间段：上午 左右',
            'emotion': '## 此刻的情绪与欲望\n情绪：V0.60/A0.30 — 平静',
            'lights': '（灯·主灯 关）',
        }
        snap = format_state_snapshot(raw)
        diff = format_state_diff(raw, raw)
        self.assertIn('【当前状态】', snap)
        self.assertEqual(diff, '')
        resident = _resident_stub(last_state_snapshot=raw)
        result = assemble_cc_state_context(
            raw_state=raw,
            is_cold=False,
            user_text='你好',
            resident=resident,
        )
        self.assertFalse(result.used_lean)
        self.assertEqual(result.state_text, '')
        self.assertEqual(result.state_mode, 'none')
        self.assertFalse(result.observation['context_lean_state_enabled'])


class StructuredFormatTests(unittest.TestCase):
    def test_anchor_has_schema_and_no_style_ban(self):
        ver = compute_state_version(RAW_V1)
        text = format_structured_state_anchor(RAW_V1, state_version=ver)
        self.assertIn('schema_version=1', text)
        self.assertIn('state_version=', text)
        self.assertNotIn('话少一些', text)
        self.assertNotIn('安静等待', text)

    def test_delta_lists_changed_fields(self):
        after = dict(RAW_V1)
        after['lights'] = '（灯·主灯 开）'
        send = {'lights': after['lights']}
        text = format_structured_state_delta(
            RAW_V1,
            send,
            prev_version=compute_state_version(RAW_V1),
            curr_version=compute_state_version(after),
        )
        self.assertIn('changed=lights', text)
        self.assertIn('lights:', text)

    def test_style_instruction_detector(self):
        self.assertTrue(contains_style_instruction('很想但已经变成安静等着，话少一些。'))
        self.assertFalse(contains_style_instruction('valence=0.60 arousal=0.30'))


class ReanchorDecisionTests(unittest.TestCase):
    def test_cold_start(self):
        reason = evaluate_reanchor_reason(
            lean_on=True,
            prev_lean_success=True,
            is_cold=True,
            resident_generation=1,
            last_anchor_generation=1,
            last_schema_version=STATE_SCHEMA_VERSION,
            turns_since_anchor=0,
            delta_chars_since_anchor=0,
            cumulative_send_nonempty=True,
        )
        self.assertEqual(reason, 'cold_start')

    def test_generation_change(self):
        reason = evaluate_reanchor_reason(
            lean_on=True,
            prev_lean_success=True,
            is_cold=False,
            resident_generation=2,
            last_anchor_generation=1,
            last_schema_version=STATE_SCHEMA_VERSION,
            turns_since_anchor=1,
            delta_chars_since_anchor=0,
            cumulative_send_nonempty=True,
        )
        self.assertEqual(reason, 'resident_generation_change')

    def test_turn_interval(self):
        from chat.context_lean_state import REANCHOR_TURN_INTERVAL
        reason = evaluate_reanchor_reason(
            lean_on=True,
            prev_lean_success=True,
            is_cold=False,
            resident_generation=1,
            last_anchor_generation=1,
            last_schema_version=STATE_SCHEMA_VERSION,
            turns_since_anchor=REANCHOR_TURN_INTERVAL,
            delta_chars_since_anchor=0,
            cumulative_send_nonempty=True,
        )
        self.assertEqual(reason, 'reanchor_turn_interval')


class AssembleContextFixtureTests(unittest.TestCase):
    @mock.patch('chat.context_lean.lean_state_enabled', return_value=True)
    def test_cold_full_anchor(self, _lean):
        resident = _resident_stub()
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=True,
            user_text='你好',
            resident=resident,
        )
        self.assertEqual(result.state_context_mode, 'full_anchor')
        self.assertIn('锚点', result.state_text)
        self.assertEqual(result.reanchor_reason, 'cold_start')

    @mock.patch('chat.context_lean.lean_state_enabled', return_value=True)
    def test_hot_unchanged_omitted(self, _lean):
        resident = _resident_stub(
            last_successful_lean_state=True,
            last_state_snapshot=RAW_V1,
            last_state_send_snapshot=RAW_V1,
            last_state_anchor_generation=1,
            last_state_schema_version=STATE_SCHEMA_VERSION,
        )
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=False,
            user_text='继续聊',
            resident=resident,
        )
        self.assertEqual(result.state_context_mode, 'omitted')
        self.assertEqual(result.state_text, '')

    @mock.patch('chat.context_lean.lean_state_enabled', return_value=True)
    def test_hot_single_field_delta(self, _lean):
        resident = _resident_stub(
            last_successful_lean_state=True,
            last_state_snapshot=RAW_V1,
            last_state_send_snapshot=RAW_V1,
            last_state_anchor_generation=1,
            generation=1,
            last_state_schema_version=STATE_SCHEMA_VERSION,
        )
        changed = dict(RAW_V1)
        changed['lights'] = '（灯·主灯 开）'
        result = assemble_cc_state_context(
            raw_state=changed,
            is_cold=False,
            user_text='无关',
            resident=resident,
        )
        self.assertEqual(result.state_context_mode, 'delta')
        self.assertIn('灯', result.state_text)
        self.assertGreaterEqual(result.observation['changed_field_count'], 1)

    @mock.patch('chat.context_lean.lean_state_enabled', return_value=True)
    def test_hot_multi_field_delta(self, _lean):
        resident = _resident_stub(
            last_successful_lean_state=True,
            last_state_snapshot=RAW_V1,
            last_state_send_snapshot=RAW_V1,
            last_state_anchor_generation=1,
            generation=1,
            last_state_schema_version=STATE_SCHEMA_VERSION,
        )
        changed = dict(RAW_V1)
        changed['lights'] = '（灯·主灯 开）'
        changed['drive'] = 'attachment=0.80 curiosity=0.20'
        result = assemble_cc_state_context(
            raw_state=changed,
            is_cold=False,
            user_text='无关',
            resident=resident,
        )
        self.assertEqual(result.state_context_mode, 'delta')
        self.assertGreaterEqual(result.observation['changed_field_count'], 2)

    @mock.patch('chat.context_lean.lean_state_enabled', return_value=True)
    def test_generation_change_reanchor(self, _lean):
        resident = _resident_stub(
            last_successful_lean_state=True,
            last_state_snapshot=RAW_V1,
            last_state_send_snapshot=RAW_V1,
            last_state_anchor_generation=1,
            generation=2,
            last_state_schema_version=STATE_SCHEMA_VERSION,
        )
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=False,
            user_text='你好',
            resident=resident,
        )
        self.assertEqual(result.reanchor_reason, 'resident_generation_change')
        self.assertEqual(result.state_context_mode, 'full_anchor')

    @mock.patch('chat.context_lean.lean_state_enabled', return_value=True)
    def test_invalid_delta_fallback(self, _lean):
        resident = _resident_stub(last_successful_lean_state=True)
        with mock.patch(
            'chat.context_lean_state.format_lean_state_for_send',
            side_effect=ValueError('bad delta'),
        ):
            result = assemble_cc_state_context(
                raw_state=RAW_V1,
                is_cold=False,
                user_text='你好',
                resident=resident,
            )
        self.assertEqual(result.state_context_mode, 'fallback')
        self.assertIsNotNone(result.fallback_reason)

    @mock.patch('chat.context_lean.lean_state_enabled', return_value=True)
    def test_observation_v3_fields_present(self, _lean):
        resident = _resident_stub()
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=True,
            user_text='你好',
            resident=resident,
        )
        obs = result.observation
        for key in (
            'context_lean_state_enabled',
            'state_context_mode',
            'state_version',
            'anchor_version',
            'changed_field_count',
            'state_context_chars',
            'state_context_estimated_tokens',
            'reanchor_reason',
            'fallback_reason',
            'resident_generation',
            'observation_version',
        ):
            self.assertIn(key, obs)
        self.assertEqual(obs['observation_version'], 3)


class ResidentCommitTests(unittest.TestCase):
    def test_reanchor_resets_delta_counter(self):
        sess = ResidentSession('/tmp', '', '')
        sess._commit_sent_context({
            'state_snapshot': RAW_V1,
            'lean_state_active': True,
            'lean_state_reanchor': True,
            'state_schema_version': STATE_SCHEMA_VERSION,
            'state_version': compute_state_version(RAW_V1),
            'state_context_chars': 500,
        })
        self.assertEqual(sess.turns_since_state_anchor, 0)
        self.assertEqual(sess.state_delta_chars_since_anchor, 0)
        self.assertEqual(sess.last_state_anchor_generation, sess.generation)

    def test_hot_turn_accumulates_delta_chars(self):
        sess = ResidentSession('/tmp', '', '')
        sess._last_successful_lean_state = True
        sess._commit_sent_context({
            'state_snapshot': RAW_V1,
            'lean_state_active': True,
            'state_send_snapshot': {'lights': 'x'},
            'state_context_chars': 120,
        })
        self.assertEqual(sess.turns_since_state_anchor, 1)
        self.assertEqual(sess.state_delta_chars_since_anchor, 120)


class OneShotAndBridgePreservationTests(unittest.TestCase):
    """State lean must not alter one-shot / bridge assembly contracts."""

    def test_one_shot_keys_untouched_by_state_module(self):
        from chat.system_builder import format_one_shot
        one_shot = {
            'task_feedback': '反馈内容',
            'wake_nonmessage_background': '后台 wake',
            'wake_reply_bridge': 'bridge 不应进 format_one_shot',
        }
        text = format_one_shot(one_shot)
        self.assertIn('反馈内容', text)
        self.assertNotIn('bridge', text)


if __name__ == '__main__':
    unittest.main()
