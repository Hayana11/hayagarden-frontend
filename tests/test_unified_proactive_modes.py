"""morning / nightwatch / self_trigger as Unified main-Chat proactive rounds."""
import datetime
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

from wake.unified_modes import (  # noqa: E402
    UNIFIED_MAIN_CHAT_WAKE_MODES,
    is_unified_main_chat_wake_mode,
)

NOW = datetime.datetime(2026, 10, 23, 8, 30)


def _gateway_source() -> str:
    return ROOT.joinpath('gateway.py').read_text(encoding='utf-8')


def _trigger_builder():
    src = _gateway_source()
    start = src.index('_WAKE_SILENT_SENTINEL = ')
    end = src.index('def _run_unified_normal_main_chat_turn(', start)
    namespace = {'datetime': datetime}
    exec(src[start:end], namespace)  # noqa: S102 - our own source, no imports
    return namespace['_build_normal_wake_main_chat_trigger']


class ProactiveTriggerTextTests(unittest.TestCase):
    SHARED = (
        '【系统主动轮】这是主动唤醒。她没有刚刚发来消息，也不是上一句话的续写；',
        '你有自己的眼睛和手',
        '本轮最多只使用一个只读工具',
        '也可以不调用工具，直接自然地开口。',
        '这些时间只是现实背景，不是行动命令。',
    )

    def setUp(self):
        self.build = _trigger_builder()

    def _assert_shared(self, text):
        for phrase in self.SHARED:
            self.assertIn(phrase, text)
        self.assertIn('\n\n', text)
        self.assertNotIn('\\n', text)

    def test_normal_keeps_its_situation(self):
        text = self.build(now=NOW, t2_hours=2.0, t_hours=1.5)
        self._assert_shared(text)
        self.assertIn('现实已经过去了一段时间。', text)
        self.assertIn('她可能在，也可能不在。', text)
        self.assertIn('距离她上次发消息约 2.0 小时，距离上次有效互动约 1.5 小时。', text)
        self.assertIn('以及最后想对她说什么', text)

    def test_morning_is_the_fixed_morning_with_last_message_clock(self):
        text = self.build(
            now=NOW, t2_hours=6.3, t_hours=6.3, mode='morning',
            last_user_at=datetime.datetime(2026, 10, 23, 2, 13),
        )
        self._assert_shared(text)
        self.assertIn('现在是每天固定的早安时刻，你本来就会在这时候醒来看她。', text)
        self.assertIn('她最后一条消息在 10-23 02:13，距今约 6.3 小时', text)
        self.assertIn('她可能刚醒，也可能还在睡。没人等你交代', text)
        self.assertIn('以及今天想对她说什么', text)
        self.assertNotIn('概率', text)

    def test_morning_without_reliable_clock_falls_back_to_hours(self):
        text = self.build(now=NOW, t2_hours=6.3, t_hours=6.0, mode='morning')
        self.assertNotIn('她最后一条消息在', text)
        self.assertIn('距离她上次发消息约 6.3 小时', text)

    def test_nightwatch_carries_activity_and_night_voice(self):
        text = self.build(
            now=NOW, t2_hours=0.2, t_hours=0.2, mode='nightwatch',
            activity_desc='她手机屏幕还亮着',
        )
        self._assert_shared(text)
        self.assertIn('现在是凌晨，你醒着巡夜，发现她还没睡。', text)
        self.assertIn('她手机屏幕还亮着。', text)
        self.assertIn('这是深夜里你的声音，不是白天那个你。', text)
        self.assertIn('以及这个深夜想对她说什么', text)

    def test_self_trigger_quotes_the_note(self):
        text = self.build(
            now=NOW, t2_hours=1.0, t_hours=1.0, mode='self_trigger',
            self_trigger_note='提醒她喝水',
        )
        self._assert_shared(text)
        self.assertIn('是你之前给自己定的提醒到时间了：「提醒她喝水」', text)
        self.assertIn('怎么兑现由你决定', text)

    def test_situation_is_fact_not_a_command(self):
        for mode in UNIFIED_MAIN_CHAT_WAKE_MODES:
            text = self.build(now=NOW, t2_hours=1.0, t_hours=1.0, mode=mode)
            for directive in ('必须', '应该发', '请发'):
                self.assertNotIn(directive, text)


class ProactiveRoutingTests(unittest.TestCase):
    def test_mode_set(self):
        self.assertEqual(
            set(UNIFIED_MAIN_CHAT_WAKE_MODES),
            {'normal', 'morning', 'nightwatch', 'self_trigger'},
        )
        self.assertTrue(is_unified_main_chat_wake_mode(''))
        self.assertFalse(is_unified_main_chat_wake_mode('ritual'))
        self.assertFalse(is_unified_main_chat_wake_mode('dream'))

    def test_basic_route_takes_every_unified_mode_with_its_context(self):
        src = _gateway_source()
        decide = src[src.index('def _wake_decide_locked'):]
        head = decide[:decide.index('if basic_normal:')]
        self.assertIn('and is_unified_main_chat_wake_mode(mode)', head)
        self.assertIn('and unified_normal_on', head)
        basic = decide[decide.index('if basic_normal:'):decide.index('    planner_view = None')]
        for arg in (
            'mode=mode,',
            "getattr(guard_clock, 'last_user_at', None)",
            'last_user_at=last_user_at,',
            'activity_desc=activity_desc,',
            "self_trigger_note=str(data.get('self_trigger_note') or ''),",
        ):
            self.assertIn(arg, basic)

    def test_unified_only_modes_fail_closed_without_the_route(self):
        from wake import runners
        for mode in ('morning', 'nightwatch', 'self_trigger'):
            with mock.patch.object(runners, '_unified_main_chat_route_on', return_value=False):
                payload = runners.wake_mode_disabled_payload(mode)
                self.assertEqual(payload['reason'], runners.WAKE_MODE_DISABLED_REASON)
            with mock.patch.object(runners, '_unified_main_chat_route_on', return_value=True):
                self.assertIsNone(runners.wake_mode_disabled_payload(mode))


class ProactiveCanonicalHistoryTests(unittest.TestCase):
    def _execute(self, mode):
        from wake.executor import execute

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir).joinpath('w.db'))
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE wake_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, thoughts TEXT, action TEXT,
                    content TEXT, consumed INTEGER, woke_at TEXT, cache_info TEXT,
                    wake_run_id TEXT
                );
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT, content TEXT,
                    thinking TEXT, cache_info TEXT, source_kind TEXT
                );
                """
            )
            conn.commit()
            conn.close()
            with mock.patch('chat.window_identity.soft_window_enabled', return_value=False):
                execute(
                    'message', 'thinking', 'hello', mode,
                    get_db_fn=lambda: sqlite3.connect(db_path),
                    cache_info={
                        'provider': 'claude_code', 'source': 'wake',
                        'b3_authority': True, 'unified_chat_resident': True,
                    },
                    wake_run_id=f'proactive-{mode}',
                    settlement_required=False,
                )
            conn = sqlite3.connect(db_path)
            raw = conn.execute('SELECT cache_info FROM wake_log').fetchone()[0]
            conn.close()
            return json.loads(raw)

    def test_shared_resident_rounds_are_canonical_for_every_unified_mode(self):
        for mode in ('normal', 'morning', 'nightwatch', 'self_trigger'):
            info = self._execute(mode)
            self.assertEqual(info['wake_mode'], mode)
            self.assertIs(info['canonical_chat_history'], True)

    def test_ritual_is_never_canonical(self):
        self.assertIs(self._execute('ritual')['canonical_chat_history'], False)


class _Resident:
    _model_identity = 'model:test'
    session_id = 'sid-1'
    generation = 1

    def _alive(self):
        return True


class MainChatTurnBehaviourTests(unittest.TestCase):
    """Drives the real _run_unified_normal_main_chat_turn with a fake stream."""

    def _run(self, *, flush=True, events=(), raise_after_flush=None, mode='normal',
             lease_error=None, **kwargs):
        import gateway
        from chat import behavior_authority_b3 as b3
        from chat import cc_history_rewrite, display_thinking
        from chat import unified_heartbeat_a1 as uh
        from tools import lease_signer

        sent = []

        def stream(messages, **stream_kwargs):
            sent.append(messages[0]['content'])
            stream_kwargs['on_stdin_begin']()
            if flush:
                stream_kwargs['on_stdin_flushed']()
            if raise_after_flush is not None:
                raise raise_after_flush
            yield from events

        fence = mock.Mock()
        watermark = uh.SharedTranscriptWatermark(
            context_id=1, context_epoch=1, resident_generation=1,
            resident_key='chat:1:1:1', claude_session_id='sid-1',
            transcript_path='/tmp/sid-1.jsonl', expected_offset=0,
            process_generation=1,
        )
        lease = mock.Mock(side_effect=lease_error, return_value={'turn_id': 'wake'})
        with mock.patch.object(gateway, '_CC_RESIDENT', _Resident()), \
                mock.patch.object(gateway, '_gen_acquire_or_wait', return_value=('own', None)), \
                mock.patch.object(gateway, '_gen_release'), \
                mock.patch.object(gateway, '_cc_resident_stream_gen', side_effect=stream), \
                mock.patch.object(gateway, '_wake_live_trace'), \
                mock.patch.object(b3, '_hot_chat_resident_ready', return_value=(True, 'ok')), \
                mock.patch.object(uh, 'prepare_shared_transcript_watermark',
                                  return_value=(watermark, 'ok')), \
                mock.patch.object(uh, 'begin_shared_wake_delivery_fence', return_value=fence), \
                mock.patch.object(uh, 'commit_shared_transcript_watermark',
                                  return_value={'committed': True}), \
                mock.patch.object(cc_history_rewrite, 'guard_cc_generation',
                                  side_effect=lambda events: events), \
                mock.patch.object(display_thinking, 'get_display_thinking_snapshot',
                                  return_value=(False, '')), \
                mock.patch.object(lease_signer, 'issue_turn_lease', lease):
            result = gateway._run_unified_normal_main_chat_turn(
                wake_run_id='proactive-test', now=NOW, t2_hours=1.0, t_hours=1.0,
                mode=mode, **kwargs,
            )
        return result, sent

    @staticmethod
    def _done(text):
        return [('done', (text, '', {'jsonl_usage': {'stream_totals_match': True}}, {}))]

    def test_mode_reaches_the_trigger(self):
        # The generation-lock result must not overwrite the Wake mode.
        _, sent = self._run(
            mode='morning', events=self._done('早'),
            last_user_at=datetime.datetime(2026, 10, 23, 2, 13),
        )
        self.assertIn('现在是每天固定的早安时刻', sent[0])
        self.assertIn('10-23 02:13', sent[0])
        _, sent = self._run(
            mode='self_trigger', events=self._done('喝水'), self_trigger_note='提醒她喝水',
        )
        self.assertIn('「提醒她喝水」', sent[0])

    def test_failure_before_stdin_is_pre_model(self):
        import gateway
        with self.assertRaises(gateway.UnifiedProactivePreModelFailed):
            self._run(lease_error=RuntimeError('lease down'))
        with self.assertRaises(gateway.UnifiedProactivePreModelFailed):
            self._run(flush=False, raise_after_flush=RuntimeError('write failed'))

    def test_failure_in_the_prologue_is_pre_model(self):
        import gateway
        with mock.patch.object(gateway, '_wake_live_trace', side_effect=OSError('trace sink down')), \
                self.assertRaises(gateway.UnifiedProactivePreModelFailed):
            gateway._run_unified_normal_main_chat_turn(
                wake_run_id='prologue', now=NOW, t2_hours=1.0, t_hours=1.0,
                mode='self_trigger', self_trigger_note='提醒她喝水',
            )

    def test_failure_after_stdin_is_not_pre_model(self):
        import gateway
        with self.assertRaises(RuntimeError) as ctx:
            self._run(raise_after_flush=RuntimeError('stream broke'))
        self.assertNotIsInstance(ctx.exception, gateway.UnifiedProactivePreModelFailed)

    def test_silence_marker_is_a_silent_round(self):
        result, sent = self._run(events=self._done(' [[SILENT]] '))
        self.assertTrue(result['silent'])
        self.assertEqual(result['text'], '')
        self.assertIn('只回复 [[SILENT]]，它不会发给她', sent[0])

    def test_spoken_text_is_not_silent_and_marker_never_leaks(self):
        result, _ = self._run(events=self._done('早安 [[SILENT]]'))
        self.assertFalse(result['silent'])
        self.assertEqual(result['text'], '早安')

    def test_empty_reply_is_still_an_error_not_silence(self):
        import gateway
        with self.assertRaisesRegex(RuntimeError, 'normal_wake_main_chat_empty_response') as ctx:
            self._run(events=self._done('   '))
        self.assertNotIsInstance(ctx.exception, gateway.UnifiedProactivePreModelFailed)


class ProactiveDispatchTests(unittest.TestCase):
    def test_silent_round_executes_none_and_pre_model_failure_is_its_own_reason(self):
        src = _gateway_source()
        decide = src[src.index('def _wake_decide_locked'):]
        basic = decide[decide.index('if basic_normal:'):decide.index('    planner_view = None')]
        self.assertIn("wake_action = 'none' if main_turn.get('silent') else 'message'", basic)
        self.assertIn('_wake_exec(\n                wake_action,', basic)
        self.assertIn('except UnifiedProactivePreModelFailed', basic)
        self.assertIn('if not main_turn_entered:', basic)
        self.assertEqual(basic.count("'reason': 'NORMAL_WAKE_PRE_MODEL_FAILED'"), 2)


class ProvenanceStrictnessTests(unittest.TestCase):
    def test_missing_wake_mode_is_never_canonical(self):
        from wake.unified_modes import is_unified_wake_provenance_mode
        for value in (None, '', '  ', 'ritual', 1):
            self.assertFalse(is_unified_wake_provenance_mode(value))
        for value in ('normal', 'morning', 'nightwatch', 'self_trigger'):
            self.assertTrue(is_unified_wake_provenance_mode(value))

    def test_daily_context_rejects_row_without_wake_mode(self):
        from chat.daily_context import is_canonical_conversation_message
        cache = {
            'canonical_chat_history': True, 'unified_chat_resident': True,
            'b3_authority': True, 'source': 'wake', 'provider': 'claude_code',
        }
        row = {
            'author': 'assistant', 'content': 'hi', 'source_kind': 'wake',
            'image_url': '', 'cache_info': json.dumps(cache),
        }

        class Row(dict):
            def keys(self):
                return super().keys()

        check = lambda r: is_canonical_conversation_message(r, include_assistant_wake=True)  # noqa: E731
        self.assertFalse(check(Row(row)))
        cache['wake_mode'] = 'morning'
        row['cache_info'] = json.dumps(cache)
        self.assertTrue(check(Row(row)))


class SelfTriggerReleaseTests(unittest.TestCase):
    def _run(self, wake_result):
        from tools import dream_wake

        claim = io.BytesIO(json.dumps([{'id': 7, 'note': '提醒她喝水'}]).encode())
        claim_cm = mock.MagicMock()
        claim_cm.__enter__.return_value = claim
        with mock.patch.object(dream_wake.urllib.request, 'urlopen', return_value=claim_cm), \
                mock.patch.object(dream_wake, '_call_wake', return_value=wake_result) as call, \
                mock.patch.object(dream_wake, '_release_self_triggers') as release, \
                mock.patch.object(dream_wake, '_log'):
            self.assertTrue(dream_wake.run_self_triggers())
        payload = call.call_args[0][0]
        self.assertEqual(payload['mode'], 'self_trigger')
        self.assertEqual(payload['self_trigger_note'], '提醒她喝水')
        return release

    def test_skips_before_the_model_saw_it_are_released(self):
        for reason in (
            'chat_generating', 'wake_in_progress', 'WAKE_MODE_DISABLED',
            'NORMAL_WAKE_SHARED_UNAVAILABLE_SKIP', 'WAKE_READ_OBSERVATION_FAILED',
            'NORMAL_WAKE_PRE_MODEL_FAILED',
        ):
            release = self._run({'ok': True, 'skipped': True, 'reason': reason})
            release.assert_called_once_with([7])

    def test_a_round_that_ran_is_used_up(self):
        for reason in (
            'NORMAL_WAKE_MAIN_CHAT_FAILED',
            'NORMAL_WAKE_MAIN_CHAT_DELIVERY_FAILED',
            'duplicate_wake_run_id',
        ):
            self._run({'ok': True, 'skipped': True, 'reason': reason}).assert_not_called()
        self._run({'ok': True, 'action': 'message'}).assert_not_called()
        self._run({'ok': True, 'action': 'none'}).assert_not_called()


if __name__ == '__main__':
    unittest.main()
