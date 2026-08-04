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

from chat.cc_model import (  # noqa: E402
    CC_MODEL_CATALOG,
    cc_model_args,
    cc_model_identity,
    cc_model_mode,
    describe_cc_model_state,
    get_cc_chat_model,
    set_cc_chat_model,
)
from chat.model_state import describe_chat_model_state  # noqa: E402
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
             mock.patch.object(config_store, 'set', side_effect=_set):
            out = set_cc_chat_model('claude-opus-4-8')
            self.assertEqual(store['CC_CHAT_MODEL'], 'claude-opus-4-8')
            self.assertEqual(out['model_mode'], 'explicit')
            self.assertEqual(out['effective_from'], 'next_turn')
            cleared = set_cc_chat_model(None)
            self.assertEqual(store['CC_CHAT_MODEL'], '')
            self.assertEqual(cleared['model_mode'], 'default')
            self.assertIsNone(cleared['configured_model'])

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
    """All four production Claude spawn entries must call cc_model_args()."""

    def _assert_calls_cc_model_args(self, path: Path, method_name: str):
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
                        if isinstance(func, ast.Name) and func.id == 'cc_model_args':
                            found = True
                        elif isinstance(func, ast.Attribute) and func.attr == 'cc_model_args':
                            found = True
                self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

        Visitor().visit(tree)
        self.assertTrue(found, '%s.%s must call cc_model_args()' % (path.name, method_name))

    def test_spawn_entries_wire_helper(self):
        resident = Path(ROOT) / 'cc_resident.py'
        gateway = Path(ROOT) / 'gateway.py'
        self._assert_calls_cc_model_args(resident, '_spawn')
        self._assert_calls_cc_model_args(resident, 'spawn_resumable')
        self._assert_calls_cc_model_args(resident, 'spawn_fresh_named')
        self._assert_calls_cc_model_args(gateway, '_cc_stream_gen')

    def test_decide_respawn_checks_model_identity(self):
        src = (Path(ROOT) / 'cc_resident.py').read_text(encoding='utf-8')
        self.assertIn("return 'model_changed'", src)
        self.assertIn('cc_model_identity()', src)
        self.assertIn('_model_identity', src)


class ModelChangedDecisionTests(unittest.TestCase):
    def test_identity_mismatch_means_respawn(self):
        """Simulate resident started as A, config now B → model_changed."""
        resident_identity = 'explicit:claude-sonnet-5'
        with mock.patch.object(
            config_store, 'get',
            side_effect=fake_get({'CC_CHAT_MODEL': 'claude-opus-4-8'}),
        ):
            requested = cc_model_identity()
        self.assertNotEqual(requested, resident_identity)
        self.assertEqual(requested, 'explicit:claude-opus-4-8')

    def test_explicit_to_default_mismatch(self):
        resident_identity = 'explicit:claude-sonnet-5'
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
            requested = cc_model_identity()
        self.assertEqual(requested, 'default')
        self.assertNotEqual(requested, resident_identity)


if __name__ == '__main__':
    unittest.main()
