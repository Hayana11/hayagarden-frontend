"""P-CONTEXT-CLEAN-WINDOW-SHADOW regression tests."""
from __future__ import annotations

import hashlib
import os
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


def _manager_with_fake_resident(fake_resident=None, **kwargs):
    fake_resident = fake_resident or _FakeResident()
    mgr = cws.CleanWindowManager(
        cc_cwd=kwargs.get('cc_cwd', '/tmp/cc-gw'),
        cc_token=kwargs.get('cc_token', 'tok'),
        mcp_config_path=kwargs.get('mcp_config_path', '/tmp/cc-gw/cc-tools.json'),
        get_provider=lambda: 'api_relay',
        get_model=lambda: 'test-model',
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
            provider='api_relay',
            model='m',
        )
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
        parts = build_cc_static_parts()
        prod_sha = _sha(parts['full_system'])
        resident = _FakeResident()
        mgr = cws.CleanWindowManager(
            cc_cwd='/tmp/cc-gw',
            cc_token='tok',
            mcp_config_path='/tmp/cc-gw/cc-tools.json',
            get_provider=lambda: 'api_relay',
            get_model=lambda: 'test-model',
        )
        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('cc_resident.ResidentSession', return_value=resident), \
             mock.patch('chat.clean_window_shadow.tempfile.mkdtemp', return_value='/tmp/clean-shadow-test'):
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
            '/api/debug/clean-window/close',
            '/api/debug/clean-window/reset',
        ):
            resp = client.post(path, json={})
            self.assertEqual(resp.status_code, 404, path)

    def test_gateway_start_when_enabled(self):
        import gateway
        fake_mgr = mock.Mock()
        fake_mgr.start.return_value = {'ok': True, 'session_id': 'clean-shadow:x'}
        with mock.patch.object(gateway, '_clean_window_shadow_enabled', return_value=True), \
             mock.patch.object(gateway, '_clean_window_shadow_manager', return_value=fake_mgr):
            resp = gateway.app.test_client().post('/api/debug/clean-window/start')
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


class CleanWindowShadowConfigTests(unittest.TestCase):
    def test_default_config_store_value(self):
        self.assertEqual(config_store._DEFAULTS.get('CC_CLEAN_WINDOW_SHADOW_ENABLED'), '0')
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            self.assertFalse(cws.enabled())


if __name__ == '__main__':
    unittest.main()
