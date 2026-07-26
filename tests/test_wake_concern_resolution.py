"""Regression tests for Wake concern-resolution filtering (Memory Hotfix)."""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-wake-resolution-config.db'),
)

from chat import context_continuity
from wake import concern_resolution as cr


class ConcernResolutionLogicTests(unittest.TestCase):
    def test_user_resolution_detected_not_assistant(self):
        self.assertTrue(cr.is_user_resolution('伤口已经愈合，医生说不用打破伤风了。'))
        self.assertFalse(cr.is_user_resolution('嗯嗯，我知道了。'))

    def test_negated_and_rhetorical_not_resolution(self):
        self.assertFalse(cr.is_user_resolution('才不是没事了，你别瞎担心。'))
        self.assertFalse(cr.is_user_resolution('你以为结束了？还早着呢。'))
        self.assertFalse(cr.is_user_resolution('才不是没事了'))
        self.assertTrue(cr.is_user_resolution('才不是没事了，后来确认已经好了。'))
        self.assertTrue(cr.is_user_reopen('你以为结束了？其实今天又报错了。'))
        self.assertFalse(cr.is_user_resolution('你以为结束了？其实今天又报错了。'))

    def test_comfort_phrases_still_count_as_resolution(self):
        self.assertTrue(cr.is_user_resolution('别担心，伤口已经好了。'))
        self.assertTrue(cr.is_user_resolution('没有啦，已经没事了。'))

    def test_question_form_is_not_resolution(self):
        self.assertFalse(cr.is_user_resolution('医生说不用打针吗？'))
        self.assertFalse(cr.is_user_resolution('医生说不用打针吗？我没听清。'))
        self.assertFalse(cr.is_user_resolution('我忘了，医生说不用打针？后来怎么说的。'))

    def test_movie_ended_is_not_resolution(self):
        self.assertFalse(cr.is_user_resolution('电影结束了，挺好看的。'))

    def test_reopen_removes_matching_resolution(self):
        messages = [
            {'id': 1, 'content': '被猫抓伤了，有点担心。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '已经咨询医生，伤口愈合了，不用打针。', 'created_at': '2026-07-21 12:00:00'},
            {'id': 3, 'content': '伤口又红肿了，还是得去医院。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_reopen_without_repeat_words(self):
        messages = [
            {'id': 1, 'content': '医生说不用打针，伤口已经好了。', 'created_at': '2026-07-21 12:00:00'},
            {'id': 2, 'content': '伤口今天开始红肿了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_express_delivery_resolution_filters_matching_worry(self):
        messages = [
            {'id': 1, 'content': '快递还没取，我有点担心。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '快递已经取完，不用再跑了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想快递有没有取。', state, recorded_at='2026-07-20 11:00:00',
        ))

    def test_factory_progress_resolution(self):
        messages = [
            {'id': 1, 'content': '工厂订单的进度还没跟完。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '工厂订单的进度已经跟完了，不用再催了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想工厂订单的进度。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_software_fault_reopen_after_resolution(self):
        messages = [
            {'id': 1, 'content': '服务故障已经修好了，可以正常用了。', 'created_at': '2026-07-21 12:00:00'},
            {'id': 2, 'content': '服务故障又报错了，还得排查。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_adjacent_unrelated_concerns_no_cross_supersede(self):
        messages = [
            {'id': 1, 'content': '快递还没取，我有点担心。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '工厂进度还没跟完。', 'created_at': '2026-07-20 11:00:00'},
            {'id': 3, 'content': '快递已经取完，不用再跑了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想工厂订单进度。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想快递有没有取。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_deictic_resolution_uses_immediate_prior_chat(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': '记得提醒我买花。', 'created_at': '2026-07-20 08:00:00'},
            {'id': 2, 'author': 'assistant', 'content': '好的，我记下了。', 'created_at': '2026-07-20 08:05:00'},
            {'id': 3, 'author': 'assistant', 'content': '还担心你的猫抓伤。', 'created_at': '2026-07-20 09:00:00'},
            {'id': 4, 'author': 'hayana', 'content': '这件事结束了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        entry = state.active[0]
        self.assertTrue(any('抓伤' in token for token in entry.topic_tokens))
        self.assertNotIn('买花', entry.topic_tokens)

    def test_unrelated_concerns_do_not_cross(self):
        messages = [
            {'id': 1, 'content': '超市鸡蛋打折的事已经处理好了。', 'created_at': '2026-07-21 10:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        wake_text = '还在想她需不需要打破伤风针。'
        self.assertFalse(cr.is_superseded_historical_concern(
            wake_text, state, recorded_at='2026-07-20 10:00:00',
        ))

    def test_superseded_wake_requires_time_before_resolution(self):
        messages = [
            {'id': 1, 'content': '医生说不用打破伤风，伤口已经愈合。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        wake_text = '醒来还在想破伤风要不要打。'
        self.assertTrue(cr.is_superseded_historical_concern(
            wake_text, state, recorded_at='2026-07-20 10:00:00',
        ))
        self.assertFalse(cr.is_superseded_historical_concern(
            wake_text, state, recorded_at='2026-07-22 10:00:00',
        ))

    def test_missing_time_fail_open(self):
        messages = [
            {'id': 1, 'content': '医生说不用打破伤风，伤口已经愈合。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想破伤风', state, recorded_at='',
        ))

    def test_deictic_resolution_uses_only_one_prior_message(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': '记得提醒我买花。', 'created_at': '2026-07-20 08:00:00'},
            {'id': 2, 'author': 'hayana', 'content': '猫抓的伤口还在疼。', 'created_at': '2026-07-20 09:00:00'},
            {'id': 3, 'author': 'hayana', 'content': '这件事结束了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        entry = state.active[0]
        self.assertTrue(any('伤口' in token for token in entry.topic_tokens))
        self.assertNotIn('买花', entry.topic_tokens)

    def test_express_resolution_does_not_absorb_distant_flower_topic(self):
        messages = [
            {'id': 1, 'content': '记得提醒我买花。', 'created_at': '2026-07-20 08:00:00'},
            {'id': 2, 'content': '快递已经取完，不用再跑了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想买花的事情。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想要不要提醒她买花。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_guard_only_for_applied_resolutions(self):
        state = cr.build_resolution_state([
            {'id': 1, 'content': '电影结束了。', 'created_at': '2026-07-21 10:00:00'},
        ])
        self.assertEqual(cr.format_resolution_guard([]), '')
        self.assertEqual(cr.format_resolution_guard(state.active), '')

    def test_positive_you_completion_is_resolution_not_reopen(self):
        self.assertTrue(cr.is_user_resolution('服务又修好了，可以正常用了。'))
        self.assertFalse(cr.is_user_reopen('服务又修好了，可以正常用了。'))
        self.assertTrue(cr.is_user_resolution('快递又送到了，不用再取了。'))
        self.assertFalse(cr.is_user_reopen('快递又送到了，不用再取了。'))

    def test_distinct_factory_lines_do_not_cross(self):
        messages = [
            {'id': 1, 'content': '一厂衬衫的进度已经跟完了，不用再催了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想二厂磁吸尾的进度。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想一厂衬衫的进度。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_distinct_orders_do_not_cross(self):
        messages = [
            {'id': 1, 'content': '订单a已经发货，不用再问了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想订单b有没有发货。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想订单a有没有发货。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_frontend_vs_frontend_gw_do_not_cross(self):
        messages = [
            {'id': 1, 'content': 'frontend 故障已经修好了，不用再看了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想 frontend-gw 故障要不要继续排查。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想 frontend 故障要不要继续排查。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_compound_sentence_last_reopen_wins(self):
        self.assertTrue(cr.is_user_reopen('昨天已经修好了，但今天又报错了。'))
        self.assertFalse(cr.is_user_resolution('昨天已经修好了，但今天又报错了。'))

    def test_compound_sentence_last_resolution_wins(self):
        self.assertTrue(cr.is_user_resolution('刚才又报错了，不过现在已经修好了。'))
        self.assertFalse(cr.is_user_reopen('刚才又报错了，不过现在已经修好了。'))

    def test_compound_sentence_doctor_then_new_problem_is_reopen(self):
        self.assertTrue(cr.is_user_reopen('医生之前说没事，但现在又出现问题。'))
        self.assertFalse(cr.is_user_resolution('医生之前说没事，但现在又出现问题。'))

    def test_compound_sentence_still_problem_now_resolved(self):
        self.assertTrue(cr.is_user_resolution('刚才仍有问题，现在已经解决。'))
        self.assertFalse(cr.is_user_reopen('刚才仍有问题，现在已经解决。'))

    def test_state_tokens_do_not_cross_distinct_orders(self):
        messages = [
            {'id': 1, 'content': '订单a已经完成，不用再问了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '订单b还是没完成。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_state_tokens_do_not_cross_frontend_variants(self):
        messages = [
            {'id': 1, 'content': 'frontend已经解决，不用再看了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            'frontend-gw还是没解决。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_state_tokens_do_not_cross_distinct_express_items(self):
        messages = [
            {'id': 1, 'content': '快递a已经送到，不用再问了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '快递b还是没送到。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_state_tokens_do_not_cross_distinct_services(self):
        messages = [
            {'id': 1, 'content': '服务a已经恢复，不用再排查了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '服务b还没恢复。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_question_suffix_does_not_block_factual_reopen(self):
        self.assertTrue(cr.is_user_reopen('服务又报错了，怎么办？'))
        self.assertFalse(cr.is_user_resolution('服务又报错了，怎么办？'))
        self.assertTrue(cr.is_user_reopen('伤口今天又恶化了，要不要处理？'))
        self.assertFalse(cr.is_user_resolution('伤口今天又恶化了，要不要处理？'))
        self.assertTrue(cr.is_user_reopen('服务又报错了怎么办？'))
        self.assertFalse(cr.is_user_resolution('服务又报错了怎么办？'))
        self.assertTrue(cr.is_user_reopen('伤口又恶化了要不要去看？'))
        self.assertTrue(cr.is_user_reopen('伤口恶化了要不要处理？'))
        self.assertIsNone(cr.classify_user_concern_event('服务又报错了吗？'))

    def test_pure_question_does_not_resolve(self):
        self.assertIsNone(cr.classify_user_concern_event('服务已经修好了吗？'))
        self.assertFalse(cr.is_user_resolution('服务已经修好了吗？'))

    def test_question_then_explicit_resolution(self):
        self.assertTrue(cr.is_user_resolution('医生说不用吗？后来确认不用了，这件事结束了。'))
        self.assertFalse(cr.is_user_reopen('医生说不用吗？后来确认不用了，这件事结束了。'))

    def test_state_end_position_not_regex_start(self):
        self.assertTrue(cr.is_user_resolution('已经出现问题，刚解决。'))
        self.assertFalse(cr.is_user_reopen('已经出现问题，刚解决。'))
        self.assertTrue(cr.is_user_resolution('刚才出现问题，后来解决了。'))
        self.assertFalse(cr.is_user_reopen('刚才出现问题，后来解决了。'))
        self.assertTrue(cr.is_user_reopen('刚才解决了，后来又出现问题。'))
        self.assertFalse(cr.is_user_resolution('刚才解决了，后来又出现问题。'))
        self.assertTrue(cr.is_user_reopen('现在修好了，但刚刚又报错了。'))
        self.assertFalse(cr.is_user_resolution('现在修好了，但刚刚又报错了。'))

    def test_repayment_entity_preserved_and_matched(self):
        messages = [
            {'id': 1, 'content': '还款已经完成，不用再问了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertTrue(cr.is_superseded_historical_concern(
            '还款仍未完成。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertFalse(cr.is_superseded_historical_concern(
            '退款仍未完成。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_bone_tail_order_identity_preserved(self):
        messages = [
            {'id': 1, 'content': '有骨有尾订单已完成，不用再催了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertTrue(any('有骨' in token or '有尾' in token for token in state.active[0].topic_tokens))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想有骨有尾订单进度。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_tail_payment_does_not_cross_intent_payment(self):
        messages = [
            {'id': 1, 'content': '尾款已经处理好了，不用再问了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '意向金还没处理好。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '尾款还没处理好。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_modifier_entity_still_matches(self):
        cases = [
            ('尾款已经处理好了，不用再问了。', '衬衫尾款还没付。', True),
            ('快递已经取完，不用再跑了。', '淘宝快递还没送到。', True),
            ('还款已经完成，不用再问了。', '本月还款仍未完成。', True),
        ]
        for resolution, wake, expected in cases:
            state = cr.build_resolution_state([
                {'id': 1, 'content': resolution, 'created_at': '2026-07-21 12:00:00'},
            ])
            self.assertEqual(
                cr.is_superseded_historical_concern(
                    wake, state, recorded_at='2026-07-20 18:00:00',
                ),
                expected,
                msg=f'{resolution} vs {wake}',
            )

    def test_exclusive_entity_variants_do_not_cross(self):
        cases = [
            ('有骨有尾订单已完成，不用再催了。', '还在想无骨有尾订单进度。'),
            ('有骨无尾订单已完成，不用再催了。', '还在想无骨无尾订单进度。'),
            ('一厂衬衫订单已经跟完了，不用再催了。', '还在想二厂衬衫订单进度。'),
            ('磁吸尾订单a已经发货，不用再问了。', '还在想磁吸尾订单b有没有发货。'),
        ]
        for resolution, wake in cases:
            state = cr.build_resolution_state([
                {'id': 1, 'content': resolution, 'created_at': '2026-07-21 12:00:00'},
            ])
            self.assertFalse(
                cr.is_superseded_historical_concern(
                    wake, state, recorded_at='2026-07-20 18:00:00',
                ),
                msg=f'{resolution} should not suppress {wake}',
            )

    def test_reopen_with_pronoun_uses_prior_context(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': 'frontend 故障已经修好了，不用再看了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'author': 'assistant', 'content': '好的，frontend 这边先结案。', 'created_at': '2026-07-21 10:05:00'},
            {'id': 3, 'author': 'hayana', 'content': '它又坏了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        self.assertEqual(len(state.active), 0)

    def test_reopen_deictic_and_temporal_use_prior_context(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': '服务故障已经修好了，可以正常用了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'author': 'hayana', 'content': '这件事又出问题了。', 'created_at': '2026-07-22 08:00:00'},
            {'id': 3, 'author': 'hayana', 'content': '昨天好了，今天又坏了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        self.assertEqual(len(state.active), 0)

    def test_assistant_message_does_not_trigger_reopen(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': 'frontend 故障已经修好了，不用再看了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'author': 'assistant', 'content': '它又坏了，要不要继续排查？', 'created_at': '2026-07-22 09:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        self.assertEqual(len(state.active), 1)

    def test_negative_unresolved_is_not_resolution(self):
        for text in (
            '尾款还没处理好。',
            '快递还没送到。',
            '订单还没完成。',
            '问题还没解决。',
        ):
            self.assertFalse(cr.is_user_resolution(text), msg=text)
            events = cr.parse_user_concern_events(text)
            self.assertEqual(len(events), 1, msg=text)
            self.assertEqual(events[0].event, 'unresolved', msg=text)

    def test_negative_unresolved_without_prior_closure_creates_none(self):
        state = cr.build_resolution_state([
            {'id': 1, 'content': '尾款还没处理好。', 'created_at': '2026-07-21 12:00:00'},
        ])
        self.assertEqual(len(state.active), 0)

    def test_negative_unresolved_reopens_existing_closure(self):
        messages = [
            {'id': 1, 'content': '尾款已经处理好了，不用再问了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '尾款还没处理好。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_multi_event_express_resolved_tail_unresolved(self):
        messages = [
            {'id': 1, 'content': '快递已经取完了，但尾款还没处理好。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertTrue(any('快递' in token for token in state.active[0].topic_tokens))
        self.assertFalse(any('尾款' in token for token in state.active[0].topic_tokens))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想快递有没有取。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想尾款有没有付。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_multi_event_frontend_vs_frontend_gw(self):
        messages = [
            {'id': 1, 'content': 'frontend 已经修好，但 frontend-gw 还在报错。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertIn('frontend', state.active[0].topic_tokens)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想 frontend-gw 故障要不要继续排查。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想 frontend 故障要不要继续排查。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_multi_event_order_a_unresolved_b_resolved(self):
        messages = [
            {'id': 1, 'content': '订单a还没完成，订单b已经完成。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertIn('订单b', state.active[0].topic_tokens)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想订单a有没有完成。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想订单b有没有完成。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_multi_event_order_a_resolved_b_unresolved(self):
        messages = [
            {'id': 1, 'content': '订单a已经完成，订单b还没完成。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertIn('订单a', state.active[0].topic_tokens)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想订单b有没有完成。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_multi_event_reopen_only_matching_closure(self):
        messages = [
            {'id': 1, 'content': '订单a已经完成，不用再问了。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '订单b已经完成，不用再问了。', 'created_at': '2026-07-20 11:00:00'},
            {'id': 3, 'content': '订单a仍正常，但订单b又坏了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertIn('订单a', state.active[0].topic_tokens)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想订单b有没有完成。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想订单a有没有完成。', state, recorded_at='2026-07-20 09:00:00',
        ))

    def test_same_message_pronoun_uses_prior_clause_not_chat(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': '今晚吃面。', 'created_at': '2026-07-21 08:00:00'},
            {'id': 2, 'author': 'assistant', 'content': '记得买牛奶。', 'created_at': '2026-07-21 08:30:00'},
            {'id': 3, 'author': 'hayana', 'content': 'frontend 已经修好，但它又报错了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        self.assertEqual(len(state.active), 0)
        events = cr.parse_user_concern_events(
            user_messages[-1]['content'], chat_messages, user_messages[-1]['id'],
        )
        self.assertEqual(events[-1].event, 'reopen')
        self.assertIn('frontend', events[-1].topics)
        self.assertNotIn('吃面', events[-1].topics)
        self.assertNotIn('牛奶', events[-1].topics)

    def test_same_message_pronoun_variants(self):
        cases = (
            ('快递已经取完了，但它又找不到了。', '快递'),
            ('服务已经恢复，不过这个又坏了。', '服务'),
        )
        for content, topic in cases:
            events = cr.parse_user_concern_events(content)
            self.assertEqual(len(events), 2, msg=content)
            self.assertEqual(events[0].event, 'resolve', msg=content)
            self.assertEqual(events[1].event, 'reopen', msg=content)
            self.assertIn(topic, events[0].topics, msg=content)
            self.assertIn(topic, events[1].topics, msg=content)

    def test_contrast_split_without_punctuation(self):
        cases = (
            ('快递已取完但尾款还没处理好', '快递', '尾款', 'resolve', 'unresolved'),
            ('frontend已修好但frontend-gw还在报错', 'frontend', 'frontend-gw', 'resolve', 'reopen'),
            ('订单a已完成但是订单b未完成', '订单a', '订单b', 'resolve', 'unresolved'),
        )
        for content, topic_a, topic_b, event_a, event_b in cases:
            events = cr.parse_user_concern_events(content)
            self.assertEqual(len(events), 2, msg=content)
            self.assertEqual(events[0].event, event_a, msg=content)
            self.assertEqual(events[1].event, event_b, msg=content)
            self.assertIn(topic_a, events[0].topics, msg=content)
            self.assertIn(topic_b, events[1].topics, msg=content)

    def test_contrast_split_same_concern_reopen_leaves_no_closure(self):
        state = cr.build_resolution_state([
            {'id': 1, 'content': '服务已修好但又报错了。', 'created_at': '2026-07-21 12:00:00'},
        ])
        self.assertEqual(len(state.active), 0)

    def test_hypothetical_reopen_does_not_deactivate_closure(self):
        messages = [
            {'id': 1, 'content': 'frontend 已经修好了，不用再看了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '如果又报错我再告诉你。', 'created_at': '2026-07-22 08:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertIn('frontend', state.active[0].topic_tokens)

    def test_negated_reopen_does_not_deactivate_closure(self):
        messages = [
            {'id': 1, 'content': 'frontend 已经修好了，不用再看了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '没有又报错，只是在复述之前的记录。', 'created_at': '2026-07-22 08:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)

    def test_factual_reopen_after_hypothetical(self):
        messages = [
            {'id': 1, 'content': 'frontend 已经修好了，不用再看了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '如果又报错我再告诉你。', 'created_at': '2026-07-22 08:00:00'},
            {'id': 3, 'content': '今天真的又报错了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_non_factual_reopen_phrases(self):
        cases = (
            '伤口已经好了，要是又红肿我会说。',
            '放心，不会又坏的。',
            '如果又报错怎么办？',
        )
        for text in cases:
            events = cr.parse_user_concern_events(text)
            self.assertFalse(
                any(event.event in {'reopen', 'unresolved'} for event in events),
                msg=text,
            )

    def test_entity_only_clause_antecedent_for_pronoun(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': '今晚吃面。', 'created_at': '2026-07-21 08:00:00'},
            {'id': 2, 'author': 'hayana', 'content': '这次说的是 frontend。它又报错了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        self.assertEqual(len(state.active), 0)
        events = cr.parse_user_concern_events(
            user_messages[-1]['content'], chat_messages, user_messages[-1]['id'],
        )
        self.assertEqual(events[-1].topics, frozenset({'frontend'}))

        chat_messages[1]['content'] = '关于淘宝快递。它又找不到了。'
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        events = cr.parse_user_concern_events(
            user_messages[-1]['content'], chat_messages, user_messages[-1]['id'],
        )
        self.assertIn('快递', events[-1].topics)
        self.assertNotIn('吃面', events[-1].topics)

    def test_non_factual_resolve_phrases(self):
        cases = (
            '如果 frontend 修好了我再告诉你。',
            '等快递送到了再说。',
            '希望伤口早点愈合。',
            '还没修好，等修好了再结案。',
        )
        for text in cases:
            events = cr.parse_user_concern_events(text)
            self.assertFalse(
                any(event.event == 'resolve' for event in events),
                msg=text,
            )

    def test_hope_then_factual_resolve(self):
        events = cr.parse_user_concern_events('本来希望它修好，今天确认已经修好了')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, 'resolve')

    def test_negated_reopen_with_subject_gap(self):
        cases = (
            '并不是它又报错了。',
            '我不是说 frontend 又坏了，只是在举例。',
            '没有发现服务又出现问题。',
        )
        for text in cases:
            events = cr.parse_user_concern_events(text)
            self.assertFalse(
                any(event.event in {'reopen', 'unresolved'} for event in events),
                msg=text,
            )

    def test_factual_reopen_after_subject_negation_contrast(self):
        events = cr.parse_user_concern_events('之前不是它报错，但今天 frontend 真的又坏了')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, 'reopen')
        self.assertIn('frontend', events[0].topics)

    def test_hypothetical_resolve_does_not_create_closure(self):
        messages = [
            {'id': 1, 'content': '如果修好了再说。', 'created_at': '2026-07-21 10:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_pronoun_binds_last_multi_order_topic(self):
        messages = [
            {'id': 1, 'content': '订单A已经完成，订单B还没完成。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '它又出问题了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertIn('订单a', state.active[0].topic_tokens)
        events = cr.parse_user_concern_events(
            messages[1]['content'], messages, messages[1]['id'],
        )
        self.assertEqual(events[-1].event, 'reopen')
        self.assertIn('订单b', events[-1].topics)
        self.assertNotIn('订单a', events[-1].topics)

    def test_pronoun_does_not_reopen_completed_express(self):
        messages = [
            {'id': 1, 'content': '快递已经取完了，但尾款还没处理好。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '它还是有问题。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertIn('快递', state.active[0].topic_tokens)
        topics = cr._collect_prior_chat_topics(messages, messages[1]['id'])
        self.assertIn('尾款', topics)
        self.assertNotIn('快递', topics)

    def test_bare_unresolved_statements_produce_events(self):
        cases = (
            ('没修好', 'unresolved'),
            ('没有处理好', 'unresolved'),
            ('没送到', 'unresolved'),
            ('还没有完成', 'unresolved'),
        )
        for text, expected in cases:
            events = cr.parse_user_concern_events(text)
            self.assertEqual(len(events), 1, msg=text)
            self.assertEqual(events[0].event, expected, msg=text)

    def test_fault_negation_still_blocks_reopen(self):
        self.assertEqual(cr.parse_user_concern_events('没有又报错'), [])

    def test_mixed_non_factual_factual_topic_isolation(self):
        cases = (
            (
                '希望 frontend 修好，今天确认 backend 已经修好了',
                'resolve',
                frozenset({'backend'}),
                frozenset({'frontend'}),
            ),
            (
                '如果订单A完成我再说，今天订单B已经完成',
                'resolve',
                frozenset({'今天订单b'}),
                frozenset({'如果订单a'}),
            ),
            (
                '希望快递送到，后来尾款已经处理好了',
                'resolve',
                frozenset({'尾款'}),
                frozenset({'快递', '希望快递'}),
            ),
        )
        for text, event_type, expected_topics, forbidden in cases:
            events = cr.parse_user_concern_events(text)
            self.assertEqual(len(events), 1, msg=text)
            self.assertEqual(events[0].event, event_type, msg=text)
            self.assertTrue(expected_topics <= events[0].topics, msg=text)
            self.assertFalse(events[0].topics & forbidden, msg=text)

    def test_hope_pronoun_then_factual_resolve_same_theme(self):
        messages = [
            {'id': 1, 'content': 'frontend 已经修好了，不用再看了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '本来希望它修好，今天确认它已经修好了', 'created_at': '2026-07-22 09:00:00'},
        ]
        events = cr.parse_user_concern_events(
            messages[1]['content'], messages, messages[1]['id'],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, 'resolve')
        self.assertIn('frontend', events[0].topics)

    def test_bare_unresolved_reopens_active_closure(self):
        messages = [
            {'id': 1, 'content': 'frontend 已经修好了，不用再看了。', 'created_at': '2026-07-21 10:00:00'},
            {'id': 2, 'content': '没修好', 'created_at': '2026-07-22 08:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_bare_unresolved_without_closure_creates_no_record(self):
        state = cr.build_resolution_state([
            {'id': 1, 'content': '没有处理好', 'created_at': '2026-07-21 10:00:00'},
        ])
        self.assertEqual(len(state.active), 0)

    def test_outer_negated_unresolved_does_not_produce_event(self):
        cases = (
            '并不是没修好',
            '不是没有处理好',
            '并非还没完成',
            '没有发现它还没送到',
        )
        for text in cases:
            self.assertEqual(cr.parse_user_concern_events(text), [], msg=text)

    def test_outer_negated_then_factual_resolve(self):
        events = cr.parse_user_concern_events('并不是没修好，后来确认已经修好了')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, 'resolve')

    def test_speaker_prefixed_wish_wait_do_not_resolve(self):
        cases = (
            '我希望 frontend 修好',
            '我在等快递送到再说',
            '我想等订单完成后再结案',
            '我只是希望伤口早点愈合',
        )
        for text in cases:
            self.assertFalse(
                any(event.event == 'resolve' for event in cr.parse_user_concern_events(text)),
                msg=text,
            )

    def test_speaker_prefixed_mixed_closure_isolation(self):
        cases = (
            (
                '我希望 frontend 修好，今天确认 backend 已经修好了',
                frozenset({'backend'}),
                frozenset({'frontend'}),
            ),
            (
                '我在等订单A完成，今天订单B已经完成',
                frozenset({'今天订单b'}),
                frozenset({'我在等订单a'}),
            ),
        )
        for text, expected_topics, forbidden in cases:
            events = cr.parse_user_concern_events(text)
            self.assertEqual(len(events), 1, msg=text)
            self.assertEqual(events[0].event, 'resolve', msg=text)
            self.assertTrue(expected_topics <= events[0].topics, msg=text)
            self.assertFalse(events[0].topics & forbidden, msg=text)

    def test_same_entity_hope_then_factual_resolve(self):
        events = cr.parse_user_concern_events(
            '我本来希望 frontend 修好，今天确认 frontend 已经修好了',
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, 'resolve')
        self.assertIn('frontend', events[0].topics)


class WakeConcernResolutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,
                layer TEXT, created_at TEXT, resolved INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 0
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE board (
                id INTEGER PRIMARY KEY, author TEXT, tag TEXT, content TEXT,
                level TEXT, category TEXT, status TEXT
            );
            CREATE TABLE dream_events (
                id INTEGER PRIMARY KEY, type TEXT, value TEXT, created_at TEXT,
                duration_minutes INTEGER
            );
            CREATE TABLE dream_pool (
                id INTEGER PRIMARY KEY, content TEXT, tone TEXT, surfaced INTEGER DEFAULT 0,
                surface_count INTEGER DEFAULT 0, created_at TEXT
            );
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY, amount REAL, category TEXT, date TEXT
            );
            CREATE TABLE ledger_budget (id INTEGER PRIMARY KEY, amount REAL, month TEXT);
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, content TEXT, thoughts TEXT,
                woke_at TEXT, consumed INTEGER DEFAULT 0
            );
            """
        )
        cr.ensure_concern_closure_schema(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _bjt_now(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    def _ts(self, *, hours_ago: float = 0.0) -> str:
        moment = self._bjt_now() - datetime.timedelta(hours=hours_ago)
        return moment.strftime('%Y-%m-%d %H:%M:%S')

    def _insert_user(self, content, *, hours_ago: float = 1.0, author: str = 'hayana'):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            (author, content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _insert_wake(self, action, content, *, hours_ago: float = 30.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO wake_log (action, content, thoughts, woke_at, consumed) VALUES (?,?,?,?,0)",
            (action, content, '', self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _insert_diary(self, content, *, hours_ago: float = 20.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO posts (type, content, layer, resolved, created_at) VALUES ('DIARY', ?, 'recent', 0, ?)",
            (content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _insert_assistant(self, content, *, hours_ago: float = 1.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('assistant', content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _age_out_resolution_messages(self, *, hours_ago: float = 200.0):
        conn = self.get_db()
        conn.execute(
            """
            UPDATE chat_messages
            SET created_at=?
            WHERE author IN ('hayana', 'haya', 'user')
              AND (
                content LIKE '%不用再%'
                OR content LIKE '%已经%好了%'
                OR content LIKE '%已经取完%'
                OR content LIKE '%可以放下%'
                OR content LIKE '%结束了%'
                OR content LIKE '%不用打针%'
                OR content LIKE '%不用打破伤风%'
              )
            """,
            (self._ts(hours_ago=hours_ago),),
        )
        conn.commit()
        conn.close()

    def _patch_build(self):
        from chat import system_builder
        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        return mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
            mock.patch.object(system_builder, 'build_shared_context', return_value=None), \
            mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
            mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
            mock.patch.object(system_builder.config_store, 'get_bool', return_value=False)

    def _build_wake_text(self, *, wake: bool = True):
        from chat import system_builder
        patches = self._patch_build()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            blocks = system_builder.build_system(wake=wake)
        return '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))

    def test_old_wake_suppressed_after_user_resolution(self):
        self._insert_wake('explore', '还在想她需不需要打破伤风针。', hours_ago=30)
        self._insert_user('猫抓的伤口已经愈合，医生说不必打破伤风，不用再问了。')

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_multiple_unconsumed_wakes_still_suppressed(self):
        self._insert_wake('explore', '破伤风要不要打', hours_ago=60)
        self._insert_wake('diary', '日记里还在担心破伤风', hours_ago=58)
        self._insert_wake('none', '醒来仍觉得破伤风悬而未决', hours_ago=56)
        self._insert_user('医生说破伤风不用打针，伤口已经好了。')

        text = self._build_wake_text()
        self.assertNotIn('你醒着的时候', text)

    def test_flower_wake_survives_unrelated_express_resolution(self):
        self._insert_user('记得提醒我买花。', hours_ago=30)
        self._insert_wake('explore', '还在想买花的事情。', hours_ago=20)
        self._insert_user('快递已经取完，不用再跑了。')

        text = self._build_wake_text()
        self.assertIn('买花', text)
        self.assertIn('你醒着的时候', text)
        self.assertNotIn('用户已明确结案', text)

    def test_flower_reminder_wake_survives_express_processed_ok(self):
        self._insert_user('记得提醒我买花。', hours_ago=30)
        self._insert_wake('explore', '还在想要不要提醒她买花。', hours_ago=20)
        self._insert_user('快递已经处理好了。')

        text = self._build_wake_text()
        self.assertIn('买花', text)
        self.assertIn('你醒着的时候', text)
        self.assertNotIn('用户已明确结案', text)

    def test_post_resolution_wake_not_filtered(self):
        self._insert_user('医生说不用打针，伤口已经好了。', hours_ago=48)
        self._insert_wake('explore', '今天伤口开始渗液了，要不要再问医生。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('渗液', text)

    def test_post_resolution_diary_not_filtered(self):
        self._insert_user('医生说破伤风不用打针，伤口已经好了。', hours_ago=48)
        self._insert_diary('今天伤口开始红肿，要不要再问医生。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('最近的日记', text)
        self.assertIn('红肿', text)

    def test_reopen_allows_wake_concern_back(self):
        self._insert_wake('explore', '还在想她需不需要打破伤风针。', hours_ago=50)
        self._insert_user('医生说不用打针，伤口已经好了。', hours_ago=48)
        self._insert_user('伤口又红肿发炎了，还是得去医院看看。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('破伤风', text)

    def test_reopen_without_repeat_words(self):
        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=50)
        self._insert_user('医生说不用打针，伤口已经好了。', hours_ago=48)
        self._insert_user('伤口今天开始红肿了。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)

    def test_normal_wake_actions_unaffected_without_resolution(self):
        self._insert_wake('explore', '想给她写一首小诗。')
        self._insert_wake('diary', '今天月色很好。')
        self._insert_wake('none', '决定先不打扰她。')

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertNotIn('用户已明确结案', text)

    def test_movie_ended_does_not_emit_guard_in_chat(self):
        self._insert_user('电影结束了，挺好看的。')
        self._insert_wake('explore', '想给她写一首小诗。')

        text = self._build_wake_text(wake=False)
        self.assertNotIn('用户已明确结案', text)
        self.assertIn('你醒着的时候', text)

    def test_multi_topic_resolution_only_affects_matching_wake(self):
        self._insert_wake('explore', '破伤风要不要打', hours_ago=40)
        self._insert_wake('explore', '记得提醒她买花。', hours_ago=35)
        self._insert_user('医生说破伤风不用打针，伤口已经好了。', hours_ago=10)

        text = self._build_wake_text()
        self.assertIn('买花', text)
        self.assertNotIn('破伤风要不要打', text)

    def test_superseded_diary_filtered_in_wake_prompt(self):
        self._insert_diary('日记：还在担心破伤风要不要打。', hours_ago=30)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。')

        text = self._build_wake_text()
        self.assertNotIn('最近的日记', text)

    def test_wake_log_consumed_semantics_unchanged(self):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO wake_log (id, action, content, consumed, woke_at) VALUES (1, 'explore', '破伤风', 0, ?)",
            (self._ts(hours_ago=30),),
        )
        conn.execute(
            "INSERT INTO wake_log (id, action, content, consumed, woke_at) VALUES (2, 'explore', '买花', 0, ?)",
            (self._ts(hours_ago=30),),
        )
        conn.commit()
        conn.close()
        self._insert_user('医生说破伤风不用打针，伤口已经好了。')

        ids = context_continuity.capture_pending_wake_ids(self.get_db)
        self.assertEqual(ids, [1, 2])

        consumed = context_continuity.consume_wake_ids(self.get_db, ids)
        self.assertEqual(consumed, 2)

    def test_cc_one_shot_filters_superseded_wake_items(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=24)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。')

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            one_shot = system_builder.build_cc_one_shot(include_wake=True)

        self.assertEqual(one_shot.get('wake_items'), [])
        self.assertEqual(one_shot.get('suppressed_wake_ids'), [1])
        self.assertIn('用户已明确结案', system_builder.format_one_shot(one_shot))

    def test_cc_suppressed_wake_ids_are_consumable(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=24)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。')

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            one_shot = system_builder.build_cc_one_shot(include_wake=True)

        self.assertEqual(one_shot.get('wake_ids'), [1])
        consumed = context_continuity.consume_wake_ids(self.get_db, one_shot['wake_ids'])
        self.assertEqual(consumed, 1)

    def test_consumed_suppressed_wake_does_not_revive_after_resolution_window(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=24)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。', hours_ago=1)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            one_shot = system_builder.build_cc_one_shot(include_wake=True)
            self.assertEqual(one_shot.get('wake_items'), [])
            self.assertEqual(one_shot.get('suppressed_wake_ids'), [1])
            context_continuity.consume_wake_ids(self.get_db, one_shot['wake_ids'])

            conn = self.get_db()
            conn.execute(
                "UPDATE chat_messages SET created_at=? WHERE id=1",
                (self._ts(hours_ago=200),),
            )
            conn.commit()
            conn.close()

            revived = system_builder.build_cc_one_shot(include_wake=True)

        self.assertEqual(revived.get('wake_items'), [])
        self.assertEqual(revived.get('suppressed_wake_ids'), [])
        self.assertEqual(revived.get('wake_resolution_guard'), '')
        conn = self.get_db()
        pending = conn.execute(
            "SELECT COUNT(*) FROM wake_log WHERE consumed=0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(pending, 0)

    def test_load_resolution_state_from_db_closes_on_error(self):
        closed = {'value': False}

        class LeakConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError('boom')

            def close(self):
                closed['value'] = True

        def get_db():
            return LeakConn()

        try:
            cr.load_resolution_state_from_db(get_db)
        except sqlite3.OperationalError:
            pass
        self.assertTrue(closed['value'])

    def test_tetanus_regression_case(self):
        self._insert_wake(
            'explore',
            '她之前被猫抓伤，我还在想破伤风针要不要打。',
            hours_ago=72,
        )
        self._insert_wake(
            'diary',
            '日记：猫抓伤口和破伤风仍让我放不下。',
            hours_ago=70,
        )
        self._insert_diary('夜里仍惦记猫抓伤和破伤风要不要打。', hours_ago=68)
        self._insert_user(
            '已经问过医生了，伤口早就愈合，不用打破伤风，这件事结束了。',
            hours_ago=6,
        )

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)
        self.assertNotIn('最近的日记', text)

    def test_concern_closures_persist_in_database(self):
        self._insert_user('快递已经取完，不用再跑了。')
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(row, 1)

    def test_closure_persists_beyond_168h_wake_and_diary(self):
        self._insert_diary('还在担心快递有没有取。', hours_ago=200)
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=200)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)
        self.assertNotIn('最近的日记', text)

        self._age_out_resolution_messages(hours_ago=200)

        text_after = self._build_wake_text()
        self.assertNotIn('你醒着的时候', text_after)
        self.assertNotIn('最近的日记', text_after)
        self.assertIn('用户已明确结案', text_after)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 1)

    def test_reopen_after_168h_allows_matching_wake_back(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=200)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=2)
        self._build_wake_text()
        self._age_out_resolution_messages(hours_ago=200)
        self._insert_user('快递又找不到了，还得再去拿。', hours_ago=0.5)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('快递', text)

    def test_express_delivery_wake_suppressed(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=20)
        self._insert_user('快递已经取完，不用再跑了。')

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_factory_progress_wake_suppressed(self):
        self._insert_wake('explore', '还在想工厂订单的进度。', hours_ago=20)
        self._insert_user('工厂订单的进度已经跟完了，不用再催了。')

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_software_fault_reopen_shows_wake_again(self):
        self._insert_wake('explore', '还在想服务故障要不要继续排查。', hours_ago=50)
        self._insert_user('服务故障已经修好了，可以正常用了。', hours_ago=48)
        self._insert_user('服务故障又报错了，还得排查。', hours_ago=1)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('服务故障', text)

    def test_adjacent_unrelated_wake_only_partially_suppressed(self):
        self._insert_wake('explore', '还在想工厂订单进度。', hours_ago=30)
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=28)
        self._insert_user('快递已经取完，不用再跑了。')

        text = self._build_wake_text()
        self.assertIn('工厂', text)
        self.assertIn('你醒着的时候', text)
        self.assertNotIn('快递有没有取', text)

    def test_deictic_resolution_across_assistant_message(self):
        self._insert_user('提醒我买花。', hours_ago=10)
        self._insert_assistant('好的，我记下了。', hours_ago=9)
        self._insert_assistant('还担心你的猫抓伤。', hours_ago=8)
        self._insert_wake('explore', '还在想猫抓伤要不要打破伤风。', hours_ago=7)
        self._insert_user('这件事结束了。', hours_ago=1)

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('破伤风', text)
        self.assertNotIn('你醒着的时候', text)

    def test_cold_once_excludes_resolution_guard(self):
        from chat import system_builder

        self._insert_diary('还在担心快递有没有取。', hours_ago=20)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=1)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            cold = system_builder._cc_collect_cold_once(self.get_db)

        self.assertNotIn('用户已明确结案', cold.get('long_term_memory', ''))
        self.assertNotIn('最近的日记', cold.get('long_term_memory', ''))

    def test_cold_resolved_then_reopen_drops_stale_guard_in_one_shot(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=30)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=2)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            cold = system_builder._cc_collect_cold_once(self.get_db)
            self.assertNotIn('用户已明确结案', cold.get('long_term_memory', ''))

            one_shot = system_builder.build_cc_one_shot(include_wake=True)
            self.assertIn('用户已明确结案', system_builder.format_one_shot(one_shot))
            self.assertEqual(one_shot.get('wake_items'), [])

            self._insert_user('快递又找不到了，还得再去拿。', hours_ago=0.2)
            reopened = system_builder.build_cc_one_shot(include_wake=True)

        formatted = system_builder.format_one_shot(reopened)
        self.assertNotIn('用户已明确结案', formatted)
        self.assertEqual(len(reopened.get('wake_items') or []), 1)
        self.assertIn('快递', formatted)

    def test_finalize_cc_wake_one_shot_fail_open_on_merge_error(self):
        from chat import system_builder

        hot = {
            'wake_items': [
                {'id': 9, 'action': 'explore', 'content': '还在想快递。', 'woke_at': '2026-07-20 10:00:00'},
            ],
            'suppressed_wake_ids': [10],
        }
        with mock.patch('wake.concern_resolution.merge_wake_consume_ids', side_effect=RuntimeError('boom')):
            result = system_builder.finalize_cc_wake_one_shot(hot, is_cold=False)
        self.assertEqual(result['wake_ids'], [9])

    def _insert_user_at(self, content, *, message_id: int, hours_ago: float = 1.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (id, author, content, created_at) VALUES (?,?,?,?)",
            (message_id, 'hayana', content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def test_sync_idempotent_resolve_reopen_resolve(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=30)
        self._insert_user_at('快递已经取完，不用再跑了。', message_id=1, hours_ago=20)
        self._insert_user_at('快递又找不到了，还得再去拿。', message_id=2, hours_ago=10)
        self._insert_user_at('快递已经取到了，不用再跑了。', message_id=3, hours_ago=1)

        states = [cr.load_resolution_state_from_db(self.get_db) for _ in range(3)]
        for state in states:
            self.assertEqual(len(state.active), 1)
            self.assertIn('取到', state.active[0].summary)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        cursor = conn.execute(
            "SELECT last_processed_message_id FROM concern_closure_sync WHERE id=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 1)
        self.assertEqual(cursor, 3)

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_sync_state_survives_service_restart(self):
        self._insert_user('快递已经取完，不用再跑了。')
        first = cr.load_resolution_state_from_db(self.get_db)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        second = cr.load_resolution_state(conn)
        conn.close()

        self.assertEqual(len(first.active), len(second.active))
        self.assertEqual(first.active[0].summary, second.active[0].summary)

    def test_cold_once_and_one_shot_share_persistent_state(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=20)
        self._insert_diary('还在担心快递有没有取。', hours_ago=18)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=1)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            for _ in range(3):
                cr.load_resolution_state_from_db(self.get_db)
            cold = system_builder._cc_collect_cold_once(self.get_db)
            one_shot = system_builder.build_cc_one_shot(include_wake=True)

        self.assertNotIn('最近的日记', cold.get('long_term_memory', ''))
        self.assertNotIn('用户已明确结案', cold.get('long_term_memory', ''))
        self.assertEqual(one_shot.get('wake_items'), [])
        self.assertIn('用户已明确结案', system_builder.format_one_shot(one_shot))

    def test_concurrent_sync_preserves_final_resolution(self):
        self._insert_user_at('快递已经取完，不用再跑了。', message_id=1, hours_ago=30)
        self._insert_user_at('快递又找不到了，还得再去拿。', message_id=2, hours_ago=20)
        self._insert_user_at('快递已经取到了，不用再跑了。', message_id=3, hours_ago=10)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker():
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                barrier.wait(timeout=5)
                cr.sync_concern_closures(conn)
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        cursor = conn.execute(
            "SELECT last_processed_message_id FROM concern_closure_sync WHERE id=1"
        ).fetchone()[0]
        summary = conn.execute(
            "SELECT summary FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(active, 1)
        self.assertEqual(cursor, 3)
        self.assertIn('取到', summary)

        state = cr.load_active_closure_state(self.get_db())
        self.assertEqual(len(state.active), 1)
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想快递有没有取。', state, recorded_at='2026-07-20 11:00:00',
        ))
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想订单b还是没完成。', state, recorded_at='2026-07-20 11:00:00',
        ))

    def _insert_chat_at(self, *, message_id: int, author: str, content: str, hours_ago: float = 1.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (id, author, content, created_at) VALUES (?,?,?,?)",
            (message_id, author, content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def test_db_reopen_pronoun_closes_frontend_closure_and_revives_wake(self):
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=50)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='frontend 故障已经修好了，不用再看了。',
            hours_ago=40,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        active_before = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active_before, 1)

        text_before = self._build_wake_text()
        self.assertIn('用户已明确结案', text_before)
        self.assertNotIn('你醒着的时候', text_before)

        self._insert_chat_at(
            message_id=2, author='assistant',
            content='好的，frontend 这边先结案。',
            hours_ago=5,
        )
        self._insert_chat_at(
            message_id=3, author='hayana',
            content='它又坏了。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        active_after = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        reopened = conn.execute(
            "SELECT reopened_at FROM concern_closures WHERE source_message_id=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active_after, 0)
        self.assertIsNotNone(reopened)

        text_after = self._build_wake_text()
        self.assertNotIn('用户已明确结案', text_after)
        self.assertIn('你醒着的时候', text_after)
        self.assertIn('frontend', text_after)

    def test_db_reopen_deictic_and_temporal_lifecycle(self):
        self._insert_wake('explore', '还在想服务故障要不要继续排查。', hours_ago=60)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='服务故障已经修好了，可以正常用了。',
            hours_ago=50,
        )
        self._insert_chat_at(
            message_id=2, author='hayana',
            content='这件事又出问题了。',
            hours_ago=20,
        )
        self._insert_chat_at(
            message_id=3, author='hayana',
            content='昨天好了，今天又坏了。',
            hours_ago=10,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 0)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('服务故障', text)

    def test_db_assistant_message_does_not_trigger_reopen(self):
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=40)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='frontend 故障已经修好了，不用再看了。',
            hours_ago=30,
        )
        self._insert_chat_at(
            message_id=2, author='assistant',
            content='它又坏了，要不要继续排查？',
            hours_ago=5,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 1)

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_db_multi_event_express_only_closure(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=30)
        self._insert_wake('explore', '还在想尾款有没有付。', hours_ago=28)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='快递已经取完了，但尾款还没处理好。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        rows = conn.execute(
            "SELECT topic_tokens, active FROM concern_closures"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 1)
        self.assertIn('快递', rows[0][0])
        self.assertNotIn('尾款', rows[0][0])

        text = self._build_wake_text()
        self.assertNotIn('快递有没有取', text)
        self.assertIn('尾款', text)

    def test_db_multi_event_frontend_gw_wake_survives(self):
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=30)
        self._insert_wake('explore', '还在想 frontend-gw 故障要不要继续排查。', hours_ago=28)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='frontend 已经修好，但 frontend-gw 还在报错。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        text = self._build_wake_text()
        self.assertNotIn('frontend 故障', text)
        self.assertIn('frontend-gw', text)

    def test_db_negative_unresolved_reopens_without_creating(self):
        self._insert_wake('explore', '还在想尾款有没有付。', hours_ago=30)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='尾款已经处理好了，不用再问了。',
            hours_ago=20,
        )
        cr.load_resolution_state_from_db(self.get_db)

        self._insert_chat_at(
            message_id=2, author='hayana',
            content='尾款还没处理好。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 0)

        text = self._build_wake_text()
        self.assertIn('尾款', text)

    def test_db_multi_event_reopen_only_order_b(self):
        self._insert_wake('explore', '还在想订单a有没有完成。', hours_ago=40)
        self._insert_wake('explore', '还在想订单b有没有完成。', hours_ago=38)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='订单a已经完成，不用再问了。',
            hours_ago=30,
        )
        self._insert_chat_at(
            message_id=2, author='hayana',
            content='订单b已经完成，不用再问了。',
            hours_ago=20,
        )
        cr.load_resolution_state_from_db(self.get_db)
        self._insert_chat_at(
            message_id=3, author='hayana',
            content='订单a仍正常，但订单b又坏了。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        active_rows = conn.execute(
            "SELECT topic_tokens FROM concern_closures WHERE active=1"
        ).fetchall()
        conn.close()
        self.assertEqual(len(active_rows), 1)
        self.assertIn('订单a', active_rows[0][0])

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertIn('还在想订单b', text)
        self.assertNotIn('还在想订单a', text)

    def test_db_same_message_pronoun_reopens_frontend_closure(self):
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='今晚吃面。',
            hours_ago=50,
        )
        self._insert_chat_at(
            message_id=2, author='assistant',
            content='记得买牛奶。',
            hours_ago=40,
        )
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=30)
        self._insert_chat_at(
            message_id=3, author='hayana',
            content='frontend 已经修好，但它又报错了。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 0)

        text = self._build_wake_text()
        self.assertNotIn('用户已明确结案', text)
        self.assertIn('你醒着的时候', text)
        self.assertIn('frontend', text)

    def test_db_contrast_split_without_punctuation(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=30)
        self._insert_wake('explore', '还在想尾款有没有付。', hours_ago=28)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='快递已取完但尾款还没处理好。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        rows = conn.execute(
            "SELECT topic_tokens, active FROM concern_closures"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertIn('快递', rows[0][0])
        self.assertNotIn('尾款', rows[0][0])

        text = self._build_wake_text()
        self.assertNotIn('快递有没有取', text)
        self.assertIn('尾款', text)

    def test_db_hypothetical_then_factual_reopen_lifecycle(self):
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='今晚吃面。',
            hours_ago=60,
        )
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=40)
        self._insert_chat_at(
            message_id=2, author='hayana',
            content='frontend 已经修好了，不用再看了。',
            hours_ago=30,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        self._insert_chat_at(
            message_id=3, author='hayana',
            content='如果又报错我再告诉你。',
            hours_ago=10,
        )
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        self._insert_chat_at(
            message_id=4, author='hayana',
            content='没有又报错，只是在复述。',
            hours_ago=5,
        )
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        text_before = self._build_wake_text()
        self.assertNotIn('你醒着的时候', text_before)

        self._insert_chat_at(
            message_id=5, author='hayana',
            content='今天真的又报错了。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 0)
        conn.close()

        text_after = self._build_wake_text()
        self.assertIn('你醒着的时候', text_after)
        self.assertIn('frontend', text_after)

    def test_db_entity_only_clause_antecedent_reopens(self):
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='今晚吃面。',
            hours_ago=40,
        )
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=30)
        self._insert_chat_at(
            message_id=2, author='hayana',
            content='frontend 已经修好了，不用再看了。',
            hours_ago=20,
        )
        cr.load_resolution_state_from_db(self.get_db)
        self._insert_chat_at(
            message_id=3, author='hayana',
            content='这次说的是 frontend。它又报错了。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 0)
        conn.close()

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('frontend', text)

    def test_db_hypothetical_resolve_then_factual_closure_lifecycle(self):
        self._insert_wake('explore', '还在想快递有没有送到。', hours_ago=40)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='快递还没送到。',
            hours_ago=35,
        )
        self._insert_chat_at(
            message_id=2, author='hayana',
            content='如果修好了再说。',
            hours_ago=30,
        )
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 0)
        conn.close()

        self._insert_chat_at(
            message_id=3, author='hayana',
            content='等送到了再说。',
            hours_ago=20,
        )
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 0)
        conn.close()

        text_before = self._build_wake_text()
        self.assertIn('快递有没有送到', text_before)

        self._insert_chat_at(
            message_id=4, author='hayana',
            content='今天确认已经送到了。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        text_after = self._build_wake_text()
        self.assertNotIn('你醒着的时候', text_after)

    def test_db_pronoun_reopens_last_not_first_order(self):
        self._insert_wake('explore', '还在想订单A进度。', hours_ago=40)
        self._insert_wake('explore', '还在想订单B进度。', hours_ago=38)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='订单A已经完成，订单B还没完成。',
            hours_ago=20,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        row = conn.execute(
            "SELECT topic_tokens FROM concern_closures WHERE active=1"
        ).fetchone()
        conn.close()
        self.assertIn('订单a', row[0].lower())

        self._insert_chat_at(
            message_id=2, author='hayana',
            content='它又出问题了。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        text = self._build_wake_text()
        self.assertNotIn('订单A进度', text)
        self.assertIn('订单B进度', text)

    def test_db_express_pronoun_does_not_reopen_completed_item(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=40)
        self._insert_wake('explore', '还在想尾款有没有付。', hours_ago=38)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='快递已经取完了，但尾款还没处理好。',
            hours_ago=20,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        row = conn.execute(
            "SELECT topic_tokens FROM concern_closures WHERE active=1"
        ).fetchone()
        conn.close()
        self.assertIn('快递', row[0])

        self._insert_chat_at(
            message_id=2, author='hayana',
            content='它还是有问题。',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        text = self._build_wake_text()
        self.assertNotIn('快递有没有取', text)
        self.assertIn('尾款', text)

    def test_db_bare_unresolved_reopens_closure_but_fault_negation_does_not(self):
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=40)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='frontend 已经修好了，不用再看了。',
            hours_ago=30,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        self._insert_chat_at(
            message_id=2, author='hayana',
            content='没修好',
            hours_ago=10,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 0)
        conn.close()

        text_after_unresolved = self._build_wake_text()
        self.assertIn('你醒着的时候', text_after_unresolved)
        self.assertIn('frontend', text_after_unresolved)

        self._insert_chat_at(
            message_id=3, author='hayana',
            content='frontend 已经修好了，不用再看了。',
            hours_ago=8,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        self._insert_chat_at(
            message_id=4, author='hayana',
            content='没有又报错',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

    def test_db_bare_unresolved_without_closure_creates_no_row(self):
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='没有处理好',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures"
        ).fetchone()[0], 0)
        conn.close()

    def test_db_outer_negated_unresolved_preserves_closure(self):
        self._insert_wake('explore', '还在想 frontend 故障要不要继续排查。', hours_ago=40)
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='frontend 已经修好了，不用再看了。',
            hours_ago=30,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        self._insert_chat_at(
            message_id=2, author='hayana',
            content='并不是没修好',
            hours_ago=10,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 1)
        conn.close()

        self._insert_chat_at(
            message_id=3, author='hayana',
            content='没修好',
            hours_ago=5,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0], 0)
        conn.close()

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('frontend', text)

    def test_db_outer_negated_without_closure_creates_no_row(self):
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='并不是没修好',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures"
        ).fetchone()[0], 0)
        conn.close()

    def test_db_bare_unresolved_without_active_closure_creates_no_row(self):
        self._insert_chat_at(
            message_id=1, author='hayana',
            content='没修好',
            hours_ago=1,
        )
        cr.load_resolution_state_from_db(self.get_db)

        conn = self.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_closures"
        ).fetchone()[0], 0)
        conn.close()


if __name__ == '__main__':
    unittest.main()
