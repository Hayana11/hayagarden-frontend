"""Focused tests for CC resident context dedup / usage v2 (no TreeGPT changes)."""

from __future__ import annotations

import datetime
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

# CI / 沙箱可能没有 /opt/frontend；在 import 生产模块前铺好路径与 stub。
_OPT_FRONTEND = Path('/opt/frontend')
try:
    _OPT_FRONTEND.mkdir(parents=True, exist_ok=True)
    for _name in ('memories.db', 'commands.db'):
        sqlite3.connect(str(_OPT_FRONTEND / _name)).close()
except OSError:
    # 无写权限时 stub config_store，避免 import 期连不上硬编码 DB
    if 'config_store' not in sys.modules:
        _cs = types.ModuleType('config_store')
        _cs.DB_PATH = ':memory:'
        _cs.get = lambda key, default=None: default
        _cs.get_bool = lambda key, default=False: bool(default)
        _cs.get_int = lambda key, default=0: int(default)
        _cs.set = lambda *a, **k: None
        sys.modules['config_store'] = _cs

from chat.system_builder import (
    _cc_collect_cold_once,
    _cc_collect_one_shot,
    _cc_collect_state,
    build_cc_static_parts,
    build_cc_static_system,
    build_stable_note,
    format_one_shot,
    format_state_diff,
    format_state_snapshot,
)
from chat.context_budget import file_content_sha256, file_ref_key, merge_cumulative_state_send
from cc_resident import (
    CC_STREAM_HARD_TIMEOUT,
    CC_STREAM_TIMEOUT,
    ResidentError,
    ResidentSession,
    StreamWatchdog,
    empty_usage,
    normalize_cache_info,
    summarize_rounds,
)


def _import_gateway():
    """gateway 在 import 时会拉 workspace 工具注册；CI/沙箱无 /opt/workspace。"""
    if 'gateway' in sys.modules:
        return sys.modules['gateway']
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
    # 清掉临时 stub，避免污染后续要 reload 真实模块的用例
    for name in stubbed:
        sys.modules.pop(name, None)
    return gateway


class NoTimeBucketRuntimeTests(unittest.TestCase):
    def test_build_time_bucket_removed(self):
        import chat.system_builder as sb
        self.assertFalse(hasattr(sb, 'build_time_bucket'))

    def test_collect_state_omits_time_bucket(self):
        def get_db():
            raise AssertionError('db not needed')

        with mock.patch('urllib.request.urlopen', side_effect=OSError('no')), \
             mock.patch('config_store.get_bool', return_value=False):
            state = _cc_collect_state(get_db, lean=True)
        self.assertNotIn('time_bucket', state)


class StableSystemTests(unittest.TestCase):
    def test_stable_note_byte_identical(self):
        first = build_stable_note()
        second = build_stable_note()
        self.assertEqual(first, second)

    def test_static_system_byte_identical(self):
        with mock.patch('chat.system_builder.read_persona', return_value='PERSONA_FIXED'):
            first = build_cc_static_system()
            second = build_cc_static_system()
            parts = build_cc_static_parts()
        self.assertEqual(first, second)
        self.assertIn('PERSONA_FIXED', first)
        self.assertIn('[[SAVE:', first)
        self.assertIn('私聊窗口', first)
        self.assertEqual(parts['full_system'], first)
        self.assertEqual(
            parts['full_system'],
            '\n\n'.join(
                p for p in (
                    parts['persona'],
                    parts['stable_note'],
                    parts['tool_companion_intuition'],
                    parts['save_instr'],
                )
                if p and str(p).strip()
            ),
        )


class StateDiffTests(unittest.TestCase):
    def test_unchanged_returns_empty(self):
        state = {'lights': '关', 'todos': '待办 A'}
        self.assertEqual(format_state_diff(state, state), '')

    def test_single_component_change(self):
        before = {'lights': '关', 'todos': '待办 A'}
        after = {'lights': '开 35%', 'todos': '待办 A'}
        text = format_state_diff(before, after)
        self.assertIn('灯', text)
        self.assertNotIn('待办 A', text)

    def test_cleared_state(self):
        before = {'reminders': '## 今日提醒\n- 明天到期'}
        after = {'reminders': ''}
        text = format_state_diff(before, after)
        self.assertIn('今日提醒：已清空', text)

    def test_snapshot_includes_all(self):
        text = format_state_snapshot({'lights': '关', 'todos': '待办 A'})
        self.assertIn('【当前状态】', text)
        self.assertIn('关', text)
        self.assertIn('待办 A', text)


class UsageV2Tests(unittest.TestCase):
    def test_sum_rounds_not_max(self):
        rounds = [
            {'index': 1, 'complete': True, 'input_tokens': 100, 'output_tokens': 10,
             'cache_read': 30000, 'cache_creation': 0, 'context_tokens': 30100},
            {'index': 2, 'complete': True, 'input_tokens': 200, 'output_tokens': 20,
             'cache_read': 77000, 'cache_creation': 0, 'context_tokens': 77200},
            {'index': 3, 'complete': True, 'input_tokens': 300, 'output_tokens': 30,
             'cache_read': 78000, 'cache_creation': 0, 'context_tokens': 78300},
        ]
        usage = summarize_rounds(rounds)
        self.assertEqual(usage['input_tokens'], 600)
        self.assertEqual(usage['cache_read'], 185000)
        self.assertEqual(usage['last_round_context'], 78300)
        self.assertEqual(usage['num_rounds'], 3)

    def test_cumulative_read_does_not_look_like_context(self):
        rounds = [
            {
                'index': i,
                'complete': True,
                'input_tokens': 500,
                'output_tokens': 100,
                'cache_read': 78000,
                'cache_creation': 0,
                'context_tokens': 78500,
            }
            for i in range(1, 7)
        ]
        usage = summarize_rounds(rounds)
        self.assertEqual(usage['cache_read'], 468000)
        self.assertEqual(usage['last_round_context'], 78500)
        self.assertLess(usage['last_round_context'], 100000)

    def test_legacy_cache_info_compatible(self):
        legacy = normalize_cache_info({'cache_read': 100, 'cache_creation': 20})
        self.assertEqual(legacy['cache_read'], 100)
        self.assertEqual(legacy['cache_creation'], 20)
        self.assertEqual(legacy.get('num_rounds', 1), 1)

        v2 = normalize_cache_info({'v': 2, 'provider': 'claude_code', 'num_rounds': 2, 'rounds': [{}, {}]})
        self.assertEqual(v2['num_rounds'], 2)
        self.assertEqual(len(v2['rounds']), 2)


class FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(''.join(line + '\n' for line in lines))
        self.stderr = io.StringIO()
        self._code = None
        self.kill_called = False

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0

    def kill(self):
        self.kill_called = True
        self._code = -9

    def wait(self, timeout=None):
        return self._code or 0


class CapturingStdin:
    def __init__(self, fail_flush=False):
        self.written = []
        self.flushed = False
        self.fail_flush = fail_flush
        self.closed = False

    def write(self, data):
        self.written.append(data)
        return len(data)

    def flush(self):
        self.flushed = True
        if self.fail_flush:
            raise BrokenPipeError('flush boom')

    def close(self):
        self.closed = True


class ResidentCommitTests(unittest.TestCase):
    def _session(self):
        return ResidentSession('/tmp', '', '/tmp/cc-tools.json')

    def test_commit_only_after_successful_flush(self):
        sess = self._session()
        sess._proc = FakeProc([])
        sess._cold = False

        class BrokenStdin:
            def write(self, _):
                raise BrokenPipeError('boom')

            def flush(self):
                pass

        sess._proc.stdin = BrokenStdin()
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'state_snapshot': {'lights': '开'},
                'group_cursor_initialized': True,
                'group_max_id': 105,
                'rel_tick': True,
                'rel_fingerprint': 'rel-v2:failed',
            }))
        self.assertEqual(sess.last_state_snapshot, {})
        self.assertEqual(sess.last_group_message_id, 0)
        self.assertFalse(sess.group_cursor_initialized)
        self.assertIsNone(sess.last_rel_fingerprint)
        self.assertEqual(sess.turns_since_rel_sent, 0)

    def test_commit_kept_when_stream_fails_after_flush(self):
        sess = self._session()
        # EOF before result → ResidentError, but flush already succeeded
        sess._proc = FakeProc([])
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'state_snapshot': {'lights': '开'},
                'group_cursor_initialized': True,
                'group_max_id': 105,
                'rel_tick': True,
                'rel_fingerprint': 'rel-v2:sent',
            }))
        self.assertEqual(sess.last_state_snapshot.get('lights'), '开')
        self.assertEqual(sess.last_group_message_id, 105)
        self.assertTrue(sess.group_cursor_initialized)
        self.assertEqual(sess.last_rel_fingerprint, 'rel-v2:sent')
        self.assertEqual(sess.turns_since_rel_sent, 0)

    def test_is_error_kills_resident(self):
        lines = [
            json.dumps({
                'type': 'result',
                'is_error': True,
                'result': 'boom',
                'usage': {},
            }),
        ]
        sess = self._session()
        fake = FakeProc(lines)
        sess._proc = fake
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'state_snapshot': {'lights': '开'},
                'group_cursor_initialized': True,
                'group_max_id': 9,
            }))
        self.assertIsNone(sess._proc)
        self.assertTrue(fake.kill_called or fake._code is not None)

    def test_partial_rounds_kept_on_error(self):
        lines = [
            json.dumps({
                'type': 'stream_event',
                'event': {
                    'type': 'message_start',
                    'message': {'usage': {
                        'input_tokens': 100,
                        'output_tokens': 10,
                        'cache_read_input_tokens': 30000,
                        'cache_creation_input_tokens': 0,
                    }},
                },
            }),
            json.dumps({
                'type': 'assistant',
                'message': {
                    'usage': {
                        'input_tokens': 100,
                        'output_tokens': 10,
                        'cache_read_input_tokens': 30000,
                        'cache_creation_input_tokens': 0,
                    },
                    'content': [{'type': 'tool_use', 'id': 't1', 'name': 'x', 'input': {}}],
                },
            }),
            json.dumps({
                'type': 'stream_event',
                'event': {
                    'type': 'message_start',
                    'message': {'usage': {
                        'input_tokens': 200,
                        'output_tokens': 20,
                        'cache_read_input_tokens': 70000,
                        'cache_creation_input_tokens': 0,
                    }},
                },
            }),
            json.dumps({
                'type': 'result',
                'is_error': True,
                'result': 'boom',
                'usage': {},
            }),
        ]
        sess = self._session()
        sess._proc = FakeProc(lines)
        sess._cold = False
        with self.assertRaises(ResidentError) as ctx:
            list(sess.send_turn('hi'))
        usage = ctx.exception.usage
        self.assertGreaterEqual(len(usage.get('rounds') or []), 1)
        self.assertTrue(usage['rounds'][0].get('complete'))
        self.assertIsNone(sess._proc)


class FeedbackConsumeTests(unittest.TestCase):
    """feedback / dream：flush 后不消费；仅 assistant 落库成功后消费。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'commands.db')
        self._cmd = __import__('command_store')
        self._orig_path = self._cmd.DB_PATH
        self._cmd.DB_PATH = self.db_path
        self._cmd._init()
        cid = self._cmd.issue('拿快递', countdown_seconds=60)
        self._cmd.mark_started(cid)
        self._cmd.mark_done(cid)

    def tearDown(self):
        self._cmd.DB_PATH = self._orig_path
        self.tmp.cleanup()

    def test_flush_fail_does_not_consume(self):
        lines, ids = self._cmd.peek_feedback()
        self.assertTrue(ids)
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        fake = FakeProc([])
        fake.stdin = CapturingStdin(fail_flush=True)
        sess._proc = fake
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'feedback_ids': ids,
                'state_snapshot': {},
            }))
        still, still_ids = self._cmd.peek_feedback()
        self.assertEqual(still_ids, ids)
        self.assertTrue(still)

    def test_flush_success_eof_does_not_consume(self):
        lines, ids = self._cmd.peek_feedback()
        self.assertTrue(ids)
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        # flush 成功后 EOF → 报错；feedback 不得消费
        sess._proc = FakeProc([])
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'feedback_ids': ids,
                'state_snapshot': {},
            }))
        still, still_ids = self._cmd.peek_feedback()
        self.assertEqual(still_ids, ids)

    def test_result_error_does_not_consume(self):
        lines, ids = self._cmd.peek_feedback()
        self.assertTrue(ids)
        result_lines = [
            json.dumps({
                'type': 'result',
                'is_error': True,
                'result': 'boom',
                'usage': {},
            }),
        ]
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._proc = FakeProc(result_lines)
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'feedback_ids': ids,
                'state_snapshot': {},
            }))
        still, still_ids = self._cmd.peek_feedback()
        self.assertEqual(still_ids, ids)

    def test_done_returns_claims_without_consuming(self):
        lines, ids = self._cmd.peek_feedback()
        self.assertTrue(ids)
        ok_lines = [
            json.dumps({
                'type': 'result',
                'is_error': False,
                'result': 'ok',
                'usage': {},
            }),
        ]
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._proc = FakeProc(ok_lines)
        sess._cold = False
        events = list(sess.send_turn('hi', commit_meta={
            'feedback_ids': ids,
            'dream_id': 7,
            'state_snapshot': {},
        }))
        self.assertEqual(events[-1][0], 'done')
        done = events[-1][1]
        self.assertEqual(len(done), 4)
        claims = done[3]
        self.assertEqual(claims.get('feedback_ids'), ids)
        self.assertEqual(claims.get('dream_id'), 7)
        # usage / claims 分离：公开 usage 不含 feedback_ids
        self.assertNotIn('feedback_ids', done[2])
        still, still_ids = self._cmd.peek_feedback()
        self.assertEqual(still_ids, ids)

    def test_persist_success_consumes_feedback(self):
        from chat.system_builder import consume_cc_one_shot_claims
        lines, ids = self._cmd.peek_feedback()
        self.assertTrue(ids)
        # 模拟 gateway：模型成功 + DB 落库成功后才 consume
        consume_cc_one_shot_claims(lambda: None, {'feedback_ids': ids, 'dream_id': None})
        still, still_ids = self._cmd.peek_feedback()
        self.assertEqual(still_ids, [])
        self.assertEqual(still, [])

    def test_persist_failure_does_not_consume(self):
        """模型成功但 DB 持久化失败 → 不调用 consume（gateway 契约）。"""
        lines, ids = self._cmd.peek_feedback()
        self.assertTrue(ids)
        # 未调用 consume_cc_one_shot_claims 即表示持久化失败路径
        still, still_ids = self._cmd.peek_feedback()
        self.assertEqual(still_ids, ids)


class DreamConsumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE dream_pool (
                id INTEGER PRIMARY KEY, content TEXT, tone TEXT,
                surfaced INTEGER DEFAULT 0, surface_count INTEGER DEFAULT 0,
                created_at TEXT, surfaced_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO dream_pool (id, content, tone, surfaced, surface_count, created_at) "
            "VALUES (1, '一段旧梦', 'soft', 0, 0, '2026-07-18 10:00:00')"
        )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self.get_db = get_db

    def tearDown(self):
        self.tmp.cleanup()

    def _surfaced(self):
        conn = self.get_db()
        row = conn.execute('SELECT surfaced FROM dream_pool WHERE id=1').fetchone()
        conn.close()
        return int(row['surfaced'])

    def test_eof_does_not_consume_dream(self):
        from chat.system_builder import consume_dream_one_shot
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._proc = FakeProc([])
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={'dream_id': 1, 'state_snapshot': {}}))
        self.assertEqual(self._surfaced(), 0)
        # 显式证明 consume 才会标记
        consume_dream_one_shot(self.get_db, 1)
        self.assertEqual(self._surfaced(), 1)

    def test_persist_success_consumes_dream(self):
        from chat.system_builder import consume_cc_one_shot_claims
        self.assertEqual(self._surfaced(), 0)
        consume_cc_one_shot_claims(self.get_db, {'feedback_ids': [], 'dream_id': 1})
        self.assertEqual(self._surfaced(), 1)


class ClientDisconnectKillTests(unittest.TestCase):
    def test_generator_close_kills_resident(self):
        lines = [
            json.dumps({
                'type': 'stream_event',
                'event': {
                    'type': 'content_block_delta',
                    'delta': {'type': 'text_delta', 'text': 'hello'},
                },
            }),
            json.dumps({
                'type': 'stream_event',
                'event': {
                    'type': 'content_block_delta',
                    'delta': {'type': 'text_delta', 'text': ' world'},
                },
            }),
            json.dumps({
                'type': 'result',
                'is_error': False,
                'result': 'ok',
                'usage': {},
            }),
        ]
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        fake = FakeProc(lines)
        sess._proc = fake
        sess._cold = False
        gen = sess.send_turn('hi', commit_meta={'state_snapshot': {}})
        evt, payload = next(gen)
        self.assertEqual(evt, 'text')
        self.assertEqual(payload, 'hello')
        gen.close()
        self.assertIsNone(sess._proc)



class GatewayOneShotPersistWiringTests(unittest.TestCase):
    """gateway CC 路径：assistant commit 之后才 consume feedback/dream。"""

    def test_consume_follows_commit_in_source(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'gateway.py').read_text(encoding='utf-8')
        # Assistant commit lives in _persist_turn_assistant (normal path).
        helper_at = source.find('def _persist_turn_assistant')
        self.assertGreater(helper_at, 0)
        helper_chunk = source[helper_at:helper_at + 5000]
        # Normal-path INSERT must commit before returning assistant_id.
        insert_at = helper_chunk.find("INSERT INTO chat_messages")
        self.assertGreater(insert_at, 0)
        helper_commit_at = helper_chunk.find('conn.commit()', insert_at)
        self.assertGreater(helper_commit_at, insert_at)
        marker = "for evt, payload in _cc_resident_stream_gen"
        idx = source.find(marker)
        self.assertGreater(idx, 0)
        chunk = source[idx:idx + 12000]
        persist_at = chunk.find('_persist_turn_assistant(')
        wake_at = chunk.find('consume_wake_ids(')
        oneshot_at = chunk.find('consume_cc_one_shot_claims(')
        self.assertGreater(persist_at, 0)
        self.assertGreater(wake_at, persist_at)
        self.assertGreater(oneshot_at, persist_at)
        cache_start = chunk.find('_cache_info_json')
        self.assertGreater(cache_start, 0)
        self.assertNotIn('feedback_ids', chunk[cache_start:persist_at])


class ResidentCumulativeStateTests(unittest.TestCase):
    def test_commit_merges_cumulative_send_snapshot(self):
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._last_state_send_snapshot = merge_cumulative_state_send(
            {},
            {'lights': '关', 'emotion': '平静'},
        )
        sess._commit_sent_context({'state_send_snapshot': {}})
        self.assertIn('lights', sess.last_state_send_snapshot)
        sess._commit_sent_context({'state_send_snapshot': {'lights': ''}})
        self.assertNotIn('lights', sess.last_state_send_snapshot)
        self.assertIn('emotion', sess.last_state_send_snapshot)


class ResidentFileRefTests(unittest.TestCase):
    def test_spawn_clears_committed_file_refs(self):
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        body = 'file-a'
        ref = file_ref_key('/static/a.txt', file_content_sha256(body))
        sess._committed_file_hashes = {ref}
        with mock.patch('subprocess.Popen', return_value=FakeProc([])), \
             mock.patch('chat.cc_runtime.require_pinned_claude_version', return_value='2.1.220'), \
             mock.patch('chat.cc_runtime.claude_cmd', side_effect=lambda *a, **k: ['claude', *a]):
            sess._spawn('STATIC', {}, reason='turn_limit')
        self.assertEqual(sess.committed_file_hashes, set())

    def test_commit_stores_url_hash_pairs(self):
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        body = 'file-a'
        ref = file_ref_key('/static/a.txt', file_content_sha256(body))
        sess._commit_sent_context({'file_inject_hashes': [ref]})
        self.assertEqual(sess.committed_file_hashes, {ref})


class ResidentRespawnFileBootstrapTests(unittest.TestCase):
    def test_respawn_cold_uses_empty_known_files_for_history(self):
        gateway = _import_gateway()
        body = 'FULL FILE BODY'
        url = '/static/doc.txt'
        ref = file_ref_key(url, file_content_sha256(body))

        class FakeResident:
            def __init__(self):
                self.committed_file_hashes = {ref}
                self.last_state_snapshot = {}
                self.last_state_send_snapshot = {}
                self.last_group_message_id = 0
                self._group_cursor_initialized = True
                self.generation = 2
                self.tool_surface_snapshot = {}
                self._spawned = False

            @property
            def group_cursor_initialized(self):
                return self._group_cursor_initialized

            def ensure_alive(self, system_text, env):
                self._spawned = True
                self.committed_file_hashes = set()
                return True

        fake = FakeResident()
        captured_build = {}

        def fake_build_messages(*, resident_file_hashes=None, history_stats_out=None):
            captured_build['resident_file_hashes'] = set(resident_file_hashes or ())
            return [{'role': 'user', 'content': 'hi'}]

        from chat.system_builder import build_cc_static_parts
        with mock.patch('chat.system_builder.read_persona', return_value='PERSONA_FIXED'):
            parts = build_cc_static_parts()
        is_cold = fake.ensure_alive(parts['full_system'], {})
        resident_files = set() if is_cold else fake.committed_file_hashes
        with mock.patch.object(gateway, 'build_messages', side_effect=fake_build_messages):
            gateway.build_messages(resident_file_hashes=resident_files)

        self.assertTrue(fake._spawned)
        self.assertEqual(captured_build['resident_file_hashes'], set())

    def test_same_url_new_hash_not_in_known_set(self):
        url = '/static/a.txt'
        old_ref = file_ref_key(url, file_content_sha256('v1'))
        new_ref = file_ref_key(url, file_content_sha256('v2'))
        self.assertNotEqual(old_ref, new_ref)

    def test_hot_file_present_in_list_content(self):
        gateway = _import_gateway()
        captured = {}

        class FakeResident:
            last_state_snapshot = {}
            last_state_send_snapshot = {}
            committed_file_hashes = set()
            last_group_message_id = 0
            group_cursor_initialized = True
            tool_surface_snapshot = {}

            def ensure_alive(self, system_text, env):
                return False

            def peek_idle_seconds(self):
                return None

            def send_turn(self, content, commit_meta=None):
                captured['content'] = content
                captured['commit_meta'] = commit_meta
                yield ('done', ('ok', '', empty_usage(), {}))

        with mock.patch.object(gateway, '_CC_RESIDENT', FakeResident()), \
             mock.patch.object(gateway, 'CC_TOKEN', 'tok'), \
             mock.patch.object(gateway, 'CC_CWD', tempfile.mkdtemp()), \
             mock.patch.object(gateway, '_recall_memories', return_value=('', [])), \
             mock.patch('chat.system_builder.build_cc_state', return_value={}), \
             mock.patch('chat.system_builder.build_cc_one_shot', return_value={
                 'wake_nonmessage_background': '', 'wake_message_background': '',
                 'wake_reply_bridge': '', 'wake_ids': [], 'wake_items': [],
                 'task_feedback': '', 'dream_flash': '',
                 'feedback_ids': [], 'dream_id': None,
             }), \
             mock.patch('chat.system_builder.build_cc_cold_once', return_value={}), \
             mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                 'persona': 'STATIC', 'stable_note': '', 'save_instr': '', 'full_system': 'STATIC',
             }), \
             mock.patch.object(gateway, '_fetch_group_chat_rows', return_value=([], 0)):
            file_text = '[用户发来文件: note.txt]\n```\nhello\n```'
            list(gateway._cc_resident_stream_gen(
                [{'role': 'user', 'content': [{'type': 'text', 'text': file_text + '\n请读'}]}],
                user_turn=True,
                is_cold=False,
            ))
        self.assertTrue(captured['commit_meta'].get('hot_file_present'))


class _DelayedLineProc:
    """Fake Claude subprocess whose stdout lines arrive with per-line delays."""

    def __init__(self, timed_lines, *, hang_after=False):
        # timed_lines: list[(delay_before_line_sec, line_str)]
        self._timed_lines = list(timed_lines)
        self._idx = 0
        self._hang_after = hang_after
        self.stdin = io.StringIO()
        self.stderr = io.StringIO()
        self._code = None
        self.kill_called = False
        self.pid = 4242

    def poll(self):
        return self._code

    def terminate(self):
        self._code = -15

    def kill(self):
        self.kill_called = True
        self._code = -9

    def wait(self, timeout=None):
        return self._code or 0

    @property
    def stdout(self):
        return self

    def readline(self):
        if self._code is not None:
            return ''
        if self._idx >= len(self._timed_lines):
            if not self._hang_after:
                return ''
            while self._code is None:
                time.sleep(0.02)
            return ''
        delay, line = self._timed_lines[self._idx]
        self._idx += 1
        end = time.time() + float(delay)
        while time.time() < end:
            if self._code is not None:
                return ''
            time.sleep(min(0.02, max(0.0, end - time.time())))
        return line + '\n'

    def close(self):
        return None


def _thinking_line(text='…'):
    return json.dumps({
        'type': 'stream_event',
        'event': {
            'type': 'content_block_delta',
            'delta': {'type': 'thinking_delta', 'thinking': text},
        },
    }, ensure_ascii=False)


def _result_line(text='ok'):
    return json.dumps({
        'type': 'result',
        'is_error': False,
        'result': text,
        'usage': {},
    }, ensure_ascii=False)


class StreamTimeoutConfigTests(unittest.TestCase):
    """CC_STREAM_TIMEOUT = stall; CC_STREAM_HARD_TIMEOUT = absolute ceiling."""

    def _watchdog_kwargs_for_cfg(self, cfg_map):
        ok_lines = [_result_line()]
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._proc = FakeProc(ok_lines)
        sess._cold = False
        captured = []

        class CapturingWatchdog:
            def __init__(self, **kwargs):
                captured.append(dict(kwargs))
                self._inner = StreamWatchdog(**kwargs)

            def start(self):
                return self._inner.start()

            def stop(self):
                return self._inner.stop()

            def note_activity(self):
                return self._inner.note_activity()

            @property
            def fired_reason(self):
                return self._inner.fired_reason

        with mock.patch(
            'cc_resident._cfg_int',
            side_effect=lambda k, d: cfg_map.get(k, d),
        ), mock.patch('cc_resident.StreamWatchdog', CapturingWatchdog):
            list(sess.send_turn('hi', commit_meta={'state_snapshot': {}}))
        return captured

    def test_case1_default_stall_timeout_360(self):
        captured = self._watchdog_kwargs_for_cfg({})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['stall_timeout'], 360)
        self.assertEqual(CC_STREAM_TIMEOUT, 360)

    def test_case2_runtime_stall_config_900(self):
        captured = self._watchdog_kwargs_for_cfg({'CC_STREAM_TIMEOUT': 900})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['stall_timeout'], 900)

    def test_hard_timeout_default_1800(self):
        captured = self._watchdog_kwargs_for_cfg({})
        self.assertEqual(captured[0]['hard_timeout'], 1800)
        self.assertEqual(CC_STREAM_HARD_TIMEOUT, 1800)

    def test_hard_timeout_runtime_config(self):
        captured = self._watchdog_kwargs_for_cfg({'CC_STREAM_HARD_TIMEOUT': 720})
        self.assertEqual(captured[0]['hard_timeout'], 720)


class StreamWatchdogSemanticsTests(unittest.TestCase):
    """CASE 3–6: stall refresh, true stall, hard ceiling, stale race."""

    def _cfg(self, stall, hard):
        return {
            'CC_STREAM_TIMEOUT': stall,
            'CC_STREAM_HARD_TIMEOUT': hard,
        }

    def test_case3_sustained_thinking_survives_past_stall(self):
        """Turn wall time > stall, but thinking keeps refreshing activity."""
        stall = 0.18
        hard = 5.0
        # 6 thinking chunks * 0.05s gap ≈ 0.30s wall > stall; each gap < stall.
        timed = [(0.05, _thinking_line(str(i))) for i in range(6)]
        timed.append((0.02, _result_line('alive')))
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        fake = _DelayedLineProc(timed)
        sess._proc = fake
        sess._cold = False
        with mock.patch(
            'cc_resident._cfg_int',
            side_effect=lambda k, d: self._cfg(stall, hard).get(k, d),
        ):
            events = list(sess.send_turn('hi', commit_meta={'state_snapshot': {}}))
        kinds = [k for k, _ in events]
        self.assertIn('think', kinds)
        self.assertIn('done', kinds)
        self.assertFalse(fake.kill_called)
        self.assertIs(sess._proc, fake)

    def test_case4_true_stall_kills_and_raises(self):
        stall = 0.12
        hard = 5.0
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        fake = _DelayedLineProc([], hang_after=True)
        sess._proc = fake
        sess._cold = False
        with mock.patch(
            'cc_resident._cfg_int',
            side_effect=lambda k, d: self._cfg(stall, hard).get(k, d),
        ):
            with self.assertRaises(ResidentError) as ctx:
                list(sess.send_turn('hi', commit_meta={'state_snapshot': {}}))
        msg = str(ctx.exception)
        self.assertIn('长时间无活动', msg)
        self.assertIn('resident 进程已重启', msg)
        self.assertIsNone(sess._proc)

    def test_case5_hard_timeout_despite_continuous_activity(self):
        stall = 5.0
        hard = 0.25
        # Keep thinking forever (hang after exhausting a few refreshes).
        timed = [(0.04, _thinking_line(str(i))) for i in range(40)]
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        fake = _DelayedLineProc(timed, hang_after=True)
        sess._proc = fake
        sess._cold = False
        with mock.patch(
            'cc_resident._cfg_int',
            side_effect=lambda k, d: self._cfg(stall, hard).get(k, d),
        ):
            with self.assertRaises(ResidentError) as ctx:
                list(sess.send_turn('hi', commit_meta={'state_snapshot': {}}))
        self.assertIn('绝对上限', str(ctx.exception))
        self.assertIn('resident 进程已重启', str(ctx.exception))
        self.assertIsNone(sess._proc)

    def test_case6_stale_watchdog_race_does_not_kill(self):
        killed = []

        def on_timeout(reason):
            killed.append(reason)

        wd = StreamWatchdog(
            stall_timeout=0.2,
            hard_timeout=30.0,
            on_timeout=on_timeout,
            is_proc_alive=lambda: True,
            poll_cap_sec=0.05,
        )
        # Simulate: stall deadline already elapsed…
        with wd._lock:
            wd._last_activity_at = time.monotonic() - 1.0
        # …but a fresh thinking activity arrives before the stale fire commits.
        wd.note_activity()
        fired = wd._try_fire('stall')
        self.assertFalse(fired)
        self.assertEqual(killed, [])
        self.assertIsNone(wd.fired_reason)
        wd.stop()


class ResidentRespawnTests(unittest.TestCase):
    def test_turn_limit_triggers_before_next_send(self):
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        with mock.patch('cc_resident._cfg_int', side_effect=lambda k, d: {
            'CC_MAX_RESIDENT_TURNS': 2,
            'CC_CONTEXT_SOFT_LIMIT': 90_000,
            'CC_CONTEXT_HARD_LIMIT': 120_000,
            'CC_MIN_TURNS_BETWEEN_RESPAWNS': 5,
        }.get(k, d)):
            sess._proc = FakeProc([])
            sess._cold = False
            sess._system_text = 'STATIC'
            sess._last_used = 10**12
            sess._resident_turn_count = 2
            with mock.patch.object(sess, '_spawn') as spawn:
                sess.ensure_alive('STATIC', {})
                spawn.assert_called()
                self.assertEqual(spawn.call_args.kwargs.get('reason'), 'turn_limit')

    def test_spawn_resets_snapshots(self):
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._last_state_snapshot = {'lights': '开'}
        sess._last_group_message_id = 99
        sess._group_cursor_initialized = True
        sess._resident_turn_count = 9
        sess._last_rel_fingerprint = 'rel-v2:old'
        sess._turns_since_rel_sent = 6
        with mock.patch('subprocess.Popen', return_value=FakeProc([])), \
             mock.patch('chat.cc_runtime.require_pinned_claude_version', return_value='2.1.220'), \
             mock.patch('chat.cc_runtime.claude_cmd', side_effect=lambda *a, **k: ['claude', *a]):
            sess._spawn('STATIC', {}, reason='turn_limit')
        self.assertEqual(sess.last_state_snapshot, {})
        self.assertEqual(sess.last_group_message_id, 0)
        self.assertFalse(sess.group_cursor_initialized)
        self.assertEqual(sess._resident_turn_count, 0)
        self.assertIsNone(sess.last_rel_fingerprint)
        self.assertEqual(sess.turns_since_rel_sent, 0)
        self.assertEqual(sess.pending_respawn_reason, 'turn_limit')


class GroupQueryContractTests(unittest.TestCase):
    """必须真实调用 _fetch_group_chat_rows 并 mock DB，不能只对手写列表求 max。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE group_chat_messages (
                id INTEGER PRIMARY KEY, room TEXT, author TEXT,
                content TEXT, created_at TEXT
            )"""
        )
        for i, content in ((101, 'a'), (102, 'b'), (103, 'c')):
            conn.execute(
                "INSERT INTO group_chat_messages (id, room, author, content, created_at) "
                "VALUES (?, 'group', 'user', ?, '2026-07-18 10:00:00')",
                (i, content),
            )
        # 远古 backlog：id 很小，若错误用 id>0 热查询会全部读出
        conn.execute(
            "INSERT INTO group_chat_messages (id, room, author, content, created_at) "
            "VALUES (1, 'group', 'user', 'ancient', '2020-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        self._gateway = _import_gateway()
        self._orig_get_db = self._gateway.get_db

        def _get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self._gateway.get_db = _get_db

    def tearDown(self):
        self._gateway.get_db = self._orig_get_db
        self.tmp.cleanup()

    def test_max_id_from_same_query_rows(self):
        rows, max_id = self._gateway._fetch_group_chat_rows(limit=3, cold=True)
        self.assertEqual([int(r['id']) for r in rows], [101, 102, 103])
        self.assertEqual(max_id, 103)
        self.assertNotEqual(max_id, 104)

    def test_empty_success_returns_zero_not_none(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('DELETE FROM group_chat_messages')
        conn.commit()
        conn.close()
        rows, max_id = self._gateway._fetch_group_chat_rows(limit=8, cold=True)
        self.assertEqual(rows, [])
        self.assertEqual(max_id, 0)

    def test_query_failure_returns_none_max_id(self):
        def _boom():
            raise RuntimeError('db down')

        self._gateway.get_db = _boom
        rows, max_id = self._gateway._fetch_group_chat_rows(limit=8, cold=True)
        self.assertEqual(rows, [])
        self.assertIsNone(max_id)

    def test_cold_failure_does_not_read_ancient_via_hot(self):
        """冷查询失败后不得用 id>0 热查询读远古 backlog。"""
        gateway = self._gateway
        captured = {}

        class FakeResident:
            def __init__(self):
                self.last_state_snapshot = {}
                self.last_group_message_id = 0
                self._group_cursor_initialized = False

            @property
            def group_cursor_initialized(self):
                return self._group_cursor_initialized

            def ensure_alive(self, system_text, env):
                return False  # hot turn, but cursor not initialized

            def send_turn(self, content, commit_meta=None):
                captured['content'] = content
                captured['commit_meta'] = commit_meta
                yield ('done', ('ok', '', empty_usage()))

        fake = FakeResident()
        real_fetch = gateway._fetch_group_chat_rows

        def fetch(*, after_id=0, limit=8, cold=False):
            if cold:
                return [], None  # cold query fails
            # 若错误退化为热查询 id>0，会读出 ancient
            return real_fetch(after_id=after_id, limit=limit, cold=False)

        with mock.patch.object(gateway, '_CC_RESIDENT', fake), \
             mock.patch.object(gateway, 'CC_TOKEN', 'tok'), \
             mock.patch.object(gateway, 'CC_CWD', tempfile.mkdtemp()), \
             mock.patch.object(gateway, '_recall_memories', return_value=('', [])), \
             mock.patch('chat.system_builder.build_cc_state', return_value={}), \
             mock.patch('chat.system_builder.build_cc_one_shot', return_value={
                 'wake_nonmessage_background': '', 'wake_message_background': '',
                 'wake_reply_bridge': '', 'wake_ids': [], 'wake_items': [],
                 'task_feedback': '', 'dream_flash': '',
                 'feedback_ids': [], 'dream_id': None,
             }), \
             mock.patch('chat.system_builder.build_cc_cold_once', return_value={}), \
             mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                 'persona': 'STATIC', 'stable_note': '', 'save_instr': '', 'full_system': 'STATIC',
             }), \
             mock.patch.object(gateway, '_fetch_group_chat_rows', side_effect=fetch):
            list(gateway._cc_resident_stream_gen(
                [{'role': 'user', 'content': '你好'}],
                user_turn=True,
            ))

        content = captured.get('content') or ''
        meta = captured.get('commit_meta') or {}
        self.assertNotIn('ancient', content)
        self.assertNotIn('group_cursor_initialized', meta)
        self.assertNotIn('group_max_id', meta)


class HotTurnContentTests(unittest.TestCase):
    """热轮必须实际捕获传给 ResidentSession.send_turn 的 content。"""

    def test_stable_note_and_cold_once_not_in_hot_send_turn(self):
        note = build_stable_note()
        cold_marker = '## 长期事实（这些不会随时间淡忘）'
        captured = {}

        def fake_send_turn(content, commit_meta=None):
            captured['content'] = content
            captured['commit_meta'] = commit_meta
            yield ('done', ('ok', '', empty_usage()))

        class FakeResident:
            def __init__(self):
                self.last_state_snapshot = {
                    'lights': 'main=关 bedside=关',
                    'emotion': '平静',
                }
                self.last_state_send_snapshot = dict(self.last_state_snapshot)
                self.committed_file_hashes = set()
                self.last_group_message_id = 50
                self._group_cursor_initialized = True
                self.generation = 1
                self.tool_surface_snapshot = {}

            @property
            def group_cursor_initialized(self):
                return self._group_cursor_initialized

            def ensure_alive(self, system_text, env):
                return False  # hot

            def peek_idle_seconds(self):
                return None

            def send_turn(self, content, commit_meta=None):
                return fake_send_turn(content, commit_meta=commit_meta)

        gateway = _import_gateway()
        fake = FakeResident()
        with mock.patch.object(gateway, '_CC_RESIDENT', fake), \
             mock.patch.object(gateway, 'CC_TOKEN', 'tok'), \
             mock.patch.object(gateway, 'CC_CWD', tempfile.mkdtemp()), \
             mock.patch.object(gateway, '_recall_memories', return_value=('', [])), \
             mock.patch('chat.system_builder.build_cc_state', return_value={
                 'lights': 'main=开 bedside=关',
                 'emotion': '平静',
             }), \
             mock.patch('chat.system_builder.build_cc_one_shot', return_value={
                 'wake_nonmessage_background': '', 'wake_message_background': '',
                 'wake_reply_bridge': '', 'wake_ids': [], 'wake_items': [],
                 'task_feedback': '', 'dream_flash': '',
                 'feedback_ids': [], 'dream_id': None,
             }), \
             mock.patch('chat.system_builder.build_cc_cold_once') as cold_builder, \
             mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                 'persona': 'STATIC', 'stable_note': '', 'save_instr': '', 'full_system': 'STATIC',
             }), \
             mock.patch.object(gateway, '_fetch_group_chat_rows', return_value=([], 0)):
            list(gateway._cc_resident_stream_gen(
                [{'role': 'user', 'content': '你好热轮'}],
                user_turn=True,
            ))
            cold_builder.assert_not_called()

        content = captured.get('content') or ''
        if isinstance(content, list):
            content = ' '.join(
                b.get('text', '') for b in content if isinstance(b, dict)
            )
        self.assertIn('你好热轮', content)
        self.assertNotIn(note[:40], content)
        self.assertNotIn(cold_marker, content)
        self.assertNotIn('【当前状态】', content)
        self.assertIn('【此刻有一点变化】', content)
        self.assertIn('主灯亮着', content)

    def test_relationship_is_dynamic_tail_and_static_system_is_unchanged(self):
        from chat.relationship_context import RelationshipContextResult

        captured = {}
        relationship_text = '【近期关系脉络】\n当前基调：低唤醒、偏暖。'

        class FakeResident:
            last_state_snapshot = {}
            last_state_send_snapshot = {}
            committed_file_hashes = set()
            last_group_message_id = 0
            group_cursor_initialized = True
            last_rel_fingerprint = 'rel-v2:old'
            turns_since_rel_sent = 1
            last_rel_mood = '低唤醒|偏暖'

            def ensure_alive(self, system_text, env):
                captured['system'] = system_text
                return False

            def peek_idle_seconds(self):
                return 30.0

            def send_turn(self, content, commit_meta=None):
                captured['content'] = content
                captured['commit_meta'] = commit_meta
                yield ('done', ('ok', '', empty_usage()))

        relationship = RelationshipContextResult(
            text=relationship_text,
            fingerprint='rel-v2:new',
            sources={'anchor': 'ok', 'daily': 'ok', 'mood': 'ok'},
            mood_key='低唤醒|偏暖',
        )
        gateway = _import_gateway()
        with mock.patch.object(gateway, '_CC_RESIDENT', FakeResident()), \
             mock.patch.object(gateway, 'CC_TOKEN', 'tok'), \
             mock.patch.object(gateway, 'CC_CWD', tempfile.mkdtemp()), \
             mock.patch.object(gateway, '_recall_memories', return_value=('', [])), \
             mock.patch.object(
                 gateway.config_store, 'get_bool',
                 side_effect=lambda key, default=False: key == 'RELATIONSHIP_CONTEXT_ENABLED',
             ), \
             mock.patch(
                 'chat.relationship_context.build_relationship_context',
                 return_value=relationship,
             ), \
             mock.patch(
                 'chat.relationship_context.should_send_relationship',
                 return_value=True,
             ), \
             mock.patch('chat.system_builder.build_cc_state', return_value={}), \
             mock.patch('chat.system_builder.build_cc_one_shot', return_value={
                 'wake_nonmessage_background': '', 'wake_message_background': '',
                 'wake_reply_bridge': '', 'wake_ids': [], 'wake_items': [],
                 'task_feedback': '', 'dream_flash': '',
                 'feedback_ids': [], 'dream_id': None,
             }), \
             mock.patch('chat.system_builder.build_cc_cold_once', return_value={}), \
             mock.patch('chat.system_builder.build_cc_static_parts', return_value={
                 'persona': 'STATIC', 'stable_note': '', 'save_instr': '',
                 'full_system': 'STATIC',
             }), \
             mock.patch.object(gateway, '_fetch_group_chat_rows', return_value=([], 0)):
            events = list(gateway._cc_resident_stream_gen(
                [{'role': 'user', 'content': '现在继续'}], user_turn=True,
            ))

        self.assertEqual(captured['system'], 'STATIC')
        content = captured['content']
        self.assertIn(relationship_text, content)
        self.assertLess(content.index(relationship_text), content.index('现在继续'))
        meta = captured['commit_meta']
        self.assertEqual(meta['rel_fingerprint'], 'rel-v2:new')
        self.assertEqual(meta.get('rel_mood'), '低唤醒|偏暖')
        self.assertFalse(meta.get('rel_tick'))
        done_usage = events[-1][1][2]
        self.assertEqual(done_usage.get('rel_context'), 'sent')
        self.assertEqual(done_usage.get('rel_sources'), {
            'anchor': 'ok', 'daily': 'ok', 'mood': 'ok',
        })


class LegacyPerceptionClassificationTests(unittest.TestCase):
    """旧动态感知组件进入正确的结构化分类。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, type TEXT, layer TEXT, tags TEXT,
                content TEXT, resolved INTEGER DEFAULT 0, importance INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE dream_events (
                id INTEGER PRIMARY KEY, type TEXT, value TEXT,
                created_at TEXT, duration_minutes INTEGER
            );
            CREATE TABLE dream_pool (
                id INTEGER PRIMARY KEY, content TEXT, tone TEXT,
                surfaced INTEGER DEFAULT 0, surface_count INTEGER DEFAULT 0,
                created_at TEXT, surfaced_at TEXT
            );
            CREATE TABLE period_records (
                id INTEGER PRIMARY KEY, type TEXT, date TEXT, note TEXT
            );
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY, amount REAL, category TEXT, date TEXT
            );
            CREATE TABLE ledger_budget (
                month TEXT PRIMARY KEY, amount REAL
            );
            CREATE TABLE todos (
                id INTEGER PRIMARY KEY, content TEXT, due_date TEXT, done INTEGER DEFAULT 0
            );
            CREATE TABLE countdowns (
                id INTEGER PRIMARY KEY, title TEXT, target_date TEXT, emoji TEXT, type TEXT
            );
            CREATE TABLE board (
                id INTEGER PRIMARY KEY, author TEXT, tag TEXT, content TEXT,
                level TEXT, category TEXT, status TEXT
            );
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, woke_at TEXT, action TEXT,
                content TEXT, thoughts TEXT, consumed INTEGER DEFAULT 0
            );
            """
        )
        now = (datetime.datetime.utcnow() + datetime.timedelta(hours=8))
        now_s = now.strftime('%Y-%m-%d %H:%M:%S')
        month = now.strftime('%Y-%m')
        # recent activity
        conn.execute(
            "INSERT INTO dream_events (type, value, created_at, duration_minutes) "
            "VALUES ('app', '刷微博', ?, 5)",
            (now_s,),
        )
        # web memo
        conn.execute(
            "INSERT INTO posts (type, tags, content, resolved, created_at) "
            "VALUES ('MEMORY', 'memo,网页窗口', '[网页窗口] 她：你好', 0, ?)",
            (now_s,),
        )
        # dream one-shot candidate
        conn.execute(
            "INSERT INTO dream_pool (content, tone, surfaced, surface_count, created_at) "
            "VALUES ('一段旧梦', 'soft', 0, 0, ?)",
            (now_s,),
        )
        # period late by >=3 days (last period 40 days ago, cycle ~28)
        late = (now.date() - datetime.timedelta(days=40)).strftime('%Y-%m-%d')
        earlier = (now.date() - datetime.timedelta(days=68)).strftime('%Y-%m-%d')
        conn.execute(
            "INSERT INTO period_records (type, date) VALUES ('period', ?)", (earlier,)
        )
        conn.execute(
            "INSERT INTO period_records (type, date) VALUES ('period', ?)", (late,)
        )
        # budget warning >=80%
        conn.execute(
            "INSERT INTO ledger_budget (month, amount) VALUES (?, 1000)", (month,)
        )
        conn.execute(
            "INSERT INTO ledger (amount, category, date) VALUES (-900, '吃', ?)",
            (month + '-01',),
        )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self.get_db = get_db

    def tearDown(self):
        self.tmp.cleanup()

    def test_components_land_in_correct_buckets(self):
        with mock.patch('chat.system_builder._ombre_handoff_sync', return_value=''), \
             mock.patch('urllib.request.urlopen', side_effect=OSError('no light')), \
             mock.patch('config_store.get_bool', return_value=False), \
             mock.patch('random.random', return_value=0.0):
            state = _cc_collect_state(self.get_db)
            cold = _cc_collect_cold_once(self.get_db)
            one_shot = _cc_collect_one_shot(self.get_db, include_wake=False)

        self.assertIn('哈娅最近的活动', state.get('recent_activity', ''))
        self.assertIn('刷微博', state.get('recent_activity', ''))
        self.assertIn('经期预测', state.get('reminders', ''))
        self.assertIn('本月预算已用', state.get('reminders', ''))

        self.assertIn('网页窗口', cold.get('web_memo', ''))
        self.assertNotIn('忽然想起来', cold.get('web_memo', ''))

        self.assertIn('忽然想起来', one_shot.get('dream_flash', ''))
        # peek only — not consumed yet
        conn = self.get_db()
        row = conn.execute('SELECT surfaced FROM dream_pool WHERE id=1').fetchone()
        conn.close()
        self.assertEqual(row['surfaced'], 0)

        text = format_one_shot(one_shot)
        self.assertNotIn('feedback_ids', text)
        self.assertNotIn("'dream_id'", text)


if __name__ == '__main__':
    unittest.main()
