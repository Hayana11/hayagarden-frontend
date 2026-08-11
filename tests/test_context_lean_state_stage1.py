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
    assemble_legacy_full_fallback,
    compute_state_version,
    evaluate_reanchor_reason,
    format_structured_state_delta,
    format_structured_state_anchor,
)
from chat.system_builder import (
    _cc_collect_state,
    format_state_diff,
    format_state_snapshot,
    lean_system_field_is_facts_only,
)
from cc_resident import ResidentSession


# Frozen legacy fixture (representative production-shaped strings).
LEGACY_RAW = normalize_state_dict({
    'emotion': '## 此刻的情绪与欲望\n情绪：V0.60/A0.30 — 平静\nPA 0.50 | NA 0.20 | 混合张力',
    'drive': '驱动：想她 中等（0.55）',
    'lights': '（灯·当前状态：主灯 关，床头灯 关）',
    'reminders': '## 今日提醒\n- 修改 AI 的语气，让表达更克制',
})

RAW_V1 = normalize_state_dict({
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


class FlagZeroSemanticTests(unittest.TestCase):
    """Provider-visible state uses persona semantic layer (not raw formatters)."""

    def test_cold_snapshot_uses_persona_header(self):
        resident = _resident_stub()
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=True,
            user_text='你好',
            resident=resident,
            lean_on=False,
        )
        self.assertIn('【此刻的感受】', result.state_text)
        self.assertFalse(result.used_lean)

    def test_hot_diff_uses_persona_delta_header(self):
        before = dict(RAW_V1)
        after = dict(RAW_V1)
        after['lights'] = 'main=开 bedside=关'
        resident = _resident_stub(last_state_snapshot=before)
        result = assemble_cc_state_context(
            raw_state=after,
            is_cold=False,
            user_text='灯还亮吗',
            resident=resident,
            lean_on=False,
        )
        if result.state_text:
            self.assertIn('变化', result.state_text)
        self.assertFalse(result.used_lean)

    def test_hot_unchanged_empty_diff(self):
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
        anchor_v = 'anchor-hash'
        prev_v = compute_state_version(RAW_V1)
        curr_v = compute_state_version(merge_cumulative_state_send(RAW_V1, send))
        text = format_structured_state_delta(
            RAW_V1,
            send,
            anchor_version=anchor_v,
            previous_version=prev_v,
            current_version=curr_v,
        )
        self.assertIn('anchor_version=anchor-hash', text)
        self.assertIn(f'previous_version={prev_v}', text)
        self.assertIn(f'current_version={curr_v}', text)
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
        self.assertEqual(result.observation['changed_field_count'], len(result.send_payload))
        self.assertIn('lights', result.send_payload)
        self.assertEqual(result.send_payload['lights'], '')
        # Persona layer omits empty tombstones; raw tombstone remains in send_payload.
        self.assertEqual(result.state_context_mode, 'omitted')
        self.assertEqual(result.state_text, '')
        expected_after = merge_cumulative_state_send(RAW_V1, {'lights': ''})
        self.assertEqual(
            result.observation['state_version'],
            compute_state_version(expected_after),
        )


class StateVersionTests(unittest.TestCase):
    def test_hot_delta_version_hashes_cumulative_state_after_merge(self):
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
        expected = merge_cumulative_state_send(RAW_V1, result.send_payload)
        self.assertEqual(result.observation['state_version'], compute_state_version(expected))

    def test_same_delta_on_different_anchors_has_different_state_version(self):
        anchor_a = dict(RAW_V1)
        anchor_b = dict(RAW_V1)
        anchor_b['emotion'] = 'valence=0.90 arousal=0.10 mood=兴奋 pa=0.80 na=0.10 longing=0.10 desire_p=0.50 desire_i=0.40 desire_c=0.60'
        lights_delta = {'lights': '（灯·主灯 开）'}
        changed_a = dict(anchor_a)
        changed_a['lights'] = lights_delta['lights']
        changed_b = dict(anchor_b)
        changed_b['lights'] = lights_delta['lights']

        res_a = assemble_cc_state_context(
            raw_state=changed_a,
            is_cold=False,
            user_text='无关',
            resident=_resident_stub(
                last_successful_lean_state=True,
                last_state_snapshot=anchor_a,
                last_state_send_snapshot=anchor_a,
                last_state_anchor_generation=1,
                generation=1,
                last_state_schema_version=STATE_SCHEMA_VERSION,
            ),
            lean_on=True,
        )
        res_b = assemble_cc_state_context(
            raw_state=changed_b,
            is_cold=False,
            user_text='无关',
            resident=_resident_stub(
                last_successful_lean_state=True,
                last_state_snapshot=anchor_b,
                last_state_send_snapshot=anchor_b,
                last_state_anchor_generation=1,
                generation=1,
                last_state_schema_version=STATE_SCHEMA_VERSION,
            ),
            lean_on=True,
        )
        self.assertNotEqual(
            res_a.observation['state_version'],
            res_b.observation['state_version'],
        )

    def test_omitted_turn_preserves_cumulative_state_version(self):
        resident = _resident_stub(
            last_successful_lean_state=True,
            last_state_snapshot=RAW_V1,
            last_state_send_snapshot=RAW_V1,
            last_state_anchor_generation=1,
            last_state_schema_version=STATE_SCHEMA_VERSION,
        )
        expected_version = compute_state_version(RAW_V1)
        result = assemble_cc_state_context(
            raw_state=RAW_V1,
            is_cold=False,
            user_text='继续',
            resident=resident,
            lean_on=True,
        )
        self.assertEqual(result.state_context_mode, 'omitted')
        self.assertEqual(result.observation['state_version'], expected_version)

    def test_tombstone_version_hashes_state_after_field_removal(self):
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
        expected_after = merge_cumulative_state_send(RAW_V1, {'lights': ''})
        self.assertEqual(
            result.observation['state_version'],
            compute_state_version(expected_after),
        )
        self.assertNotIn('lights', expected_after)


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
        self.assertIn('【此刻的感受】', result.state_text)
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
            'previous_version',
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


class ConsecutiveDeltaVersionChainTests(unittest.TestCase):
    @staticmethod
    def _commit(sess: ResidentSession, result, raw_state):
        meta = {
            'state_snapshot': copy.deepcopy(raw_state),
            **result.commit_meta_extras,
        }
        sess._commit_sent_context(meta)

    def test_two_consecutive_deltas_link_previous_version(self):
        sess = ResidentSession('/tmp', '', '')
        raw = dict(RAW_V1)

        anchor = assemble_cc_state_context(
            raw_state=raw,
            is_cold=True,
            user_text='你好',
            resident=sess,
            lean_on=True,
        )
        self.assertEqual(anchor.state_context_mode, 'full_anchor')
        anchor_version = anchor.observation['state_version']
        self._commit(sess, anchor, raw)

        raw_after_delta1 = dict(raw)
        raw_after_delta1['lights'] = 'main=on bedside=off'
        delta1 = assemble_cc_state_context(
            raw_state=raw_after_delta1,
            is_cold=False,
            user_text='开灯',
            resident=sess,
            lean_on=True,
        )
        self.assertEqual(delta1.state_context_mode, 'delta')
        delta1_current = delta1.observation['state_version']
        self.assertEqual(delta1.observation['anchor_version'], anchor_version)
        self.assertEqual(
            delta1.observation['previous_version'],
            compute_state_version(sess.last_state_send_snapshot),
        )
        self.assertIn('【此刻有一点变化】', delta1.state_text)
        self.assertIn('主灯亮着', delta1.state_text)
        self._commit(sess, delta1, raw_after_delta1)

        raw_after_delta2 = dict(raw_after_delta1)
        raw_after_delta2['drive'] = 'attachment=0.80 curiosity=0.50'
        delta2 = assemble_cc_state_context(
            raw_state=raw_after_delta2,
            is_cold=False,
            user_text='嗯',
            resident=sess,
            lean_on=True,
        )
        self.assertEqual(delta2.state_context_mode, 'delta')
        self.assertEqual(delta2.observation['previous_version'], delta1_current)
        self.assertEqual(delta2.observation['anchor_version'], anchor_version)
        self.assertIn('【此刻有一点变化】', delta2.state_text)
        self.assertIn('对亲近她的欲望很明显', delta2.state_text)


class FallbackTests(unittest.TestCase):
    def test_legacy_full_fallback_uses_snapshot_not_diff(self):
        from chat.persona_state_semantic import (
            format_persona_semantic_snapshot,
            translate_raw_state_to_persona_semantic,
        )

        resident = _resident_stub(last_state_snapshot=RAW_V1)
        legacy = dict(LEGACY_RAW)
        result = assemble_legacy_full_fallback(
            legacy_raw_state=legacy,
            fallback_reason='ValueError',
            resident=resident,
        )
        expected_text = format_persona_semantic_snapshot(
            translate_raw_state_to_persona_semantic(legacy),
        )
        self.assertEqual(result.state_context_mode, 'fallback')
        self.assertEqual(result.state_text, expected_text)
        self.assertEqual(result.state_mode, 'snapshot')
        self.assertFalse(result.commit_meta_extras.get('lean_state_active'))
        self.assertTrue(result.commit_meta_extras.get('state_lean_fallback'))


class GatewayFallbackTests(unittest.TestCase):
    def test_gateway_fail_safe_reloads_legacy_raw_on_assemble_failure(self):
        from chat.context_lean_state import assemble_cc_state_for_resident_turn
        from chat.persona_state_semantic import (
            format_persona_semantic_snapshot,
            translate_raw_state_to_persona_semantic,
        )

        lean_raw = dict(RAW_V1)
        legacy_raw = dict(LEGACY_RAW)
        resident = _resident_stub()
        build_calls = []

        def fake_build_cc_state(*, lean=False):
            build_calls.append(lean)
            return lean_raw if lean else legacy_raw

        with mock.patch('chat.context_lean.lean_state_enabled', return_value=True):
            with mock.patch('chat.system_builder.build_cc_state', side_effect=fake_build_cc_state):
                with mock.patch(
                    'chat.context_lean_state.assemble_cc_state_context',
                    side_effect=ValueError('boom'),
                ):
                    raw_state, state_ctx = assemble_cc_state_for_resident_turn(
                        is_cold=False,
                        user_text='你好',
                        resident=resident,
                    )

        expected_text = format_persona_semantic_snapshot(
            translate_raw_state_to_persona_semantic(legacy_raw),
        )
        self.assertEqual(build_calls, [True, False])
        self.assertIs(raw_state, legacy_raw)
        self.assertEqual(state_ctx.state_text, expected_text)
        self.assertEqual(state_ctx.state_context_mode, 'fallback')
        self.assertFalse(state_ctx.commit_meta_extras.get('lean_state_active'))
        self.assertTrue(state_ctx.commit_meta_extras.get('state_lean_fallback'))

    def test_gateway_fail_safe_reloads_legacy_raw_on_lean_collect_failure(self):
        from chat.context_lean_state import assemble_cc_state_for_resident_turn
        from chat.persona_state_semantic import (
            format_persona_semantic_snapshot,
            translate_raw_state_to_persona_semantic,
        )

        legacy_raw = dict(LEGACY_RAW)
        resident = _resident_stub()
        build_calls = []

        def fake_build_cc_state(*, lean=False):
            build_calls.append(lean)
            if lean:
                raise RuntimeError('lean collect failed')
            return legacy_raw

        with mock.patch('chat.context_lean.lean_state_enabled', return_value=True):
            with mock.patch('chat.system_builder.build_cc_state', side_effect=fake_build_cc_state):
                raw_state, state_ctx = assemble_cc_state_for_resident_turn(
                    is_cold=False,
                    user_text='你好',
                    resident=resident,
                )

        expected_text = format_persona_semantic_snapshot(
            translate_raw_state_to_persona_semantic(legacy_raw),
        )
        self.assertEqual(build_calls, [True, False])
        self.assertIs(raw_state, legacy_raw)
        self.assertEqual(state_ctx.state_text, expected_text)
        self.assertEqual(state_ctx.state_context_mode, 'fallback')
        self.assertFalse(state_ctx.commit_meta_extras.get('lean_state_active'))
        self.assertTrue(state_ctx.commit_meta_extras.get('state_lean_fallback'))


class LeanFactsOnlyCollectionTests(unittest.TestCase):
    _SYSTEM_FIELD_KEYS = (
        'emotion', 'drive', 'lights', 'pocket', 'ledger', 'recent_activity',
    )

    def test_lean_system_generated_fields_are_facts_only(self):
        def get_db():
            raise AssertionError('db should not be queried in this unit test')

        with mock.patch('chat.system_builder._format_structured_emotion_snippet', return_value='valence=0.60 arousal=0.30'), \
             mock.patch('chat.system_builder._format_structured_drive_snippet', return_value='attachment=0.55'), \
             mock.patch('urllib.request.urlopen') as urlopen_mock, \
             mock.patch('config_store.get_bool', return_value=False):
            state = _cc_collect_state(get_db, lean=True)

        urlopen_mock.assert_not_called()
        self.assertNotIn('time_bucket', state)
        for key in self._SYSTEM_FIELD_KEYS:
            value = state.get(key) or ''
            if value:
                self.assertTrue(
                    lean_system_field_is_facts_only(value),
                    msg=f'{key} still carries behavior instructions: {value!r}',
                )
        self.assertEqual(state['lights'], '')

    def test_lean_user_records_preserve_raw_text_without_behavior_prefix(self):
        import sqlite3
        import tempfile

        tmp = tempfile.NamedTemporaryFile(delete=False)
        db_path = tmp.name
        tmp.close()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE board (id INTEGER, author TEXT, tag TEXT, content TEXT, "
            "level TEXT, category TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO board VALUES (1, '爸爸', '语气', '修改 AI 的语气，让表达更克制', '', '给活儿', 'open')"
        )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        with mock.patch('urllib.request.urlopen', side_effect=OSError('no light')), \
             mock.patch('config_store.get_bool', return_value=False):
            state = _cc_collect_state(get_db, lean=True)

        self.assertNotIn('time_bucket', state)
        self.assertIn('user_record:', state['todos'])
        self.assertIn('修改 AI 的语气，让表达更克制', state['todos'])
        self.assertTrue(lean_system_field_is_facts_only(state['lights']))

    def test_lean_reminder_user_records_wrap_due_todos_and_countdowns(self):
        import datetime as dt
        import sqlite3
        import tempfile

        today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
        due_today = today.strftime('%Y-%m-%d')
        due_in_two = (today + dt.timedelta(days=2)).strftime('%Y-%m-%d')
        user_todo_text = '请调整语气，别太热情'

        tmp = tempfile.NamedTemporaryFile(delete=False)
        db_path = tmp.name
        tmp.close()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE todos (id INTEGER, content TEXT, due_date TEXT, done INTEGER)"
        )
        conn.execute(
            "INSERT INTO todos VALUES (1, ?, ?, 0)",
            (user_todo_text, due_today),
        )
        conn.execute(
            "CREATE TABLE countdowns (title TEXT, target_date TEXT, emoji TEXT, type TEXT)"
        )
        conn.execute(
            "INSERT INTO countdowns VALUES ('生日派对', ?, '🎂', 'countdown')",
            (due_in_two,),
        )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        with mock.patch('urllib.request.urlopen', side_effect=OSError('no light')), \
             mock.patch('config_store.get_bool', return_value=False):
            state = _cc_collect_state(get_db, lean=True)

        self.assertNotIn('time_bucket', state)
        reminders = state['reminders']
        self.assertIn('reminders_data:', reminders)
        self.assertIn('user_record: type=todo', reminders)
        self.assertIn(f'content="{user_todo_text}"', reminders)
        self.assertIn('due_status=today', reminders)
        self.assertIn(f'due_date={due_today}', reminders)
        self.assertIn('user_record: type=countdown', reminders)
        self.assertIn('title="生日派对"', reminders)
        self.assertIn('days_remaining=2', reminders)
        self.assertIn('emoji=🎂', reminders)
        self.assertNotIn('- 待办「', reminders)
        self.assertNotIn('你自己判断', reminders)


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


class PersonaSemanticContractTests(unittest.TestCase):
    """9A-2: persona semantic translator contracts."""

    def test_case1_cold_semantic_sanitized(self):
        from chat.daily_history import _build_state_text
        from chat.persona_state_semantic import translate_raw_state_to_persona_semantic

        raw = {
            'emotion': (
                'valence=0.52 arousal=0.30 mood=慵懒 pa=0.50 na=0.20 '
                'longing=0.35 desire_p=0.80 desire_i=0.30 desire_c=0.70'
            ),
            'drive': 'fatigue=0.40 libido=0.75 curiosity=0.20 stress=0.35 social=0.60',
            'lights': 'main=关 bedside=亮',
            'pocket': 'phone_connected=true last_seen=2026-08-02T12:00:00+08:00',
            'todos': 'todos_user_records:\n- item',
            'ledger': 'expense=100.00',
            'reminders': 'reminders_data:\n- todo',
        }
        semantic = translate_raw_state_to_persona_semantic(raw)
        with mock.patch('chat.system_builder.build_cc_state', return_value=raw):
            text, mode, snapshot = _build_state_text(is_cold=True)
        self.assertEqual(mode, 'snapshot')
        self.assertEqual(snapshot, {k: str(v) for k, v in raw.items()})
        self.assertIn('【此刻的感受】', text)
        self.assertNotRegex(text, r'\d+\.\d+')
        self.assertNotIn('libido=', text)
        self.assertNotIn('valence', text)
        self.assertNotIn('last_seen', text)
        self.assertNotIn('schema_version', text)
        self.assertIn('Pocket', text)
        self.assertTrue(semantic['inner_state'])

    def test_case2_hot_stability_small_float_jitter(self):
        from chat.persona_state_semantic import (
            format_persona_semantic_diff,
            translate_raw_state_to_persona_semantic,
        )

        before = {
            'emotion': 'valence=0.60 arousal=0.30 mood=平静 pa=0.50 na=0.20 longing=0.20 desire_p=0.10 desire_i=0.30 desire_c=0.40',
            'drive': 'fatigue=0.40 stress=0.30 social=0.35',
        }
        after = dict(before)
        after['emotion'] = after['emotion'].replace('0.60', '0.61')
        after['drive'] = after['drive'].replace('0.40', '0.41')
        delta = format_persona_semantic_diff(
            translate_raw_state_to_persona_semantic(before),
            translate_raw_state_to_persona_semantic(after),
        )
        self.assertEqual(delta, '')

    def test_case3_representative_semantic_change(self):
        from chat.persona_state_semantic import (
            format_persona_semantic_diff,
            translate_raw_state_to_persona_semantic,
        )

        before = {
            'drive': 'fatigue=0.40 libido=0.20 social=0.35',
        }
        after = {
            'drive': 'fatigue=0.70 libido=0.80 social=0.35',
        }
        delta = format_persona_semantic_diff(
            translate_raw_state_to_persona_semantic(before),
            translate_raw_state_to_persona_semantic(after),
        )
        self.assertIn('【此刻有一点变化】', delta)
        self.assertIn('疲惫', delta)


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
