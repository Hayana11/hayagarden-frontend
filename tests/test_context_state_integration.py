"""Integration tests for BP3 state send vs raw snapshot semantics."""

from __future__ import annotations

import copy
import unittest

from chat.context_budget import (
    build_state_send_payload,
    format_state_for_send,
    merge_cumulative_state_send,
    normalize_state_dict,
)


class StateSendIntegrationTests(unittest.TestCase):
  def setUp(self):
    self.raw = {
      'lights': '（灯·当前状态：主灯 关）',
      'emotion': '平静',
    }

  def test_cold_full_snapshot_then_hot_omits_unchanged_lights(self):
    send_cold = build_state_send_payload({}, self.raw, is_cold=True)
    text_cold, mode_cold = format_state_for_send({}, send_cold, is_cold=True)
    self.assertEqual(mode_cold, 'snapshot')
    self.assertIn('灯', text_cold)

    cumulative = merge_cumulative_state_send({}, send_cold)
    send_hot1 = build_state_send_payload(self.raw, self.raw, user_text='你好', is_cold=False)
    text_hot1, mode_hot1 = format_state_for_send(cumulative, send_hot1, is_cold=False)
    self.assertEqual(text_hot1, '')
    self.assertEqual(mode_hot1, 'none')
    self.assertNotIn('lights', send_hot1)

    send_hot2 = build_state_send_payload(self.raw, self.raw, user_text='继续聊', is_cold=False)
    text_hot2, _ = format_state_for_send(cumulative, send_hot2, is_cold=False)
    self.assertEqual(text_hot2, '')
    self.assertNotIn('已清空', text_hot2)

  def test_delayed_tombstone_four_turn_sequence(self):
    raw = normalize_state_dict(self.raw)
    cumulative = {}
    last_raw = {}

    send_cold = build_state_send_payload({}, raw, is_cold=True)
    text_cold, _ = format_state_for_send({}, send_cold, is_cold=True)
    self.assertIn('灯', text_cold)
    cumulative = merge_cumulative_state_send(cumulative, send_cold)
    last_raw = dict(raw)

    send_hot1 = build_state_send_payload(last_raw, raw, user_text='无关话题', is_cold=False)
    text_hot1, _ = format_state_for_send(cumulative, send_hot1, is_cold=False)
    self.assertEqual(text_hot1, '')
    self.assertNotIn('灯', text_hot1)
    cumulative = merge_cumulative_state_send(cumulative, send_hot1)

    send_hot2 = build_state_send_payload(last_raw, raw, user_text='继续', is_cold=False)
    text_hot2, _ = format_state_for_send(cumulative, send_hot2, is_cold=False)
    self.assertEqual(text_hot2, '')
    self.assertNotIn('灯', text_hot2)
    cumulative = merge_cumulative_state_send(cumulative, send_hot2)
    self.assertIn('lights', cumulative)

    cleared = dict(raw)
    cleared['lights'] = ''
    send_hot3 = build_state_send_payload(last_raw, cleared, user_text='嗯', is_cold=False)
    text_hot3, _ = format_state_for_send(cumulative, send_hot3, is_cold=False)
    self.assertIn('已清空', text_hot3)
    self.assertEqual(text_hot3.count('已清空'), 1)
    cumulative = merge_cumulative_state_send(cumulative, send_hot3)
    last_raw = cleared
    self.assertNotIn('lights', cumulative)

    send_hot4 = build_state_send_payload(last_raw, cleared, user_text='嗯', is_cold=False)
    text_hot4, _ = format_state_for_send(cumulative, send_hot4, is_cold=False)
    self.assertEqual(text_hot4, '')
    self.assertNotIn('已清空', text_hot4)

  def test_tombstone_only_when_raw_cleared(self):
    cleared = dict(self.raw)
    cleared['lights'] = ''
    cumulative = merge_cumulative_state_send(
      {},
      build_state_send_payload({}, self.raw, is_cold=True),
    )
    send = build_state_send_payload(self.raw, cleared, user_text='嗯', is_cold=False)
    text, _ = format_state_for_send(cumulative, send, is_cold=False)
    self.assertIn('已清空', text)

  def test_user_relevant_resends_unchanged_lights(self):
    cumulative = merge_cumulative_state_send(
      {},
      build_state_send_payload({}, self.raw, is_cold=True),
    )
    send = build_state_send_payload(self.raw, self.raw, user_text='床头灯还亮吗', is_cold=False)
    text, mode = format_state_for_send(cumulative, send, is_cold=False)
    self.assertIn('灯', text)
    self.assertEqual(mode, 'delta')
    merged = merge_cumulative_state_send(cumulative, send)
    self.assertIn('lights', merged)

  def test_snapshot_commit_is_full_raw_not_send_payload(self):
    raw = normalize_state_dict(self.raw)
    send = build_state_send_payload(self.raw, self.raw, user_text='你好', is_cold=False)
    committed = copy.deepcopy(raw)
    self.assertIn('lights', committed)
    self.assertNotIn('lights', send)

  def test_three_turn_sequence(self):
    raw1 = normalize_state_dict(self.raw)
    send1 = build_state_send_payload({}, raw1, is_cold=True)
    cumulative = merge_cumulative_state_send({}, send1)
    last_raw = raw1

    send2 = build_state_send_payload(last_raw, raw1, user_text='你好', is_cold=False)
    t2, _ = format_state_for_send(cumulative, send2, is_cold=False)
    self.assertEqual(t2, '')
    cumulative = merge_cumulative_state_send(cumulative, send2)
    self.assertEqual(last_raw['lights'], raw1['lights'])

    send3 = build_state_send_payload(last_raw, raw1, user_text='继续', is_cold=False)
    t3, _ = format_state_for_send(cumulative, send3, is_cold=False)
    self.assertEqual(t3, '')
    self.assertNotIn('已清空', t3)


if __name__ == '__main__':
  unittest.main()
