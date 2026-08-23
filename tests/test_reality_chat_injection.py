"""Focused P2C.1h Reality request-context tests.

These tests cover the provider seam helpers and source-level fences without
starting a provider or reading the Reality runtime.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-reality-chat-config.db'),
)


def _import_gateway():
    if 'gateway' in sys.modules:
        return sys.modules['gateway']
    stubbed = []
    if 'tools.workspace_registry' not in sys.modules:
        registry = mock.MagicMock()
        registry.TOOLS_NOTE = ''
        registry.build_resident_tool_defs.return_value = []
        registry.load_registry.return_value = []
        sys.modules['tools.workspace_registry'] = registry
        stubbed.append('tools.workspace_registry')
    if 'tools.workspace_agent' not in sys.modules:
        agent = mock.MagicMock()
        agent.get_workspace_tool_defs.return_value = []
        sys.modules['tools.workspace_agent'] = agent
        stubbed.append('tools.workspace_agent')
    import gateway
    for name in stubbed:
        sys.modules.pop(name, None)
    return gateway


class RealityRequestContextTests(unittest.TestCase):
    def test_empty_context_is_a_noop(self):
        gateway = _import_gateway()
        original = [{'role': 'user', 'content': 'hello'}]
        self.assertEqual(gateway._normalize_reality_context(None), '')
        self.assertEqual(gateway._normalize_reality_context('   '), '')
        self.assertIs(
            gateway._append_reality_to_last_user(original, '   '),
            original,
        )
        self.assertEqual(original[0]['content'], 'hello')

    def test_multimodal_context_appends_text_without_touching_image(self):
        gateway = _import_gateway()
        original = [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'hello'},
                    {'type': 'image', 'source': {'type': 'base64', 'data': 'abc'}},
                ],
            },
        ]
        injected = gateway._append_reality_to_last_user(
            original,
            '设备当前静止，电量80%。',
        )
        self.assertIsNot(injected, original)
        self.assertEqual(
            injected[0]['content'][:2],
            original[0]['content'],
        )
        self.assertEqual(injected[0]['content'][2], {
            'type': 'text',
            'text': '\n\n设备当前静止，电量80%。',
        })
        self.assertEqual(len(original[0]['content']), 2)

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

    def test_frontend_has_three_independent_capture_boundaries(self):
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
            'const realityContext = decision === \'approve\'',
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
            "_stream_cc_deferred_confirmation(",
            source,
        )
        self.assertIn(
            'reality_context=_reality_context',
            source,
        )


if __name__ == '__main__':
    unittest.main()
