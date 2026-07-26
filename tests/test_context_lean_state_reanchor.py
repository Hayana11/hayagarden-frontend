"""State lean 0→1 re-anchor per resident generation."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

from chat.context_budget import (
    build_state_send_payload,
    format_state_for_send,
    merge_cumulative_state_send,
    normalize_state_dict,
)
from cc_resident import ResidentSession


class LeanStateReanchorTests(unittest.TestCase):
    def setUp(self):
        self.raw = normalize_state_dict({
            'time_bucket': '当前时间段：上午 左右',
            'lights': '（灯·当前状态：主灯 关）',
            'emotion': '平静',
        })

    def _simulate_turn(self, sess, *, lean_on, prev_lean, flush_ok=True):
        if not lean_on:
            commit_meta = {'lean_state_active': False}
            if flush_ok:
                sess._commit_sent_context(commit_meta)
            return '', 'none', False
        needs_reanchor = lean_on and not prev_lean
        send_payload = build_state_send_payload(
            {} if needs_reanchor else sess.last_state_snapshot,
            self.raw,
            user_text='你好',
            is_cold=needs_reanchor,
        )
        cumulative_before = {} if needs_reanchor else sess.last_state_send_snapshot
        text, mode = format_state_for_send(cumulative_before, send_payload, is_cold=needs_reanchor)
        commit_meta = {
            'state_snapshot': copy.deepcopy(self.raw),
            'lean_state_active': lean_on,
        }
        if lean_on:
            commit_meta['state_send_snapshot'] = send_payload
            if needs_reanchor:
                commit_meta['lean_state_reanchor'] = True
        if flush_ok:
            sess._commit_sent_context(commit_meta)
        return text, mode, needs_reanchor

    def test_hot_legacy_to_lean_sends_full_snapshot_once(self):
        sess = ResidentSession('/tmp', '', '')
        sess._last_state_snapshot = dict(self.raw)
        text, mode, reanchor = self._simulate_turn(sess, lean_on=True, prev_lean=False)
        self.assertTrue(reanchor)
        self.assertEqual(mode, 'snapshot')
        self.assertIn('灯', text)

    def test_next_hot_turn_omits_unchanged(self):
        sess = ResidentSession('/tmp', '', '')
        self._simulate_turn(sess, lean_on=True, prev_lean=False)
        text, mode, reanchor = self._simulate_turn(sess, lean_on=True, prev_lean=True)
        self.assertFalse(reanchor)
        self.assertEqual(text, '')
        self.assertEqual(mode, 'none')

    def test_clear_sends_tombstone(self):
        sess = ResidentSession('/tmp', '', '')
        self._simulate_turn(sess, lean_on=True, prev_lean=False)
        cleared = dict(self.raw)
        cleared['lights'] = ''
        send = build_state_send_payload(sess.last_state_snapshot, cleared, user_text='嗯', is_cold=False)
        text, _ = format_state_for_send(sess.last_state_send_snapshot, send, is_cold=False)
        self.assertIn('已清空', text)

    def test_flush_failure_still_needs_reanchor(self):
        sess = ResidentSession('/tmp', '', '')
        self._simulate_turn(sess, lean_on=True, prev_lean=False, flush_ok=False)
        self.assertFalse(sess.last_successful_lean_state)
        text, mode, reanchor = self._simulate_turn(sess, lean_on=True, prev_lean=False)
        self.assertTrue(reanchor)
        self.assertEqual(mode, 'snapshot')

    def test_generation_unchanged_on_flag_toggle(self):
        sess = ResidentSession('/tmp', '', '')
        gen_before = sess.generation
        self._simulate_turn(sess, lean_on=True, prev_lean=False)
        self.assertEqual(sess.generation, gen_before)

    def test_one_to_zero_to_one_reanchors_again(self):
        sess = ResidentSession('/tmp', '', '')
        self._simulate_turn(sess, lean_on=True, prev_lean=False)
        self._simulate_turn(sess, lean_on=False, prev_lean=True)
        text, mode, reanchor = self._simulate_turn(sess, lean_on=True, prev_lean=False)
        self.assertTrue(reanchor)
        self.assertEqual(mode, 'snapshot')
        self.assertIn('灯', text)
        merged = merge_cumulative_state_send({}, build_state_send_payload({}, self.raw, is_cold=True))
        self.assertIn('lights', merged)


if __name__ == '__main__':
    unittest.main()
