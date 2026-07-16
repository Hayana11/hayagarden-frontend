import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import config_store
import tool_drawers


class ToolDrawersToggleTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE runtime_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
        conn.close()
        self._db_patch = mock.patch.object(config_store, 'DB_PATH', self.db_path)
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        os.unlink(self.db_path)

    def test_disabled_tool_excluded_from_selection(self):
        all_tools = [
            {'name': 'save_memory'},
            {'name': 'light_on'},
            {'name': 'web_search'},
        ]
        tool_drawers.set_tool_enabled('light_on', False)
        with mock.patch.object(tool_drawers, 'enabled', return_value=True):
            with mock.patch.object(tool_drawers, 'match_drawers', return_value=['light']):
                selected, info = tool_drawers.select_tools('开灯', all_tools)
        names = {tool['name'] for tool in selected}
        self.assertIn('save_memory', names)
        self.assertNotIn('light_on', names)
        self.assertEqual(info['mode'], 'routed')

    def test_serialize_drawers_reflects_disabled_state(self):
        tool_drawers.set_drawer_enabled('shopping', False)
        payload = tool_drawers.serialize_drawers()
        shopping = next(row for row in payload if row['id'] == 'shopping')
        self.assertFalse(shopping['enabled'])
        self.assertTrue(all(not tool['enabled'] for tool in shopping['tools']))

    def test_set_tool_enabled_persists_json(self):
        tool_drawers.set_tool_enabled('web_search', False)
        raw = config_store.get(tool_drawers.DISABLED_TOOLS_KEY, '')
        self.assertIn('web_search', json.loads(raw))


if __name__ == '__main__':
    unittest.main()
