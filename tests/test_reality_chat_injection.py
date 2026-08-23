"""Focused P2C.1h Reality request-context tests.

Load only the three pure gateway helper functions under test. Importing the
whole gateway would start unrelated production infrastructure (POSIX locks,
/opt/frontend databases, resident wiring, and provider setup).
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_reality_helpers():
    source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
    tree = ast.parse(source, filename='gateway.py')
    wanted = {
        '_normalize_reality_context',
        '_append_reality_context',
        '_append_reality_to_last_user',
    }
    body = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), 'gateway.py', 'exec'), namespace)
    return namespace


class RealityRequestContextTests(unittest.TestCase):
    def test_normalization_canonicalizes_empty_values(self):
        helpers = _load_reality_helpers()
        normalize = helpers['_normalize_reality_context']
        for raw in (None, '', '   ', '\n\t'):
            self.assertEqual(normalize(raw), '')
        self.assertEqual(
            normalize('  设备当前静止，电量80%。  '),
            '设备当前静止，电量80%。',
        )

    def test_empty_context_is_a_noop(self):
        helpers = _load_reality_helpers()
        normalize = helpers['_normalize_reality_context']
        append = helpers['_append_reality_context']
        append_last = helpers['_append_reality_to_last_user']
        original_content = 'hello'
        original_messages = [{'role': 'user', 'content': original_content}]
        for raw in (None, '', '   ', '\n\t'):
            empty = normalize(raw)
            self.assertEqual(append(original_content, empty), original_content)
            self.assertIs(
                append_last(original_messages, empty),
                original_messages,
            )
        self.assertEqual(original_messages[0]['content'], original_content)

    def test_multimodal_context_preserves_existing_blocks(self):
        helpers = _load_reality_helpers()

        class CanonicalReality(str):
            def strip(self, *args, **kwargs):
                raise AssertionError('downstream helper must not normalize')

        reality = CanonicalReality('设备当前静止，电量80%。')
        original = [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'hello'},
                    {
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'abc'},
                    },
                ],
            },
        ]
        injected = helpers['_append_reality_to_last_user'](original, reality)
        self.assertIsNot(injected, original)
        self.assertEqual(injected[0]['content'][:2], original[0]['content'])
        self.assertEqual(injected[0]['content'][2], {
            'type': 'text',
            'text': '\n\n设备当前静止，电量80%。',
        })
        self.assertEqual(original[0]['content'][1]['source']['data'], 'abc')
        self.assertEqual(len(original[0]['content']), 2)

    def test_persistence_payload_is_separate_from_canonical_context(self):
        helpers = _load_reality_helpers()
        request_data = {
            'user_message_id': 42,
            'reality_context': '  设备当前静止。  ',
        }
        reality = helpers['_normalize_reality_context'](
            request_data.pop('reality_context', None)
        )
        self.assertEqual(reality, '设备当前静止。')
        self.assertEqual(request_data, {'user_message_id': 42})

    def test_relay_selector_precedes_reality_injection(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        selector = source.index(
            'tool_drawers.select_tools_from_messages(messages, get_tools())'
        )
        reality = source.index(
            'messages = _append_reality_to_last_user(messages, _reality_context)'
        )
        rounds = source.index('for _round in range(5):', reality)
        self.assertLess(selector, reality)
        self.assertLess(reality, rounds)

    def test_frontend_has_independent_capture_boundaries(self):
        screen = (ROOT / 'app/src/screens/ChatScreen.tsx').read_text(encoding='utf-8')
        self.assertEqual(
            screen.count('realityPromptProjection.getSnapshot().text'),
            3,
        )
        self.assertIn(
            'await runStream(messageId, { realityContext });',
            screen,
        )
        self.assertIn(
            "const realityContext = decision === 'approve'",
            screen,
        )

        chat = (ROOT / 'app/src/lib/chat.ts').read_text(encoding='utf-8')
        self.assertIn('body.reality_context = extra.realityContext;', chat)
        self.assertNotIn('realityPromptProjection.getSnapshot()', chat)

    def test_gateway_strips_context_before_turn_persistence(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        extract = source.index(
            'request_reality_context = _normalize_reality_context('
        )
        prepare = source.index('prepare_turn(', extract)
        strip = source.index("_request_data.pop('reality_context', None)", extract)
        self.assertLess(strip, prepare)
        self.assertIn(
            'reality_context=request_reality_context',
            source,
        )


    def test_daily_active_route_passes_request_reality_to_production_path(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        tree = ast.parse(source, filename='gateway.py')
        daily_function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == '_stream_cc_daily_soft_window'
        )
        daily_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_stream_cc_daily_soft_window'
        ]
        self.assertEqual(len(daily_calls), 1)
        keyword = next(
            item for item in daily_calls[0].keywords
            if item.arg == 'request_reality_context'
        )
        self.assertIsInstance(keyword.value, ast.Name)
        self.assertEqual(keyword.value.id, 'request_reality_context')
        parameter_names = {
            arg.arg for arg in (
                daily_function.args.args
                + daily_function.args.kwonlyargs
            )
        }
        self.assertIn('request_reality_context', parameter_names)

    def test_daily_adapter_appends_at_send_seam_without_downstream_normalize(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        tree = ast.parse(source, filename='gateway.py')
        adapter = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and node.name == '_RequestRealityResident'
        )
        send_turn = next(
            node for node in adapter.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'send_turn'
        )
        append_calls = [
            node for node in ast.walk(send_turn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_append_reality_context'
        ]
        self.assertEqual(len(append_calls), 1)
        self.assertIsInstance(append_calls[0].args[1], ast.Attribute)
        self.assertEqual(
            append_calls[0].args[1].attr,
            '_request_reality_context',
        )
        daily_function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == '_stream_cc_daily_soft_window'
        )
        daily_stream = next(
            node for node in ast.walk(daily_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'stream_daily_resident_turn'
        )
        resident_kw = next(
            item for item in daily_stream.keywords
            if item.arg == 'resident'
        )
        self.assertIsInstance(resident_kw.value, ast.Name)
        self.assertEqual(resident_kw.value.id, '_daily_resident')
        self.assertEqual(
            sum(
                1 for node in ast.walk(daily_function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == '_normalize_reality_context'
            ),
            0,
        )

    def test_daily_empty_snapshot_is_noop_and_not_in_turn_data(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        tree = ast.parse(source, filename='gateway.py')
        daily_function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == '_stream_cc_daily_soft_window'
        )
        turn_data_reality_writes = [
            node for node in ast.walk(daily_function)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == '_turn_data'
            and isinstance(node.targets[0].slice, ast.Constant)
            and node.targets[0].slice.value == 'reality_context'
        ]
        self.assertEqual(turn_data_reality_writes, [])
        self.assertIn(
            "if request_reality_context else _CC_RESIDENT",
            ast.get_source_segment(source, daily_function),
        )
        helpers = _load_reality_helpers()
        self.assertEqual(
            helpers['_append_reality_context']('hello', ''),
            'hello',
        )

    def test_daily_cold_hot_use_one_request_local_send_adapter(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        tree = ast.parse(source, filename='gateway.py')
        daily_function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == '_stream_cc_daily_soft_window'
        )
        stream_calls = [
            node for node in ast.walk(daily_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'stream_daily_resident_turn'
        ]
        self.assertEqual(len(stream_calls), 1)
        daily_source = ast.get_source_segment(source, daily_function)
        self.assertIn('_daily_plan', daily_source)
        self.assertIn('_full_system', daily_source)
        self.assertIn('_RequestRealityResident', daily_source)
        self.assertIn('_CC_RESIDENT', daily_source)

    def test_daily_legacy_context_coexists_with_physical_request_context(self):
        gateway = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        daily_runtime = (ROOT / 'chat/daily_runtime.py').read_text(encoding='utf-8')
        self.assertIn('request_reality_context', gateway)
        self.assertIn('from chat.reality_context import build_reality_context', daily_runtime)
        self.assertIn(
            'from chat.reality_context import prepend_reality_to_provider_content',
            daily_runtime,
        )
        self.assertIn('_append_reality_context', gateway)
        self.assertNotIn('compileRealityContext', daily_runtime)

    def test_daily_reality_does_not_feed_static_or_resident_identity(self):
        source = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        tree = ast.parse(source, filename='gateway.py')
        daily_function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == '_stream_cc_daily_soft_window'
        )
        daily_source = ast.get_source_segment(source, daily_function)
        self.assertNotIn('static_system=request_reality_context', daily_source)
        self.assertNotIn('resident_key=request_reality_context', daily_source)
        self.assertNotIn('static_system_sha256=request_reality_context', daily_source)
        self.assertNotIn('persona_sha256=request_reality_context', daily_source)


if __name__ == '__main__':
    unittest.main()
