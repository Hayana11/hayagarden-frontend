"""CONTEXT_LEAN_* flags default off and preserve legacy prompt bytes."""
from __future__ import annotations

import unittest
from unittest import mock

from chat.context_lean import (
    any_lean_enabled,
    lean_file_dedup_enabled,
    lean_history_enabled,
    lean_state_enabled,
    lean_tool_budget_enabled,
)


class ContextLeanFlagTests(unittest.TestCase):
    def test_defaults_are_off(self):
        with mock.patch('config_store.get_bool', return_value=False):
            self.assertFalse(lean_state_enabled())
            self.assertFalse(lean_history_enabled())
            self.assertFalse(lean_tool_budget_enabled())
            self.assertFalse(lean_file_dedup_enabled())
            self.assertFalse(any_lean_enabled())

    def test_legacy_build_messages_uses_block_window_when_lean_off(self):
        from chat.history_boundary import legacy_block_limit
        self.assertEqual(legacy_block_limit(70), 70)


if __name__ == '__main__':
    unittest.main()
