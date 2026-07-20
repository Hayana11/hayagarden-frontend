"""B1.1: Wake → CC Chat continuous-dialogue bridge."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
# 强制仓库根优先，避免误 import /opt/frontend 的生产 gateway。
sys.path = [p for p in sys.path if p not in (ROOT, '')]
sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

_OPT_FRONTEND = Path('/opt/frontend')
try:
    _OPT_FRONTEND.mkdir(parents=True, exist_ok=True)
    for _name in ('memories.db', 'commands.db'):
        sqlite3.connect(str(_OPT_FRONTEND / _name)).close()
except OSError:
    if 'config_store' not in sys.modules:
        _cs = types.ModuleType('config_store')
        _cs.DB_PATH = ':memory:'
        _cs.get = lambda key, default=None: default
        _cs.get_bool = lambda key, default=False: bool(default)
        _cs.get_int = lambda key, default=0: int(default)
        _cs.set = lambda *a, **k: None
        sys.modules['config_store'] = _cs

from chat.system_builder import (  # noqa: E402
    WAKE_REPLY_BRIDGE_CONTENT_MAX,
    _cc_collect_one_shot,
    format_one_shot,
    format_wake_reply_bridge,
)


def empty_usage():
    return {
        'v': 2,
        'provider': 'claude_code',
        'cache_read': 0,
        'cache_creation': 0,
    }


def _import_gateway():
    """与 test_cc_context_dedup 相同：stub workspace，并确保加载仓库 gateway。"""
    existing = sys.modules.get('gateway')
    if existing is not None:
        mod_file = getattr(existing, '__file__', '') or ''
        if mod_file.startswith(ROOT):
            return existing
        del sys.modules['gateway']

    stubbed = []
    if 'tools.workspace_registry' not in sys.modules:
        reg = mock.MagicMock()
        reg.TOOLS_NOTE = ''
        reg.build_resident_tool_defs.return_value = []
        reg.load_registry.return_value = []
        sys.modules['tools.workspace_registry'] = reg
        stubbed.append('tools.workspace_registry')
    if 'tools.workspace_agent' not in sys.modules:
        wa = mock.MagicMock()
        wa.get_workspace_tool_defs.return_value = []
        sys.modules['tools.workspace_agent'] = wa
        stubbed.append('tools.workspace_agent')
    import gateway
    for name in stubbed:
        sys.modules.pop(name, None)
    mod_file = getattr(gateway, '__file__', '') or ''
    if not mod_file.startswith(ROOT):
        raise ImportError('expected repo gateway, got %s' % mod_file)
    return gateway


class WakeOneShotSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 't.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY,
                woke_at TEXT,
                action TEXT,
                content TEXT,
                thoughts TEXT,
                consumed INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert(self, wid, woke_at, action, content, thoughts='SECRET_THOUGHT', consumed=0):
        conn = self.get_db()
        conn.execute(
            'INSERT INTO wake_log (id, woke_at, action, content, thoughts, consumed) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (wid, woke_at, action, content, thoughts, consumed),
        )
        conn.commit()
        conn.close()

    def test_latest_message_is_bridge_older_is_background(self):
        self._insert(1, '2026-07-20 18:00:00', 'message', '手怎么样了？')
        self._insert(2, '2026-07-20 19:00:00', 'diary', '写了一点夜里的事')
        self._insert(3, '2026-07-20 21:00:00', 'message', '记得吃点东西。')

        one_shot = _cc_collect_one_shot(self.get_db, include_wake=True)
        self.assertEqual(one_shot['wake_ids'], [1, 2, 3])
        self.assertIn('手怎么样了？', one_shot['wake_background'])
        self.assertIn('写了一点夜里的事', one_shot['wake_background'])
        self.assertIn('记得吃点东西。', one_shot['wake_reply_bridge'])
        self.assertNotIn('记得吃点东西。', one_shot['wake_background'])
        self.assertIn('直接回复上面这句话', one_shot['wake_reply_bridge'])
        self.assertNotIn('SECRET_THOUGHT', one_shot['wake_background'])
        self.assertNotIn('SECRET_THOUGHT', one_shot['wake_reply_bridge'])

        text = format_one_shot(one_shot)
        self.assertIn('你醒着的时候', text)
        self.assertIn('手怎么样了？', text)
        self.assertNotIn('记得吃点东西。', text)
        self.assertNotIn('连续对话', text)

    def test_non_message_actions_only_background(self):
        self._insert(1, '2026-07-20 01:00:00', 'none', '她在睡')
        self._insert(2, '2026-07-20 02:00:00', 'explore', '查了点资料')
        one_shot = _cc_collect_one_shot(self.get_db, include_wake=True)
        self.assertTrue(one_shot['wake_background'])
        self.assertEqual(one_shot['wake_reply_bridge'], '')
        self.assertNotIn('直接回复', one_shot['wake_background'])
        self.assertNotIn('连续对话', format_one_shot(one_shot))

    def test_bridge_keeps_long_content_beyond_old_40_char_cap(self):
        body = '手怎么样了？' + ('还疼不疼' * 20)
        self.assertGreater(len(body), 40)
        self.assertLessEqual(len(body), WAKE_REPLY_BRIDGE_CONTENT_MAX)
        self._insert(1, '2026-07-20 18:00:00', 'message', body)
        one_shot = _cc_collect_one_shot(self.get_db, include_wake=True)
        self.assertIn(body, one_shot['wake_reply_bridge'])
        # 旧逻辑只留前 40 字；桥接必须保留完整正文
        self.assertGreater(
            one_shot['wake_reply_bridge'].index(body) + len(body),
            one_shot['wake_reply_bridge'].index(body) + 40,
        )

    def test_bridge_clips_at_500_not_40(self):
        body = 'X' * 600
        bridge = format_wake_reply_bridge(body)
        # full 600 should not appear; first 500 + ellipsis should
        self.assertNotIn('X' * 600, bridge)
        self.assertIn('X' * 500 + '…', bridge)
        self.assertIn('直接回复上面这句话', bridge)


class WakeReplyBridgeHotColdTests(unittest.TestCase):
    def _stream(self, *, is_cold, one_shot, user_text='好多了', relationship_text=''):
        gateway = _import_gateway()
        captured = {}

        class FakeResident:
            last_state_snapshot = {}
            last_group_message_id = 0
            group_cursor_initialized = True
            last_rel_fingerprint = None
            turns_since_rel_sent = 0
            last_rel_mood = None

            def ensure_alive(self, system_text, env):
                return is_cold

            def peek_idle_seconds(self):
                return 30.0

            def send_turn(self, content, commit_meta=None):
                captured['content'] = content
                captured['commit_meta'] = commit_meta
                yield ('done', ('ok', '', empty_usage(), {
                    'feedback_ids': list((commit_meta or {}).get('feedback_ids') or []),
                    'dream_id': (commit_meta or {}).get('dream_id'),
                    'wake_ids': list((commit_meta or {}).get('wake_ids') or []),
                }))

        stack = ExitStack()
        stack.enter_context(mock.patch.object(gateway, '_CC_RESIDENT', FakeResident()))
        stack.enter_context(mock.patch.object(gateway, 'CC_TOKEN', 'tok'))
        stack.enter_context(mock.patch.object(gateway, 'CC_CWD', tempfile.mkdtemp()))
        stack.enter_context(mock.patch.object(gateway, '_recall_memories', return_value=('', [])))
        stack.enter_context(mock.patch('chat.system_builder.build_cc_state', return_value={}))
        stack.enter_context(mock.patch('chat.system_builder.build_cc_one_shot', return_value=one_shot))
        stack.enter_context(mock.patch('chat.system_builder.build_cc_cold_once', return_value={}))
        stack.enter_context(mock.patch('chat.system_builder.build_cc_static_parts', return_value={
            'persona': 'STATIC', 'stable_note': '', 'save_instr': '',
            'full_system': 'STATIC',
        }))
        stack.enter_context(mock.patch.object(gateway, '_fetch_group_chat_rows', return_value=([], 0)))
        stack.enter_context(mock.patch.object(
            gateway, 'messages_to_text',
            return_value='assistant: 手怎么样了？\nuser: 好多了',
        ))
        if relationship_text:
            from chat.relationship_context import RelationshipContextResult
            relationship = RelationshipContextResult(
                text=relationship_text,
                fingerprint='rel-v2:bridge',
                sources={'anchor': 'ok'},
                mood_key='ok',
            )
            stack.enter_context(mock.patch.object(
                gateway.config_store, 'get_bool',
                side_effect=lambda key, default=False: key == 'RELATIONSHIP_CONTEXT_ENABLED',
            ))
            stack.enter_context(mock.patch(
                'chat.relationship_context.build_relationship_context',
                return_value=relationship,
            ))
            stack.enter_context(mock.patch(
                'chat.relationship_context.should_send_relationship',
                return_value=True,
            ))
        else:
            stack.enter_context(mock.patch.object(
                gateway.config_store, 'get_bool', return_value=False,
            ))
        with stack:
            list(gateway._cc_resident_stream_gen(
                [{'role': 'user', 'content': user_text}],
                user_turn=True,
            ))
        return captured

    def test_hot_bridge_immediately_before_user_after_relationship(self):
        bridge = format_wake_reply_bridge('手怎么样了？')
        one_shot = {
            'wake_background': '## 你醒着的时候\n- [18:00] 你写了篇日记：夜里',
            'wake_reply_bridge': bridge,
            'wake_ids': [1, 2],
            'task_feedback': '',
            'dream_flash': '',
            'feedback_ids': [],
            'dream_id': None,
        }
        rel = '【近期关系脉络】\n当前基调：偏暖。'
        captured = self._stream(
            is_cold=False, one_shot=one_shot, user_text='好多了',
            relationship_text=rel,
        )
        content = captured['content']
        self.assertIn(bridge, content)
        self.assertIn('好多了', content)
        self.assertIn('直接回复上面这句话', content)
        self.assertLess(content.index(rel), content.index(bridge))
        self.assertLess(content.index(bridge), content.index('好多了'))
        # background still in earlier one-shot block
        self.assertIn('你醒着的时候', content)
        self.assertLess(content.index('你醒着的时候'), content.index(rel))
        self.assertEqual(captured['commit_meta'].get('wake_ids'), [1, 2])

    def test_cold_does_not_inject_bridge_or_wake_background(self):
        bridge = format_wake_reply_bridge('手怎么样了？')
        one_shot = {
            'wake_background': '## 你醒着的时候\n- [18:00] 你主动发了条消息：手怎么样了？',
            'wake_reply_bridge': bridge,
            'wake_ids': [9],
            'task_feedback': '',
            'dream_flash': '',
            'feedback_ids': [],
            'dream_id': None,
        }
        captured = self._stream(is_cold=True, one_shot=one_shot, user_text='好多了')
        content = captured['content']
        self.assertNotIn('连续对话·紧邻上一句', content)
        self.assertNotIn('直接回复上面这句话', content)
        self.assertNotIn('你醒着的时候', content)
        # cold still relies on transcript assistant wake message
        self.assertIn('手怎么样了？', content)
        self.assertEqual(captured['commit_meta'].get('wake_ids'), [9])


class WakeConsumeSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 't.db')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY,
                woke_at TEXT,
                action TEXT,
                content TEXT,
                thoughts TEXT,
                consumed INTEGER DEFAULT 0
            )
            """
        )
        conn.executemany(
            'INSERT INTO wake_log (id, woke_at, action, content, consumed) VALUES (?,?,?,?,0)',
            [
                (1, '2026-07-20 18:00:00', 'message', '手怎么样了？'),
                (2, '2026-07-20 21:00:00', 'message', '记得吃点东西。'),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_failure_keeps_pending(self):
        from chat.context_continuity import capture_pending_wake_ids, consume_wake_ids
        claim = capture_pending_wake_ids(self.get_db)
        self.assertEqual(claim, [1, 2])
        # simulate flush/model/persist failure: do not consume
        conn = self.get_db()
        states = dict(conn.execute('SELECT id, consumed FROM wake_log'))
        conn.close()
        self.assertEqual(states, {1: 0, 2: 0})
        # later success consumes only snapshot
        consume_wake_ids(self.get_db, claim)
        conn = self.get_db()
        states = dict(conn.execute('SELECT id, consumed FROM wake_log'))
        conn.close()
        self.assertEqual(states, {1: 1, 2: 1})

    def test_consume_only_snapshot_ids(self):
        from chat.context_continuity import consume_wake_ids
        consume_wake_ids(self.get_db, [2])
        conn = self.get_db()
        states = dict(conn.execute('SELECT id, consumed FROM wake_log'))
        conn.close()
        self.assertEqual(states[1], 0)
        self.assertEqual(states[2], 1)


if __name__ == '__main__':
    unittest.main()
