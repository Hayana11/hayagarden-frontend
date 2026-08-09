"""P-CONTEXT-CLEAN-WINDOW-SHADOW regression tests."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_store
from chat import clean_window_shadow as cws


def _sha(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


VALID_HANDOFF_SHA256 = hashlib.sha256(b'clean-shadow-handoff-fixture').hexdigest()


class _FakeResident:
    generation = 1
    session_id = 'shadow-test-session'
    last_group_message_id = 0
    group_cursor_initialized = True
    committed_file_hashes = set()
    last_state_snapshot = {}
    last_state_send_snapshot = {}

    def __init__(self, *, cold_turns=1):
        self._alive_calls = 0
        self._cold_turns = cold_turns
        self.sent_contents: list[str] = []
        self.sent_commit_meta: list[dict] = []

    def ensure_alive(self, system_text, env):
        self._alive_calls += 1
        return self._alive_calls <= self._cold_turns

    def _kill(self, quiet=True):
        pass

    def send_turn(self, content, commit_meta=None):
        self.sent_contents.append(content)
        self.sent_commit_meta.append(commit_meta or {})
        yield ('text', 'shadow reply')
        yield ('done', ('shadow reply', '', {'input_tokens': 1, 'output_tokens': 2}, []))


class _RespawnResident(_FakeResident):
    def ensure_alive(self, system_text, env):
        self._alive_calls += 1
        return self._alive_calls in (1, 3)


class _FailingResident(_FakeResident):
    def send_turn(self, content, commit_meta=None):
        self.sent_contents.append(content)
        self.sent_commit_meta.append(commit_meta or {})
        yield ('text', 'partial')


def _manager_with_fake_resident(fake_resident=None, **kwargs):
    fake_resident = fake_resident or _FakeResident()
    mgr = cws.CleanWindowManager(
        cc_cwd=kwargs.get('cc_cwd', '/tmp/cc-gw'),
        cc_token=kwargs.get('cc_token', 'tok'),
        mcp_config_path=kwargs.get('mcp_config_path', '/tmp/cc-gw/cc-tools.json'),
        get_provider=kwargs.get('get_provider', lambda: 'claude_code'),
        get_model=kwargs.get('get_model', lambda: 'test-model'),
    )
    static_parts = {
        'persona': 'PERSONA_BLOCK',
        'stable_note': 'NOTE',
        'save_instr': 'SAVE',
        'full_system': 'PERSONA_BLOCK\n\nNOTE\n\nSAVE',
    }
    patches = [
        mock.patch.object(config_store, 'get_bool', return_value=True),
        mock.patch('chat.system_builder.build_cc_static_parts', return_value=static_parts),
        mock.patch('cc_resident.ResidentSession', return_value=fake_resident),
        mock.patch('chat.clean_window_shadow.tempfile.mkdtemp', return_value='/tmp/clean-shadow-test'),
    ]
    return mgr, fake_resident, static_parts, patches


class _PatchedManager:
    def __init__(self, fake_resident=None, **kwargs):
        self._mgr, self._resident, self._static_parts, self._patches = _manager_with_fake_resident(
            fake_resident, **kwargs
        )
        self._stack = None

    def __enter__(self):
        self._stack = ExitStack()
        for p in self._patches:
            self._stack.enter_context(p)
        return self._stack, self._mgr, self._resident, self._static_parts

    def __exit__(self, exc_type, exc, tb):
        if self._stack is not None:
            self._stack.close()
        return False


class CleanWindowShadowUnitTests(unittest.TestCase):
    def setUp(self):
        cws.reset_manager_for_tests()

    def tearDown(self):
        cws.reset_manager_for_tests()

    def test_disabled_by_default(self):
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            self.assertFalse(cws.enabled())
        self.assertEqual(config_store._DEFAULTS.get('CC_CLEAN_WINDOW_SHADOW_ENABLED'), '0')

    def test_strip_save_markers(self):
        cleaned, had = cws.strip_save_markers('你好 [[SAVE: secret]] 世界')
        self.assertTrue(had)
        self.assertEqual(cleaned, '你好  世界')

    def test_side_effect_tool_detection(self):
        self.assertTrue(cws.is_side_effect_tool('mcp__home__light_on'))
        self.assertTrue(cws.is_side_effect_tool('mcp__home__add_todo'))
        self.assertFalse(cws.is_side_effect_tool('mcp__codebase__read_file'))

    def test_manifest_flags_all_dynamic_sources_off(self):
        manifest = cws.build_base_manifest(
            session_id='clean-shadow:abc',
            static_system_sha256='a' * 64,
            persona_sha256='b' * 64,
            provider='claude_code',
            model='m',
        )
        self.assertEqual(manifest['actual_executor'], 'claude_code')
        for key in (
            'state_injected', 'cold_once_injected', 'long_term_memory_injected',
            'handoff_injected', 'diary_summary_injected', 'web_memo_injected',
            'auto_recall_injected', 'ombre_recall_injected', 'relationship_context_injected',
            'wake_bridge_injected', 'one_shot_injected', 'file_context_injected',
            'old_tool_history_injected', 'formal_chat_history_injected',
            'formal_resident_reused', 'formal_conversation_id_reused',
        ):
            self.assertFalse(manifest[key], key)
        self.assertTrue(manifest['clean_window_shadow'])
        self.assertTrue(manifest['side_effect_tools_blocked'])
        self.assertFalse(manifest['save_marker_persisted'])


class CleanWindowShadowSessionTests(unittest.TestCase):
    def setUp(self):
        cws.reset_manager_for_tests()

    def tearDown(self):
        cws.reset_manager_for_tests()

    def _run_with_patches(self, fake_resident=None):
        return _PatchedManager(fake_resident)

    def test_static_parity_with_production_builder(self):
        from chat.system_builder import build_cc_static_parts
        resident = _FakeResident()
        mgr = cws.CleanWindowManager(
            cc_cwd='/tmp/cc-gw',
            cc_token='tok',
            mcp_config_path='/tmp/cc-gw/cc-tools.json',
            get_provider=lambda: 'claude_code',
            get_model=lambda: 'test-model',
        )
        with mock.patch('chat.system_builder.read_persona', return_value='PERSONA_FIXED'), \
             mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('cc_resident.ResidentSession', return_value=resident), \
             mock.patch('chat.clean_window_shadow.tempfile.mkdtemp', return_value='/tmp/clean-shadow-test'):
            parts = build_cc_static_parts()
            prod_sha = _sha(parts['full_system'])
            started = mgr.start()
            self.assertEqual(started['static_system_sha256'], prod_sha)
            self.assertEqual(
                started['context_manifest']['static_system_sha256'],
                prod_sha,
            )
            self.assertEqual(started['persona_sha256'], _sha(parts.get('persona') or ''))

    def test_empty_history_on_start(self):
        with self._run_with_patches() as (stack, mgr, resident, static_parts):
            started = mgr.start()
            self.assertTrue(started['session_id'].startswith(cws.SESSION_PREFIX))
            manifest = started['context_manifest']
            self.assertEqual(manifest['shadow_history_user_turns'], 0)
            self.assertEqual(manifest['shadow_history_assistant_turns'], 0)
            self.assertFalse(manifest['formal_resident_reused'])
            self.assertFalse(manifest['formal_conversation_id_reused'])
            self.assertEqual(len(resident.sent_contents), 0)

    def test_session_continuity_within_clean_window(self):
        resident = _FakeResident(cold_turns=0)
        with self._run_with_patches(resident) as (stack, mgr, *_):
            started = mgr.start()
            sid = started['session_id']
            turn1 = mgr.turn(sid, '第一轮')
            self.assertEqual(turn1['turn_index'], 1)
            self.assertEqual(turn1['history_message_count'], 2)
            turn2 = mgr.turn(sid, '第二轮')
            self.assertEqual(turn2['turn_index'], 2)
            self.assertEqual(turn2['history_message_count'], 4)
            self.assertEqual(resident.sent_contents[0], '第一轮')
            self.assertEqual(resident.sent_contents[1], '第二轮')

    def test_sessions_are_isolated(self):
        resident_a = _FakeResident(cold_turns=0)
        resident_b = _FakeResident(cold_turns=0)
        call_count = {'n': 0}

        def _factory(*_a, **_k):
            call_count['n'] += 1
            return resident_a if call_count['n'] == 1 else resident_b

        mgr, _, _, patches = _manager_with_fake_resident()
        patches.append(mock.patch('cc_resident.ResidentSession', side_effect=_factory))
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            a = mgr.start()
            b = mgr.start()
            mgr.turn(a['session_id'], 'A-only')
            mgr.turn(b['session_id'], 'B-only')
            self.assertEqual(resident_a.sent_contents, ['A-only'])
            self.assertEqual(resident_b.sent_contents, ['B-only'])

    def test_dynamic_injection_builders_not_called(self):
        resident = _FakeResident(cold_turns=0)
        with self._run_with_patches(resident) as (_stack, mgr, *_):
            with mock.patch('chat.system_builder.build_cc_state') as st, \
                 mock.patch('chat.system_builder.build_cc_cold_once') as cold, \
                 mock.patch('chat.system_builder.build_cc_one_shot') as one_shot, \
                 mock.patch('chat.relationship_context.build_relationship_context') as rel:
                started = mgr.start()
                mgr.turn(started['session_id'], '诊断消息')
                st.assert_not_called()
                cold.assert_not_called()
                one_shot.assert_not_called()
                rel.assert_not_called()

    def test_commit_meta_empty_no_formal_cursor_touch(self):
        resident = _FakeResident(cold_turns=0)
        with self._run_with_patches(resident) as (stack, mgr, *_):
            started = mgr.start()
            mgr.turn(started['session_id'], 'hi')
            self.assertEqual(len(resident.sent_commit_meta), 1)
            self.assertEqual(resident.sent_commit_meta[0], {})

    def test_save_marker_suppressed(self):
        class SaveResident(_FakeResident):
            def send_turn(self, content, commit_meta=None):
                yield ('text', '可见 [[SAVE: hidden]] 文本')
                yield ('done', ('', '', {}, []))

        with self._run_with_patches(SaveResident(cold_turns=0)) as (stack, mgr, *_):
            started = mgr.start()
            turn = mgr.turn(started['session_id'], '保存测试')
            self.assertNotIn('[[SAVE:', turn['content'])
            self.assertTrue(turn['context_manifest']['save_marker_suppressed'])
            self.assertFalse(turn['context_manifest']['save_marker_persisted'])

    def test_tool_side_effect_blocked(self):
        class ToolResident(_FakeResident):
            def send_turn(self, content, commit_meta=None):
                yield ('tool_use', {'id': 't1', 'name': 'mcp__home__light_on', 'args': {}})
                yield ('text', '灯没开')
                yield ('done', ('灯没开', '', {}, []))

        with self._run_with_patches(ToolResident(cold_turns=0)) as (stack, mgr, *_):
            started = mgr.start()
            turn = mgr.turn(started['session_id'], '开灯')
            manifest = turn['context_manifest']
            self.assertIn('mcp__home__light_on', manifest['blocked_tool_calls'])
            self.assertTrue(manifest['side_effect_tools_blocked'])

    def test_close_removes_session(self):
        with self._run_with_patches() as (stack, mgr, *_):
            started = mgr.start()
            sid = started['session_id']
            closed = mgr.close(sid)
            self.assertTrue(closed['ok'])
            with self.assertRaises(KeyError):
                mgr.turn(sid, 'after close')

    def test_ttl_expiry(self):
        with self._run_with_patches() as (stack, mgr, resident, *_):
            started = mgr.start()
            sid = started['session_id']
            session = mgr._sessions[sid]
            session.expires_at = time.time() - 1
            killed = {'n': 0}
            original_kill = resident._kill

            def tracked_kill(*args, **kwargs):
                killed['n'] += 1
                return original_kill(*args, **kwargs)

            resident._kill = tracked_kill
            with self.assertRaises(KeyError):
                mgr.turn(sid, 'expired')
            self.assertEqual(killed['n'], 1)
            self.assertNotIn(sid, mgr._sessions)

    def test_ttl_purge_closes_session_resources(self):
        with self._run_with_patches() as (stack, mgr, resident, *_):
            started = mgr.start()
            sid = started['session_id']
            session = mgr._sessions[sid]
            session.expires_at = time.time() - 1
            killed = {'n': 0}
            original_kill = resident._kill

            def tracked_kill(*args, **kwargs):
                killed['n'] += 1
                return original_kill(*args, **kwargs)

            resident._kill = tracked_kill
            os.makedirs(session.work_dir, exist_ok=True)
            marker = os.path.join(session.work_dir, 'marker')
            with open(marker, 'w', encoding='utf-8') as fh:
                fh.write('ttl')
            mgr._purge_expired()
            self.assertNotIn(sid, mgr._sessions)
            self.assertEqual(killed['n'], 1)
            self.assertFalse(os.path.exists(marker))

    def test_turn_limit_auto_closes(self):
        with self._run_with_patches() as (stack, mgr, *_):
            started = mgr.start()
            sid = started['session_id']
            mgr._sessions[sid].turn_count = cws.MAX_TURNS_PER_SESSION
            with self.assertRaises(RuntimeError):
                mgr.turn(sid, 'too many')

    def test_first_turn_sends_raw_message_when_cold(self):
        class AlwaysColdResident(_FakeResident):
            def ensure_alive(self, system_text, env):
                self._alive_calls += 1
                return True

        msg = '爸爸，我今天有一点累，你抱着小猫说一会儿话。'
        with self._run_with_patches(AlwaysColdResident()) as (stack, mgr, resident, *_):
            started = mgr.start()
            mgr.turn(started['session_id'], msg)
            self.assertEqual(resident.sent_contents[0], msg)
            self.assertNotIn('诊断会话', resident.sent_contents[0])

    def test_mid_session_respawn_replays_clean_history(self):
        class RespawnOnThirdAlive(_FakeResident):
            def ensure_alive(self, system_text, env):
                self._alive_calls += 1
                return self._alive_calls in (1, 3)

        with self._run_with_patches(RespawnOnThirdAlive()) as (stack, mgr, resident, *_):
            started = mgr.start()
            sid = started['session_id']
            mgr.turn(sid, '第一轮')
            mgr.turn(sid, '第二轮')
            self.assertEqual(resident.sent_contents[0], '第一轮')
            self.assertIn('诊断会话内的对话记录', resident.sent_contents[1])
            self.assertIn('[用户] 第二轮', resident.sent_contents[1])

    def test_rejects_non_claude_code_provider(self):
        mgr = cws.CleanWindowManager(
            cc_cwd='/tmp/cc-gw',
            cc_token='tok',
            mcp_config_path='/tmp/cc-gw/cc-tools.json',
            get_provider=lambda: 'api_relay',
            get_model=lambda: 'test-model',
        )
        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('cc_resident.ResidentSession', return_value=_FakeResident()):
            with self.assertRaisesRegex(RuntimeError, 'claude_code'):
                mgr.start()

    def test_manifest_reports_actual_executor(self):
        with self._run_with_patches() as (stack, mgr, *_):
            started = mgr.start()
            manifest = started['context_manifest']
            self.assertEqual(manifest['provider'], 'claude_code')
            self.assertEqual(manifest['actual_executor'], 'claude_code')

    def test_start_failure_cleans_work_dir(self):
        class BoomResident:
            def ensure_alive(self, *args, **kwargs):
                raise RuntimeError('boom')

            def _kill(self, quiet=True):
                pass

        tmp = tempfile.mkdtemp()
        work_dir = os.path.join(tmp, 'clean-shadow-fail')
        os.makedirs(work_dir)
        marker = os.path.join(work_dir, 'marker')
        with open(marker, 'w', encoding='utf-8') as fh:
            fh.write('x')
        mgr = cws.CleanWindowManager(
            cc_cwd='/tmp/cc-gw',
            cc_token='tok',
            mcp_config_path='/tmp/cc-gw/cc-tools.json',
            get_provider=lambda: 'claude_code',
            get_model=lambda: 'test-model',
        )
        try:
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                     'persona': 'P', 'full_system': 'P',
                 }), \
                 mock.patch('cc_resident.ResidentSession', return_value=BoomResident()), \
                 mock.patch('chat.clean_window_shadow.tempfile.mkdtemp', return_value=work_dir):
                with self.assertRaises(RuntimeError):
                    mgr.start()
                self.assertEqual(len(mgr._sessions), 0)
                self.assertFalse(os.path.exists(marker))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ttl_timer_closes_without_followup_request(self):
        resident = _FakeResident(cold_turns=0)
        killed = {'n': 0}
        original_kill = resident._kill

        def tracked_kill(*args, **kwargs):
            killed['n'] += 1
            return original_kill(*args, **kwargs)

        resident._kill = tracked_kill
        with self._run_with_patches(resident) as (stack, mgr, *_):
            started = mgr.start()
            sid = started['session_id']
            session = mgr._sessions[sid]
            if session._ttl_timer is not None:
                session._ttl_timer.cancel()
            session.expires_at = time.time() + 0.05
            mgr._schedule_ttl(session)
            time.sleep(0.2)
            self.assertNotIn(sid, mgr._sessions)
            self.assertEqual(killed['n'], 1)

    def test_expire_session_closes_even_if_timer_fires_early(self):
        resident = _FakeResident(cold_turns=0)
        killed = {'n': 0}
        original_kill = resident._kill

        def tracked_kill(*args, **kwargs):
            killed['n'] += 1
            return original_kill(*args, **kwargs)

        resident._kill = tracked_kill
        with self._run_with_patches(resident) as (stack, mgr, *_):
            started = mgr.start()
            sid = started['session_id']
            session = mgr._sessions[sid]
            session.expires_at = time.time() + 60
            mgr._expire_session(sid)
            self.assertNotIn(sid, mgr._sessions)
            self.assertEqual(killed['n'], 1)

    def test_close_force_works_when_feature_disabled(self):
        with self._run_with_patches() as (stack, mgr, *_):
            started = mgr.start()
            sid = started['session_id']
            with mock.patch.object(config_store, 'get_bool', return_value=False):
                closed = mgr.close(sid, force=True)
            self.assertTrue(closed['ok'])


class CleanWindowShadowGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs('/opt/workspace/tools', exist_ok=True)
        touch = os.path.join('/opt/workspace/tools', '.gitkeep')
        if not os.path.exists(touch):
            open(touch, 'w').close()

    def setUp(self):
        cws.reset_manager_for_tests()

    def tearDown(self):
        cws.reset_manager_for_tests()

    def test_gateway_endpoints_disabled_by_default(self):
        import gateway
        client = gateway.app.test_client()
        for path in (
            '/api/debug/clean-window/start',
            '/api/debug/clean-window/turn',
            '/api/debug/clean-window/reset',
        ):
            resp = client.post(path, json={})
            self.assertEqual(resp.status_code, 404, path)
        resp = client.post('/api/debug/clean-window/close', json={'session_id': 'clean-shadow:x'})
        self.assertIn(resp.status_code, (401, 503))

    def test_gateway_requires_bearer_token(self):
        import gateway
        client = gateway.app.test_client()
        with mock.patch.object(gateway, '_clean_window_shadow_enabled', return_value=True), \
             mock.patch.object(gateway, 'CC_CLEAN_WINDOW_SHADOW_TOKEN', 'top-secret'):
            resp = client.post('/api/debug/clean-window/start', json={})
            self.assertEqual(resp.status_code, 401)
            resp = client.post(
                '/api/debug/clean-window/start',
                json={},
                headers={'Authorization': 'Bearer top-secret'},
            )
            self.assertNotEqual(resp.status_code, 401)

    def test_gateway_close_allowed_when_disabled_with_auth(self):
        import gateway
        fake_mgr = mock.Mock()
        fake_mgr.close.return_value = {'ok': True, 'session_id': 'clean-shadow:x'}
        client = gateway.app.test_client()
        with mock.patch.object(gateway, '_clean_window_shadow_enabled', return_value=False), \
             mock.patch.object(gateway, 'CC_CLEAN_WINDOW_SHADOW_TOKEN', 'top-secret'), \
             mock.patch.object(gateway, '_clean_window_shadow_manager', return_value=fake_mgr):
            resp = client.post(
                '/api/debug/clean-window/close',
                json={'session_id': 'clean-shadow:x'},
                headers={'Authorization': 'Bearer top-secret'},
            )
            self.assertEqual(resp.status_code, 200)
            fake_mgr.close.assert_called_once_with('clean-shadow:x', force=True)

    def test_gateway_start_when_enabled(self):
        import gateway
        fake_mgr = mock.Mock()
        fake_mgr.start.return_value = {'ok': True, 'session_id': 'clean-shadow:x'}
        with mock.patch.object(gateway, '_clean_window_shadow_enabled', return_value=True), \
             mock.patch.object(gateway, 'CC_CLEAN_WINDOW_SHADOW_TOKEN', 'top-secret'), \
             mock.patch.object(gateway, '_clean_window_shadow_manager', return_value=fake_mgr):
            resp = gateway.app.test_client().post(
                '/api/debug/clean-window/start',
                headers={'Authorization': 'Bearer top-secret'},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()['ok'])

    def test_formal_chat_path_unchanged(self):
        with open(os.path.join(ROOT, 'gateway.py'), encoding='utf-8') as fh:
            gateway_src = fh.read()
        for needle in (
            'def _cc_resident_stream_gen',
            'build_cc_state',
            'build_cc_cold_once',
            'build_cc_one_shot',
            '_recall_memories',
            '_CC_RESIDENT',
        ):
            self.assertIn(needle, gateway_src)

    def test_no_chat_messages_write_from_shadow_turn(self):
        db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        db.close()
        conn = sqlite3.connect(db.name)
        conn.execute(
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT)"
        )
        conn.execute("INSERT INTO chat_messages(author, content) VALUES ('hayana', 'formal')")
        conn.commit()
        before = conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0]
        conn.close()

        resident = _FakeResident(cold_turns=0)
        mgr, _, _, patches = _manager_with_fake_resident(resident)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            started = mgr.start()
            mgr.turn(started['session_id'], 'shadow only')
        conn = sqlite3.connect(db.name)
        after = conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0]
        rows = conn.execute('SELECT content FROM chat_messages').fetchall()
        conn.close()
        os.unlink(db.name)
        self.assertEqual(before, after)
        self.assertEqual(rows[0][0], 'formal')


class DailyCandidateShadowTests(unittest.TestCase):
    def setUp(self):
        cws.reset_manager_for_tests()
        self._tmpdir = tempfile.mkdtemp()
        self._handoff_patch = mock.patch('chat.day_handoff.SHADOW_HANDOFF_DIR', self._tmpdir)
        self._handoff_patch.start()
        import chat.day_handoff as dh
        dh.ensure_shadow_handoff_dir()

    def tearDown(self):
        self._handoff_patch.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        cws.reset_manager_for_tests()

    def _write_handoff_file(self, name: str = 'day_handoff_20260726.yaml') -> str:
        import chat.day_handoff as dh
        data = {
            'day': '2026-07-26',
            'source_day': '2026-07-26',
            'source_start_at': '2026-07-26 04:00:00',
            'source_end_at': '2026-07-27 03:59:59',
            'source_first_message_id': 1,
            'source_last_message_id': 2,
            'source_message_count': 2,
            'source_sha256': VALID_HANDOFF_SHA256,
            'extraction_mode': 'conservative_rules',
            'requires_human_review': True,
            'topics': ['疲劳'],
            'confirmed_facts': [],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': ['不要给我列建议，只要陪我'],
            'last_topic': '用户谈及休息',
        }
        path = os.path.join(self._tmpdir, name)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(dh.format_day_handoff_yaml(data))
        os.chmod(path, 0o600)
        return path

    def _run_with_patches(self, fake_resident=None):
        return _PatchedManager(fake_resident)

    def test_daily_candidate_requires_valid_file_path(self):
        resident = _FakeResident(cold_turns=2)
        fake_state = {
            'time_bucket': 'bucket=2026-07-27 12:00',
            'emotion': 'valence=0.5',
            'lights': 'main=关 bedside=关',
        }
        handoff_path = self._write_handoff_file()
        with self._run_with_patches(resident) as (stack, mgr, *_):
            with mock.patch('chat.clean_window_shadow._build_daily_state_text') as build_state:
                build_state.side_effect = [
                    ('【当前状态】\n' + fake_state['emotion'], 'snapshot', fake_state),
                    ('', 'none', fake_state),
                ]
                started = mgr.start(
                    context_profile='daily_candidate',
                    day_handoff_path=handoff_path,
                )
                self.assertEqual(started['context_profile'], 'daily_candidate')
                manifest = started['context_manifest']
                self.assertTrue(manifest['day_handoff_loaded'])
                self.assertFalse(manifest['day_handoff_injected_this_turn'])
                self.assertFalse(manifest['cold_once_injected'])

                sid = started['session_id']
                turn1 = mgr.turn(sid, '第一轮')
                first = resident.sent_contents[0]
                self.assertIn('【昨日交接·事实记录】', first)
                self.assertIn('【当前状态】', first)
                m1 = turn1['context_manifest']
                self.assertTrue(m1['day_handoff_injected_this_turn'])
                self.assertTrue(m1['state_injected_this_turn'])

                turn2 = mgr.turn(sid, '第二轮')
                second = resident.sent_contents[1]
                self.assertNotIn('【昨日交接·事实记录】', second)
                self.assertEqual(second, '第二轮')
                m2 = turn2['context_manifest']
                self.assertFalse(m2['day_handoff_injected_this_turn'])

    def test_daily_candidate_reinjects_on_cold_respawn(self):
        resident = _RespawnResident(cold_turns=99)
        fake_state = {'time_bucket': 'bucket=1', 'emotion': 'valence=0.5', 'lights': 'main=关 bedside=关'}
        handoff_path = self._write_handoff_file()
        with self._run_with_patches(resident) as (stack, mgr, *_):
            with mock.patch('chat.clean_window_shadow._build_daily_state_text') as build_state:
                build_state.side_effect = [
                    ('【当前状态】\nX', 'snapshot', fake_state),
                    ('', 'none', fake_state),
                    ('【当前状态】\nX', 'snapshot', fake_state),
                ]
                started = mgr.start(context_profile='daily_candidate', day_handoff_path=handoff_path)
                sid = started['session_id']
                mgr.turn(sid, '第一轮')
                mgr.turn(sid, '第二轮')
                second = resident.sent_contents[1]
                self.assertIn('【昨日交接·事实记录】', second)

    def test_send_turn_failure_does_not_commit_state_snapshot(self):
        resident = _FailingResident(cold_turns=1)
        handoff_path = self._write_handoff_file()
        with self._run_with_patches(resident) as (stack, mgr, *_):
            with mock.patch('chat.clean_window_shadow._build_daily_state_text') as build_state:
                build_state.return_value = ('【当前状态】\nX', 'snapshot', {'emotion': 'x'})
                started = mgr.start(context_profile='daily_candidate', day_handoff_path=handoff_path)
                sid = started['session_id']
                with self.assertRaises(RuntimeError):
                    mgr.turn(sid, 'fail')
                session = mgr._sessions[sid]
                self.assertEqual(session.last_state_snapshot, {})
                self.assertFalse(session.last_state_injected_this_turn)

    def test_raw_day_handoff_text_not_accepted(self):
        with self._run_with_patches(_FakeResident()) as (stack, mgr, *_):
            with self.assertRaises(ValueError):
                mgr.start(context_profile='daily_candidate', day_handoff_path=None)

    def test_clean_profile_unchanged(self):
        with self._run_with_patches() as (stack, mgr, resident, *_):
            started = mgr.start(context_profile='clean')
            self.assertEqual(started.get('context_profile', 'clean'), 'clean')
            mgr.turn(started['session_id'], 'hi')
            self.assertEqual(resident.sent_contents[0], 'hi')


class CleanWindowShadowConfigTests(unittest.TestCase):
    def test_default_config_store_value(self):
        self.assertEqual(config_store._DEFAULTS.get('CC_CLEAN_WINDOW_SHADOW_ENABLED'), '0')
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            self.assertFalse(cws.enabled())


if __name__ == '__main__':
    unittest.main()
