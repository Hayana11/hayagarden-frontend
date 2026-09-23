"""MODEL-1B: CC_CHAT_MODEL helper + spawn argv wiring (pure, no production DB)."""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ['HAYAGARDEN_CONFIG_DB_PATH'] = str(
    Path(tempfile.gettempdir()) / 'hayagarden-test-cc-model-runtime.db'
)

from chat import cc_model as cc_model_module  # noqa: E402
from chat.cc_model import (  # noqa: E402
    CC_MODEL_CATALOG,
    CC_MODEL_NOT_ALLOWED,
    cc_model_args,
    cc_model_identity,
    cc_model_mode,
    cc_model_snapshot,
    describe_cc_model_state,
    get_cc_chat_model,
    is_allowed_cc_model,
    set_cc_chat_model,
)
from chat.model_state import describe_chat_model_state  # noqa: E402
import cc_resident  # noqa: E402
import config_store  # noqa: E402


def fake_get(values):
    def _get(key, default=None):
        return values.get(key, default)
    return _get


class CcModelHelperTests(unittest.TestCase):
    def test_default_empty(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
            self.assertEqual(get_cc_chat_model(), '')
            self.assertEqual(cc_model_mode(), 'default')
            self.assertEqual(cc_model_identity(), 'default')
            self.assertEqual(cc_model_args(), [])
            state = describe_cc_model_state()
            self.assertEqual(state['model_mode'], 'default')
            self.assertIsNone(state['configured_model'])

    def test_explicit_args(self):
        with mock.patch.object(
            config_store, 'get',
            side_effect=fake_get({'CC_CHAT_MODEL': 'claude-sonnet-5'}),
        ):
            self.assertEqual(get_cc_chat_model(), 'claude-sonnet-5')
            self.assertEqual(cc_model_mode(), 'explicit')
            self.assertEqual(cc_model_identity(), 'explicit:claude-sonnet-5')
            self.assertEqual(cc_model_args(), ['--model', 'claude-sonnet-5'])
            state = describe_cc_model_state()
            self.assertEqual(state['configured_model'], 'claude-sonnet-5')

    def test_whitespace_treated_as_default(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': '  '})):
            self.assertEqual(cc_model_args(), [])
            self.assertEqual(cc_model_identity(), 'default')

    def test_set_cc_chat_model_roundtrip(self):
        store = {'CC_CHAT_MODEL': ''}

        def _get(key, default=None):
            return store.get(key, default)

        def _set(key, value):
            store[key] = value

        with mock.patch.object(config_store, 'get', side_effect=_get), \
             mock.patch.object(config_store, 'set', side_effect=_set), \
             mock.patch.object(cc_model_module, '_active_runtime_version_for_catalog', return_value='2.1.280'):
            out = set_cc_chat_model('claude-opus-4-8')
            self.assertEqual(store['CC_CHAT_MODEL'], 'claude-opus-4-8')
            self.assertEqual(out['model_mode'], 'explicit')
            self.assertEqual(out['effective_from'], 'next_turn')
            cleared = set_cc_chat_model(None)
            self.assertEqual(store['CC_CHAT_MODEL'], '')
            self.assertEqual(cleared['model_mode'], 'default')
            self.assertIsNone(cleared['configured_model'])

    def test_relay_alias_rejected_without_write(self):
        """Relay-style aliases must never enter CC_CHAT_MODEL / --model."""
        store = {'CC_CHAT_MODEL': 'claude-sonnet-5'}
        writes = []

        def _get(key, default=None):
            return store.get(key, default)

        def _set(key, value):
            writes.append((key, value))
            store[key] = value

        alias = '[反重力量] claude-opus-4-6-thinking [不补]'
        self.assertFalse(is_allowed_cc_model(alias))
        with mock.patch.object(config_store, 'get', side_effect=_get), \
             mock.patch.object(config_store, 'set', side_effect=_set):
            out = set_cc_chat_model(alias)
        self.assertEqual(out['ok'], False)
        self.assertEqual(out['error'], CC_MODEL_NOT_ALLOWED)
        self.assertEqual(out['rejected_model'], alias)
        self.assertEqual(store['CC_CHAT_MODEL'], 'claude-sonnet-5')
        self.assertEqual(writes, [])
        self.assertEqual(out['configured_model'], 'claude-sonnet-5')

    def test_empty_clears_to_default_without_catalog(self):
        store = {'CC_CHAT_MODEL': 'claude-sonnet-5'}

        def _get(key, default=None):
            return store.get(key, default)

        def _set(key, value):
            store[key] = value

        with mock.patch.object(config_store, 'get', side_effect=_get), \
             mock.patch.object(config_store, 'set', side_effect=_set):
            out = set_cc_chat_model('')
        self.assertTrue(out.get('ok'))
        self.assertEqual(store['CC_CHAT_MODEL'], '')
        self.assertEqual(out['model_mode'], 'default')

    def test_never_reads_relay_keys(self):
        """CC helper must ignore MODEL / ACTIVE_RELAY."""
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CC_CHAT_MODEL': '',
            'MODEL': 'relay-should-not-leak',
            'ACTIVE_RELAY': '9',
        })) as get_mock:
            self.assertEqual(cc_model_args(), [])
            keys = [c.args[0] for c in get_mock.call_args_list]
            self.assertIn('CC_CHAT_MODEL', keys)
            self.assertNotIn('MODEL', keys)
            self.assertNotIn('ACTIVE_RELAY', keys)

    def test_catalog_uses_official_ids(self):
        ids = {row['id'] for row in CC_MODEL_CATALOG}
        self.assertIn('claude-sonnet-5', ids)
        self.assertIn('claude-opus-5', ids)
        self.assertIn('claude-opus-4-8', ids)
        for mid in ids:
            self.assertFalse(mid.startswith('['), mid)

    def test_describe_chat_model_state_cc_reads_cc_chat_model(self):
        with mock.patch.object(
            config_store, 'get',
            side_effect=fake_get({'CC_CHAT_MODEL': 'claude-sonnet-5'}),
        ):
            payload = describe_chat_model_state(
                'claude_code',
                relay_id='2',
                relay_model='relay-alias',
            )
            self.assertEqual(payload['provider'], 'claude_code')
            self.assertEqual(payload['model_mode'], 'explicit')
            self.assertEqual(payload['configured_model'], 'claude-sonnet-5')
            self.assertNotIn('relay', payload)


class SpawnEntryWiringTests(unittest.TestCase):
    """Resident spawns use cc_model_snapshot(); legacy one-shot uses cc_model_args()."""

    def _assert_calls_helper(self, path: Path, method_name: str, helper_name: str):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        found = False

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                nonlocal found
                if node.name != method_name:
                    self.generic_visit(node)
                    return
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = child.func
                        if isinstance(func, ast.Name) and func.id == helper_name:
                            found = True
                        elif isinstance(func, ast.Attribute) and func.attr == helper_name:
                            found = True
                self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

        Visitor().visit(tree)
        self.assertTrue(
            found,
            '%s.%s must call %s()' % (path.name, method_name, helper_name),
        )

    def test_spawn_entries_wire_helper(self):
        resident = Path(ROOT) / 'cc_resident.py'
        gateway = Path(ROOT) / 'gateway.py'
        self._assert_calls_helper(resident, '_spawn', 'cc_model_snapshot')
        self._assert_calls_helper(resident, 'spawn_resumable', 'cc_model_snapshot')
        self._assert_calls_helper(resident, 'spawn_fresh_named', 'cc_model_snapshot')
        self._assert_calls_helper(gateway, '_cc_stream_gen', 'cc_model_args')

    def test_decide_respawn_checks_model_identity(self):
        src = (Path(ROOT) / 'cc_resident.py').read_text(encoding='utf-8')
        self.assertIn("return 'model_changed'", src)
        self.assertIn('stored_identity is not None', src)
        self.assertIn('_model_identity', src)

    def test_snapshot_keeps_argv_and_identity_aligned(self):
        with mock.patch.object(
            config_store, 'get',
            side_effect=fake_get({'CC_CHAT_MODEL': 'claude-sonnet-5'}),
        ):
            raw, identity, args = cc_model_snapshot()
        self.assertEqual(raw, 'claude-sonnet-5')
        self.assertEqual(identity, 'explicit:claude-sonnet-5')
        self.assertEqual(args, ['--model', 'claude-sonnet-5'])


class ModelChangedDecisionTests(unittest.TestCase):
    def _alive_session(self, *, identity=None, turn_count=0):
        sess = cc_resident.ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        proc = mock.Mock()
        proc.poll.return_value = None
        sess._proc = proc
        sess._cold = False
        sess._system_text = 'STATIC'
        sess._last_used = 10**12
        sess._resident_turn_count = turn_count
        sess._last_round_context = 0
        sess._turns_since_respawn = 0
        sess._model_identity = identity
        return sess

    def test_none_identity_does_not_false_positive_model_changed(self):
        """Pre-spawn / fake alive fixtures keep identity=None → skip model check."""
        sess = self._alive_session(identity=None)
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
            with mock.patch('cc_resident._cfg_int', side_effect=lambda k, d: {
                'CC_MAX_RESIDENT_TURNS': 30,
                'CC_CONTEXT_SOFT_LIMIT': 90_000,
                'CC_CONTEXT_HARD_LIMIT': 120_000,
                'CC_MIN_TURNS_BETWEEN_RESPAWNS': 5,
            }.get(k, d)):
                self.assertIsNone(sess._decide_respawn_reason('STATIC'))

    def test_default_identity_matches_default_config(self):
        sess = self._alive_session(identity='default')
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
            with mock.patch('cc_resident._cfg_int', side_effect=lambda k, d: d):
                self.assertIsNone(sess._decide_respawn_reason('STATIC'))

    def test_turn_limit_when_model_unchanged(self):
        sess = self._alive_session(identity='default', turn_count=2)
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
            with mock.patch('cc_resident._cfg_int', side_effect=lambda k, d: {
                'CC_MAX_RESIDENT_TURNS': 2,
                'CC_CONTEXT_SOFT_LIMIT': 90_000,
                'CC_CONTEXT_HARD_LIMIT': 120_000,
                'CC_MIN_TURNS_BETWEEN_RESPAWNS': 5,
            }.get(k, d)):
                self.assertEqual(sess._decide_respawn_reason('STATIC'), 'turn_limit')

    def test_identity_mismatch_means_respawn(self):
        sess = self._alive_session(identity='explicit:claude-sonnet-5')
        with mock.patch.object(
            config_store, 'get',
            side_effect=fake_get({'CC_CHAT_MODEL': 'claude-opus-4-8'}),
        ):
            self.assertEqual(sess._decide_respawn_reason('STATIC'), 'model_changed')

    def test_explicit_to_default_mismatch(self):
        sess = self._alive_session(identity='explicit:claude-sonnet-5')
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
            self.assertEqual(sess._decide_respawn_reason('STATIC'), 'model_changed')


if __name__ == '__main__':
    unittest.main()
