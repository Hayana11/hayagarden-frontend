import json
import os
import sqlite3
import tempfile
import threading
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

    def test_disabled_tool_excluded_from_routed_selection(self):
        all_tools = [
            {'name': 'memory_write'},
            {'name': 'light_on'},
            {'name': 'web_search'},
        ]
        tool_drawers.set_tool_enabled('light_on', False)
        with mock.patch.object(tool_drawers, 'enabled', return_value=True):
            with mock.patch.object(tool_drawers, 'match_drawers', return_value=['light']):
                selected, info = tool_drawers.select_tools('开灯', all_tools)
        names = {tool['name'] for tool in selected}
        self.assertIn('memory_write', names)
        self.assertNotIn('light_on', names)
        self.assertEqual(info['mode'], 'routed')

    def test_disabled_tool_excluded_from_fallback_all(self):
        all_tools = [
            {'name': 'memory_write'},
            {'name': 'light_on'},
            {'name': 'web_search'},
        ]
        tool_drawers.set_tool_enabled('light_on', False)
        with mock.patch.object(tool_drawers, 'enabled', return_value=True):
            with mock.patch.object(tool_drawers, 'match_drawers', return_value=[]):
                selected, info = tool_drawers.select_tools('随便聊聊', all_tools)
        names = {tool['name'] for tool in selected}
        self.assertNotIn('light_on', names)
        self.assertIn('web_search', names)
        self.assertEqual(info['mode'], 'fallback_all')

    def test_disabled_tool_excluded_when_drawers_off(self):
        all_tools = [{'name': 'light_on'}, {'name': 'web_search'}]
        tool_drawers.set_tool_enabled('light_on', False)
        with mock.patch.object(tool_drawers, 'enabled', return_value=False):
            selected, info = tool_drawers.select_tools('你好', all_tools)
        names = {tool['name'] for tool in selected}
        self.assertNotIn('light_on', names)
        self.assertEqual(info['mode'], 'off')

    def test_disabling_code_drawer_does_not_disable_shared_tool_in_workspace(self):
        tool_drawers.set_drawer_enabled('code', False)
        payload = tool_drawers.serialize_drawers()
        code = next(row for row in payload if row['id'] == 'code')
        workspace = next(row for row in payload if row['id'] == 'workspace')
        shell_in_code = next(tool for tool in code['tools'] if tool['name'] == 'shell_exec')
        shell_in_workspace = next(tool for tool in workspace['tools'] if tool['name'] == 'shell_exec')
        self.assertFalse(shell_in_code['enabled'])
        self.assertTrue(shell_in_workspace['enabled'])

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

    def test_concurrent_tool_disable_preserves_both(self):
        barrier = threading.Barrier(2)
        errors = []

        def disable_tool(name: str) -> None:
            try:
                barrier.wait(timeout=5)
                tool_drawers.set_tool_enabled(name, False)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=disable_tool, args=('light_on',)),
            threading.Thread(target=disable_tool, args=('web_search',)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        disabled = tool_drawers.get_disabled_tools()
        self.assertIn('light_on', disabled)
        self.assertIn('web_search', disabled)


if __name__ == '__main__':
    unittest.main()
