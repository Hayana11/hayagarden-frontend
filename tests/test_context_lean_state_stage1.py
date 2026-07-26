"""Stage 1: State delta / re-anchor — fixtures, parity, and observation contracts."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from chat.context_budget import normalize_state_dict
from chat.context_lean_state import (
    STATE_SCHEMA_VERSION,
    assemble_cc_state_context,
    assemble_legacy_full_fallback,
    compute_state_version,
    evaluate_reanchor_reason,
    format_structured_state_delta,
    format_structured_state_anchor,
)
from chat.system_builder import format_state_diff, format_state_snapshot
from cc_resident import ResidentSession


# Frozen legacy fixture (representative production-shaped strings).
LEGACY_RAW = normalize_state_dict({
    'time_bucket': '当前时间段：上午 左右',
    'emotion': '## 此刻的情绪与欲望\n情绪：V0.60/A0.30 — 平静\nPA 0.50 | NA 0.20 | 混合张力',
    'drive': '驱动：想她 中等（0.55）',
    'lights': '（灯·当前状态：主灯 关，床头灯 关）',
    'reminders': '## 今日提醒\n- 修改 AI 的语气，让表达更克制',
})

RAW_V1 = normalize_state_dict({
    'time_bucket': '当前时间段：上午 左右',
    'emotion': 'valence=0.60 arousal=0.30 mood=平静 pa=0.50 na=0.20 longing=0.35 desire_p=0.10 desire_i=0.30 desire_c=0.70',
    'drive': 'attachment=0.55 curiosity=0.20',
    'lights': '（灯·当前状态：主灯 关）',
})


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


class FlagZeroParityTests(unittest.TestCase):
    def test_cold_snapshot_matches_legacy_formatter(self):
        resident = _resident_stub()
        expected = format_state_snapshot(LEGACY_RAW)
        result = assemble_cc_state_context(
            raw_state=LEGACY_RAW,
            is_cold=True,
            user_text='你好',
            resident=resident,
            lean_on=False,
        )
        self.assertEqual(result.state_text, expected)
        self.assertFalse(result.used_lean)

    def test_hot_diff_matches_legacy_formatter(self):
        before = dict(LEGACY_RAW)
        after = dict(LEGACY_RAW)
        after['lights'] = '（灯·当前状态：主灯 开，床头灯 关）'
        resident = _resident_stub(last_state_snapshot=before)
        expected = format_state_diff(before, after)
        result = assemble_cc_state_context(
            raw_state=after,
            is_cold=False,
            user_text='灯还亮吗',
            resident=resident,
            lean_on=False,
        )
        self.assertEqual(result.state_text, expected)
        self.assertFalse(result.used_lean)

    def test_hot_unchanged_matches_empty_legacy_diff(self):
        resident = _resident_stub(last_state_snapshot=LEGACY_RAW)
        result = assemble_cc_state_context(
            raw_state=LEGACY_RAW,
            is_cold=False,
            user_text='继续',
            resident=resident,
            lean_on=False,
        )
        self.assertEqual(result.state_text, '')
        self.assertEqual(result.state_mode, 'none')


class StructuredFormatTests(unittest.TestCase):
    def test_anchor_preserves_user_fact_text_verbatim(self):
        payload = {'reminders': '## 今日提醒\n- 修改 AI 的语气，让表达更克制'}
        ver = compute_state_version(payload)
        text = format_structured_state_anchor(payload, state_version=ver)
        self.assertIn('修改 AI 的语气，让表达更克制', text)
        self.assertIn('语气', text)
        self.assertIn('克制', text)

    def test_delta_lists_changed_fields(self):
        after = dict(RAW_V1)
        after['lights'] = '（灯·主灯 开）'
        send = {'lights': after['lights']}
        text = format_structured_state_delta(
            RAW_V1,
            send,
            prev_version=compute_state_version(RAW_V1),
            curr_version=compute_state_version(send),
        )
        self.assertIn('changed=lights', text)
        self.assertIn('lights:', text)

    def test_tombstone_changed_field_count(self):
        resident = _resident_stub(
            last_successful_lean_state=True,
            last_state_snapshot=RAW_V1,
            last_state_send_snapshot=RAW_V1,
            last_state_anchor_generation=1,
            generation=1,
            last_state_schema_version=STATE_SCHEMA_VERSION,
        )
        cleared = dict(RAW_V1)
        cleared['lights'] = ''
        result = assemble_cc_state_context(
            raw_state=cleared,
            is_cold=False,
            user_text='嗯',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'delta')
        self.assertEqual(result.observation['changed_field_count'], len(result.send_payload))
        self.assertIn('lights', result.send_payload)
        self.assertEqual(result.send_payload['lights'], '')
        self.assertIn('lights: cleared', result.state_text)
        self.assertEqual(
            compute_state_version(result.send_payload),
            result.observation['state_version'],
        )


class FallbackTests(unittest.TestCase):
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


class AssembleContextFixtureTests(unittest.TestCase):
    def test_cold_full_anchor(self):
        resident = _resident_stub()
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=True,
            user_text='你好',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'full_anchor')
        self.assertIn('锚点', result.state_text)
        self.assertEqual(result.reanchor_reason, 'cold_start')
        self.assertEqual(
            compute_state_version(result.send_payload),
            result.observation['state_version'],
        )

    def test_hot_unchanged_omitted(self):
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
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'omitted')
        self.assertEqual(result.state_text, '')

    def test_hot_single_field_delta(self):
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
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'delta')
        self.assertIn('灯', result.state_text)
        self.assertGreaterEqual(result.observation['changed_field_count'], 1)

    def test_generation_change_reanchor(self):
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
            lean_on=True,
        )
        self.assertEqual(result.reanchor_reason, 'resident_generation_change')
        self.assertEqual(result.state_context_mode, 'full_anchor')

    def test_observation_v3_fields_present(self):
        resident = _resident_stub()
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=True,
            user_text='你好',
            resident=resident,
            lean_on=True,
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


class FallbackTests(unittest.TestCase):
    def test_legacy_full_fallback_uses_snapshot_not_diff(self):
        resident = _resident_stub(last_state_snapshot=RAW_V1)
        legacy = dict(LEGACY_RAW)
        result = assemble_legacy_full_fallback(
            legacy_raw_state=legacy,
            fallback_reason='ValueError',
            resident=resident,
        )
        self.assertEqual(result.state_context_mode, 'fallback')
        self.assertEqual(result.state_text, format_state_snapshot(legacy))
        self.assertEqual(result.state_mode, 'snapshot')
        self.assertFalse(result.commit_meta_extras.get('lean_state_active'))
        self.assertTrue(result.commit_meta_extras.get('state_lean_fallback'))


class ResidentCommitTests(unittest.TestCase):
    def test_reanchor_resets_delta_counter(self):
        sess = ResidentSession('/tmp', '', '')
        payload = dict(RAW_V1)
        sess._commit_sent_context({
            'state_snapshot': RAW_V1,
            'lean_state_active': True,
            'lean_state_reanchor': True,
            'state_send_snapshot': payload,
            'state_schema_version': STATE_SCHEMA_VERSION,
            'state_version': compute_state_version(payload),
            'state_context_chars': 500,
        })
        self.assertEqual(sess.turns_since_state_anchor, 0)
        self.assertEqual(sess.state_delta_chars_since_anchor, 0)


class OneShotPreservationTests(unittest.TestCase):
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
