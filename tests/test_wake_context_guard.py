"""A1.1 Wake relationship injection + idle guard (no model calls)."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-a11-config.db'),
)

from chat.interaction_state import read_interaction_clock, wake_guard_reason
from wake.usage import build_wake_cache_info


class WakeRelationshipInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'sys.db')
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
            CREATE TABLE board (id INTEGER PRIMARY KEY, content TEXT, status TEXT);
            CREATE TABLE todos (
                id INTEGER PRIMARY KEY, content TEXT, due_date TEXT, done INTEGER DEFAULT 0
            );
            CREATE TABLE dream_events (
                id INTEGER PRIMARY KEY, type TEXT, value TEXT, created_at TEXT
            );
            CREATE TABLE dream_pool (
                id INTEGER PRIMARY KEY, content TEXT, tone TEXT, surfaced INTEGER DEFAULT 0
            );
            CREATE TABLE period_records (
                id INTEGER PRIMARY KEY, type TEXT, date TEXT, note TEXT
            );
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY, amount REAL, category TEXT, date TEXT
            );
            CREATE TABLE ledger_budget (id INTEGER PRIMARY KEY, amount REAL);
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, content TEXT, consumed INTEGER DEFAULT 0,
                woke_at TEXT
            );
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

    def _patch_build(self, shared, get_bool):
        from chat import system_builder
        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        return mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
            mock.patch.object(system_builder, 'build_shared_context', return_value=shared), \
            mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
            mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
            mock.patch.object(system_builder.config_store, 'get_bool', side_effect=get_bool)

    def test_normal_wake_includes_relationship_when_enabled(self):
        from chat import system_builder

        rel_text = '【近期关系脉络】\n我们是长期伴侣。'
        shared = types.SimpleNamespace(
            persona='PERSONA',
            relationship_context=rel_text,
            relationship_fingerprint='rel-v2:x',
        )

        def get_bool(key, default=False):
            return key in (
                'RELATIONSHIP_CONTEXT_ENABLED',
                'WAKE_RELATIONSHIP_CONTEXT_ENABLED',
            )

        patches = self._patch_build(shared, get_bool)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            blocks = system_builder.build_system(
                wake=True, include_relationship_context=True,
            )
        flat = '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))
        self.assertIn('【近期关系脉络】', flat)

    def test_dream_and_summarize_exclude_relationship(self):
        from chat import system_builder

        shared = types.SimpleNamespace(
            persona='PERSONA',
            relationship_context='【近期关系脉络】\n我们是长期伴侣。',
            relationship_fingerprint='rel-v2:x',
        )

        def get_bool(key, default=False):
            return key in (
                'RELATIONSHIP_CONTEXT_ENABLED',
                'WAKE_RELATIONSHIP_CONTEXT_ENABLED',
            )

        patches = self._patch_build(shared, get_bool)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            blocks = system_builder.build_system(
                wake=True, include_relationship_context=False,
            )
        flat = '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))
        self.assertNotIn('【近期关系脉络】', flat)

    def test_wake_relationship_rollback_switch(self):
        from chat import system_builder

        shared = types.SimpleNamespace(
            persona='PERSONA',
            relationship_context='【近期关系脉络】\nx',
            relationship_fingerprint='rel-v2:x',
        )

        def get_bool(key, default=False):
            if key == 'RELATIONSHIP_CONTEXT_ENABLED':
                return True
            if key == 'WAKE_RELATIONSHIP_CONTEXT_ENABLED':
                return False
            return default

        patches = self._patch_build(shared, get_bool)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            blocks = system_builder.build_system(
                wake=True, include_relationship_context=True,
            )
        flat = '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))
        self.assertNotIn('【近期关系脉络】', flat)


class WakeGuardEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, content TEXT, thoughts TEXT,
                woke_at TEXT, consumed INTEGER DEFAULT 0
            );
            """
        )
        now = datetime.datetime(2026, 7, 20, 12, 0, 0)
        recent = (now - datetime.timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('hayana', '刚聊过', recent),
        )
        conn.commit()
        conn.close()
        self.now = now

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_recent_interaction_skips_without_wake_log(self):
        clock = read_interaction_clock(self.get_db, now=self.now)
        reason = wake_guard_reason(clock, mode='normal', min_idle_minutes=30)
        self.assertEqual(reason, 'recent_interaction')
        # Ensure no wake_log rows were created by the guard itself.
        conn = self.get_db()
        n = conn.execute('SELECT COUNT(*) FROM wake_log').fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_scheduler_skips_before_dice_when_recent(self):
        from tools import dream_wake

        with mock.patch.object(dream_wake, '_db', self.get_db), \
             mock.patch.object(dream_wake, '_now', return_value=self.now), \
             mock.patch.object(dream_wake, '_in_active_hours', return_value=True), \
             mock.patch.object(dream_wake, 'run_self_triggers', return_value=False), \
             mock.patch.object(dream_wake, '_call_wake') as call_wake, \
             mock.patch.object(dream_wake, '_log') as log:
            dream_wake.run()
        call_wake.assert_not_called()
        joined = ' '.join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn('recent_interaction', joined)

    def test_usage_marks_source_provider_mode(self):
        payload = build_wake_cache_info(
            [{
                'index': 1, 'complete': True, 'input_tokens': 1, 'output_tokens': 1,
                'cache_read': 0, 'cache_creation': 0, 'cache_creation_5m': 0,
                'cache_creation_1h': 0, 'context_tokens': 1,
            }],
            elapsed_sec=1.0,
            cache_supported=True,
            mode='normal',
            model='x',
            payload_builder=lambda **kw: dict(kw),
        )
        self.assertEqual(payload['source'], 'wake')
        self.assertEqual(payload['provider'], 'api_relay')
        self.assertEqual(payload['mode'], 'normal')


class InjectSnippetOverrideTests(unittest.TestCase):
    def test_inject_passes_override_to_desire(self):
        from wake.builder import inject_snippets

        seen = {}

        class Desire:
            @staticmethod
            def get_longing_wake_fact(t_hours_override=None):
                seen['t'] = t_hours_override
                return 'longing snip'

            @staticmethod
            def get_wake_snippet(t_hours_override=None):
                seen['bad'] = True
                return 'MUST_NOT_INJECT'

        class Drive:
            @staticmethod
            def decide():
                return {
                    'fired': None, 'action': 'none', 'hint': '',
                    'blocked': False, 'drive': {}, 'contributors': [],
                }

            @staticmethod
            def freeze_decision_provenance(decision=None):
                return {
                    'source': 'drive_engine.decide',
                    'captured_at': '2026-08-04 12:00:00',
                    'primary_drive': None,
                    'contributors': [],
                    'blocked': False,
                    'suggested_action': 'none',
                }

            @staticmethod
            def get_wake_snippet(decision=None):
                return ''

        with mock.patch.dict(sys.modules, {'desire': Desire, 'drive_engine': Drive}):
            _system, _prov = inject_snippets(
                'base', 'normal', longing_enabled=True, t_hours_override=0.17,
            )
        self.assertAlmostEqual(seen['t'], 0.17)


if __name__ == '__main__':
    unittest.main()
