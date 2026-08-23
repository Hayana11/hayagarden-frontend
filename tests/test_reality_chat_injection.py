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
        append = helpers['_append_reality_context']
        append_last = helpers['_append_reality_to_last_user']
        original_content = 'hello'
        original_messages = [{'role': 'user', 'content': original_content}]
        for empty in (None, '', '   ', '\n\t'):
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
            '_reality_context = _normalize_reality_context('
        )
        prepare = source.index('prepare_turn(', extract)
        strip = source.index("_request_data.pop('reality_context', None)", extract)
        self.assertLess(strip, prepare)
        self.assertIn(
            'reality_context=_reality_context',
            source,
        )


if __name__ == '__main__':
    unittest.main()
