import unittest

from relay.adapter import adapt_request


class RelayAdapterTests(unittest.TestCase):
    def test_opus_55_uses_default_thinking_budget(self):
        payload = {
            'model': 'claude-opus-5-5',
            'thinking': {'type': 'enabled', 'budget_tokens': 16000},
        }

        adapted, _headers = adapt_request(payload, {}, {})

        self.assertNotIn('thinking', adapted)
        self.assertEqual(
            payload['thinking'],
            {'type': 'enabled', 'budget_tokens': 16000},
        )
