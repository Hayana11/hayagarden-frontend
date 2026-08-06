"""R0 rewrite native fork: resolver, fallback, parent immutability, trial lifecycle."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chat import daily_context as dc
from chat import rewrite_native_fork as rnf
from chat import rewrite_staging


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RewriteNativeForkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 't.db')
        self.cwd = os.path.join(self.tmp.name, 'proj')
        self.claude_home = os.path.join(self.tmp.name, 'claude')
        os.makedirs(self.cwd, exist_ok=True)
        os.makedirs(self.claude_home, exist_ok=True)
        dc.ensure_schema(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            '''CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT, content TEXT, thinking TEXT DEFAULT '',
                tool_calls TEXT DEFAULT '', branches TEXT DEFAULT '',
                cache_info TEXT DEFAULT '', choices TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )'''
        )
        rewrite_staging.ensure_schema(self.conn)
        self.conn.commit()
        self._flag_patch = mock.patch('config_store.get_bool', side_effect=self._get_bool)
        self._flag_on = True
        self._flag_patch.start()
        self.addCleanup(self._flag_patch.stop)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _get_bool(self, key, default=False):
        if key == rnf.FLAG_KEY:
            return self._flag_on
        return default

    def _insert_msg(self, author: str, content: str) -> int:
        cur = self.conn.execute(
            'INSERT INTO chat_messages (author, content) VALUES (?, ?)',
            (author, content),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _map(
        self,
        *,
        event_uuid: str,
        message_id: int,
        role: str,
        session: str,
        offset: int = 0,
        context_id: int = 1,
        epoch: int = 1,
        gen: int = 1,
    ) -> None:
        self.conn.execute(
            '''INSERT INTO chat_message_claude_events (
                event_uuid, message_id, role, claude_session_id,
                context_id, context_epoch, resident_generation, jsonl_byte_offset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (event_uuid, message_id, role, session, context_id, epoch, gen, offset),
        )
        self.conn.commit()

    def _write_parent(self, session: str, body: bytes = b'{"type":"user"}\n') -> Path:
        from tools.cc_jsonl_usage import session_jsonl_path
        path = session_jsonl_path(self.cwd, session, claude_home=self.claude_home)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def _chain_h_a0_u1_a1(self, session='sid-parent'):
        h = self._insert_msg('hayana', 'H')
        a0 = self._insert_msg('assistant', 'A0')
        u1 = self._insert_msg('hayana', 'U1')
        a1 = self._insert_msg('assistant', 'A1')
        self._map(event_uuid='ev-h', message_id=h, role='user', session=session, offset=10)
        self._map(event_uuid='ev-a0', message_id=a0, role='assistant', session=session, offset=20)
        self._map(event_uuid='ev-u1', message_id=u1, role='user', session=session, offset=30)
        self._map(event_uuid='ev-a1', message_id=a1, role='assistant', session=session, offset=40)
        self._write_parent(session)
        return h, a0, u1, a1

    # T1
    def test_regen_resolves_previous_assistant_boundary(self):
        _h, a0, u1, a1 = self._chain_h_a0_u1_a1()
        staging = {
            'operation': 'regen',
            'source_message_id': a1,
            'user_message_id': u1,
        }
        plan = rnf.resolve_rewrite_native_fork(
            self.conn, staging, cwd=self.cwd, claude_home=self.claude_home,
        )
        self.assertTrue(plan.eligible)
        self.assertEqual(plan.fork_event_uuid, 'ev-a0')
        self.assertEqual(plan.resend_content, 'U1')
        self.assertEqual(plan.boundary_assistant_message_id, a0)
        self.assertEqual(plan.mapping_provenance.get('boundary_message_id'), a0)
        self.assertEqual(plan.tool_profile, 'text_only')
        self.assertEqual(plan.static_system_kind, 'daily')

    # T2
    def test_edit_resolves_previous_assistant_boundary(self):
        _h, a0, u1, _a1 = self._chain_h_a0_u1_a1()
        staging = {
            'operation': 'edit',
            'source_message_id': u1,
            'user_message_id': u1,
            'edited_content': "U1'",
        }
        plan = rnf.resolve_rewrite_native_fork(
            self.conn, staging, cwd=self.cwd, claude_home=self.claude_home,
        )
        self.assertTrue(plan.eligible)
        self.assertEqual(plan.fork_event_uuid, 'ev-a0')
        self.assertEqual(plan.resend_content, "U1'")
        self.assertEqual(plan.boundary_assistant_message_id, a0)
        self.assertEqual(plan.mapping_provenance.get('boundary_message_id'), a0)

    def test_partial_mapping_gap_does_not_skip_to_earlier_assistant(self):
        """A0 unmapped must cold-fallback even if an earlier assistant is mapped."""
        a_prev = self._insert_msg('assistant', 'A-1')
        u0 = self._insert_msg('hayana', 'U0')
        a0 = self._insert_msg('assistant', 'A0')
        u1 = self._insert_msg('hayana', 'U1')
        a1 = self._insert_msg('assistant', 'A1')
        sid = 'sid-gap'
        self._map(event_uuid='ev-a-1', message_id=a_prev, role='assistant', session=sid, offset=10)
        self._map(event_uuid='ev-u0', message_id=u0, role='user', session=sid, offset=20)
        # Intentionally no mapping for exact A0.
        self._map(event_uuid='ev-u1', message_id=u1, role='user', session=sid, offset=40)
        self._map(event_uuid='ev-a1', message_id=a1, role='assistant', session=sid, offset=50)
        self._write_parent(sid)
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, rnf.REASON_MAPPING_GAP)
        self.assertEqual(plan.mapping_provenance.get('boundary_assistant_message_id'), a0)
        self.assertNotEqual(plan.fork_event_uuid, 'ev-a-1')

    # T3
    def test_no_previous_assistant_ineligible(self):
        u1 = self._insert_msg('hayana', 'U1')
        a1 = self._insert_msg('assistant', 'A1')
        sid = 'sid-first'
        self._map(event_uuid='ev-u1', message_id=u1, role='user', session=sid, offset=1)
        self._map(event_uuid='ev-a1', message_id=a1, role='assistant', session=sid, offset=2)
        self._write_parent(sid)
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, rnf.REASON_NO_SAFE_PRE_USER_BOUNDARY)

    # T4
    def test_missing_mapping_fallback(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()
        self.conn.execute('DELETE FROM chat_message_claude_events')
        self.conn.commit()
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, rnf.REASON_MAPPING_MISSING)

    # T5
    def test_ambiguous_boundary_offset_tie_refuses(self):
        _h, a0, u1, a1 = self._chain_h_a0_u1_a1()
        # Second assistant event at same message + same offset → refuse.
        self._map(
            event_uuid='ev-a0-b',
            message_id=a0,
            role='assistant',
            session='sid-parent',
            offset=20,
        )
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, rnf.REASON_MAPPING_AMBIGUOUS)

    # T6
    def test_session_mismatch_fallback(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1(session='sid-a')
        # Conflicting session stamped on the same DB user message → refuse.
        self._map(
            event_uuid='ev-u1-conflict',
            message_id=u1,
            role='tool_use',
            session='sid-b',
            offset=31,
        )
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, rnf.REASON_SESSION_MISMATCH)

    # T7
    def test_parent_immutable_across_fork(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertTrue(plan.eligible)
        parent = Path(plan.parent_transcript_path)
        before = parent.read_bytes()

        class _Result:
            session_id = 'sid-child'

        def _fake_fork(session_id, directory=None, up_to_message_id=None, title=None):
            # Official contract: parent unchanged; write child elsewhere.
            from tools.cc_jsonl_usage import session_jsonl_path
            child = session_jsonl_path(self.cwd, 'sid-child', claude_home=self.claude_home)
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(before[: max(1, len(before) // 2)] or b'{}\n')
            self.assertEqual(parent.read_bytes(), before)
            self.assertEqual(up_to_message_id, 'ev-a0')
            self.assertEqual(session_id, 'sid-parent')
            return _Result()

        exe = rnf.execute_native_fork(
            plan,
            cwd=self.cwd,
            claude_home=self.claude_home,
            fork_session_fn=_fake_fork,
        )
        self.assertTrue(exe.ok)
        self.assertEqual(exe.parent_sha256_before, exe.parent_sha256_after)
        self.assertEqual(exe.parent_sha256_before, _sha(before))
        self.assertEqual(parent.read_bytes(), before)

    def test_parent_mutation_refuses(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )

        class _Result:
            session_id = 'sid-child'

        def _mutating_fork(session_id, directory=None, up_to_message_id=None, title=None):
            Path(plan.parent_transcript_path).write_bytes(b'mutated\n')
            from tools.cc_jsonl_usage import session_jsonl_path
            child = session_jsonl_path(self.cwd, 'sid-child', claude_home=self.claude_home)
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(b'child\n')
            return _Result()

        exe = rnf.execute_native_fork(
            plan,
            cwd=self.cwd,
            claude_home=self.claude_home,
            fork_session_fn=_mutating_fork,
        )
        self.assertFalse(exe.ok)
        self.assertEqual(exe.reason, rnf.REASON_PARENT_MUTATED)

    # T8
    def test_child_resume_failure_falls_back(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()
        staging = {
            'operation': 'regen',
            'source_message_id': a1,
            'user_message_id': u1,
        }

        class _Result:
            session_id = 'sid-child'

        def _fake_fork(session_id, directory=None, up_to_message_id=None, title=None):
            from tools.cc_jsonl_usage import session_jsonl_path
            child = session_jsonl_path(self.cwd, 'sid-child', claude_home=self.claude_home)
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(b'child\n')
            return _Result()

        class BoomResident:
            def spawn_resumable(self, *a, **k):
                raise RuntimeError('spawn_resumable failure')

            def invalidate_for_history_rewrite(self, reason='x'):
                self.killed = reason

        resident = BoomResident()
        ok, meta = rnf.try_prepare_native_trial_resident(
            staging=staging,
            conn=self.conn,
            cwd=self.cwd,
            system_text='sys',
            env={},
            resident=resident,
            claude_home=self.claude_home,
            fork_session_fn=_fake_fork,
        )
        self.assertFalse(ok)
        self.assertEqual(meta.get('rewrite_cache_mode'), rnf.MODE_COLD)
        self.assertEqual(meta.get('rewrite_cache_fallback_reason'), rnf.REASON_CHILD_RESUME_FAILED)
        self.assertTrue(getattr(resident, 'killed', None))

    # T9 / T10 — staging contract remains: native prep does not touch chat_messages tip
    def test_native_prep_does_not_mutate_active_transcript(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()
        before = [
            dict(r)
            for r in self.conn.execute(
                'SELECT id, author, content FROM chat_messages ORDER BY id'
            ).fetchall()
        ]
        staging = rewrite_staging.prepare_regen(self.conn, source_assistant_id=a1)
        self.conn.commit()
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {
                'operation': 'regen',
                'source_message_id': a1,
                'user_message_id': staging['user_message_id'],
            },
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertTrue(plan.eligible)

        class _Result:
            session_id = 'sid-child'

        def _fake_fork(session_id, directory=None, up_to_message_id=None, title=None):
            from tools.cc_jsonl_usage import session_jsonl_path
            child = session_jsonl_path(self.cwd, 'sid-child', claude_home=self.claude_home)
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(b'child\n')
            return _Result()

        class OkResident:
            def __init__(self):
                self.spawned = None
                self.tool_profile = None
                self.system_text = None

            def spawn_resumable(self, system_text, env, *, resume_session_id, tool_profile='legacy', reason='x'):
                self.spawned = resume_session_id
                self.tool_profile = tool_profile
                self.system_text = system_text
                return self

            def wait_staged_health(self, **kwargs):
                return None

            def invalidate_for_history_rewrite(self, reason='x'):
                pass

        resident = OkResident()
        with mock.patch(
            'chat.system_builder.build_cc_daily_static_parts',
            return_value={'full_system': 'DAILY_STATIC_PROBE'},
        ):
            ok, meta = rnf.try_prepare_native_trial_resident(
                staging={
                    'operation': 'regen',
                    'source_message_id': a1,
                    'user_message_id': staging['user_message_id'],
                },
                conn=self.conn,
                cwd=self.cwd,
                system_text='CLASSIC_STATIC_SHOULD_NOT_WIN',
                env={},
                resident=resident,
                claude_home=self.claude_home,
                fork_session_fn=_fake_fork,
            )
        self.assertTrue(ok)
        self.assertEqual(meta.get('rewrite_cache_mode'), rnf.MODE_NATIVE)
        self.assertEqual(resident.tool_profile, 'text_only')
        self.assertEqual(resident.system_text, 'DAILY_STATIC_PROBE')
        self.assertEqual(meta.get('rewrite_cache_tool_profile'), 'text_only')
        after = [
            dict(r)
            for r in self.conn.execute(
                'SELECT id, author, content FROM chat_messages ORDER BY id'
            ).fetchall()
        ]
        self.assertEqual(before, after)
        # Candidate still only in staging, not active assistant insert.
        tip = self.conn.execute(
            "SELECT id FROM chat_messages WHERE author IN ('assistant','fyodor') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(int(tip['id']), a1)

    # T11
    def test_trial_child_discard_on_failure_path(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()

        class _Result:
            session_id = 'sid-child'

        def _fake_fork(session_id, directory=None, up_to_message_id=None, title=None):
            from tools.cc_jsonl_usage import session_jsonl_path
            child = session_jsonl_path(self.cwd, 'sid-child', claude_home=self.claude_home)
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(b'child\n')
            return _Result()

        class HealthFailResident:
            def __init__(self):
                self.invalidated = None

            def spawn_resumable(self, *a, **k):
                return self

            def wait_staged_health(self, **kwargs):
                raise RuntimeError('staged_exited_during_health_window')

            def invalidate_for_history_rewrite(self, reason='x'):
                self.invalidated = reason

        resident = HealthFailResident()
        ok, meta = rnf.try_prepare_native_trial_resident(
            staging={'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            conn=self.conn,
            cwd=self.cwd,
            system_text='sys',
            env={},
            resident=resident,
            claude_home=self.claude_home,
            fork_session_fn=_fake_fork,
        )
        self.assertFalse(ok)
        self.assertEqual(meta['rewrite_cache_fallback_reason'], rnf.REASON_CHILD_HEALTH_FAILED)
        self.assertEqual(resident.invalidated, 'rewrite_native_fork_resume_failed')

    # T12 — flag off keeps cold equivalence
    def test_flag_off_always_cold(self):
        self._flag_on = False
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()
        plan = rnf.resolve_rewrite_native_fork(
            self.conn,
            {'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, rnf.REASON_FLAG_OFF)
        self.assertEqual(rnf.FLAG_KEY, 'CC_REWRITE_NATIVE_FORK_ENABLED')
        import config_store
        self.assertEqual(config_store._DEFAULTS.get(rnf.FLAG_KEY), '0')

    def test_observability_never_claims_cache_hit(self):
        plan = rnf.NativeForkPlan(
            eligible=True,
            reason='',
            parent_session_id='abc',
            fork_event_uuid='def',
            tool_profile='text_only',
            static_system_kind='daily',
        )
        obs = plan.observability()
        self.assertNotIn('cache_read', obs)
        self.assertEqual(obs['rewrite_cache_mode'], rnf.MODE_NATIVE)
        self.assertTrue(obs['rewrite_cache_parent_session_hash'])
        self.assertNotEqual(obs['rewrite_cache_parent_session_hash'], 'abc')
        self.assertEqual(obs['rewrite_cache_tool_profile'], 'text_only')

    def test_legacy_tool_profile_override_rejected(self):
        _h, _a0, u1, a1 = self._chain_h_a0_u1_a1()

        class _Result:
            session_id = 'sid-child'

        def _fake_fork(session_id, directory=None, up_to_message_id=None, title=None):
            from tools.cc_jsonl_usage import session_jsonl_path
            child = session_jsonl_path(self.cwd, 'sid-child', claude_home=self.claude_home)
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(b'child\n')
            return _Result()

        class GuardResident:
            def spawn_resumable(self, *a, **k):
                raise AssertionError('must not spawn with mismatched profile')

            def invalidate_for_history_rewrite(self, reason='x'):
                pass

        ok, meta = rnf.try_prepare_native_trial_resident(
            staging={'operation': 'regen', 'source_message_id': a1, 'user_message_id': u1},
            conn=self.conn,
            cwd=self.cwd,
            system_text='sys',
            env={},
            resident=GuardResident(),
            claude_home=self.claude_home,
            fork_session_fn=_fake_fork,
            tool_profile='legacy',
        )
        self.assertFalse(ok)
        self.assertEqual(meta.get('rewrite_cache_fallback_reason'), rnf.REASON_PROFILE_UNSUPPORTED)


if __name__ == '__main__':
    unittest.main()
