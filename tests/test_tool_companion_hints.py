import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config_store
from tools.capability_manifest import P1_ENABLED_CAPABILITY_IDS
from tools import tool_companion_hints as hints


class ToolCompanionHintsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / 'runtime.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'CREATE TABLE runtime_config ('
            'key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)'
        )
        conn.commit()
        conn.close()
        self.db_patch = mock.patch.object(config_store, 'DB_PATH', self.db_path)
        self.db_patch.start()
        importlib.reload(hints)

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_catalog_is_exactly_the_enabled_manifest(self):
        grouped = [cid for _, _, ids in hints._EXPECTED_GROUPS for cid in ids]
        self.assertEqual(set(grouped), set(P1_ENABLED_CAPABILITY_IDS))
        self.assertEqual(len(grouped), 11)
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(hints._DEFAULTS), set(P1_ENABLED_CAPABILITY_IDS))

    def test_light_boundary_is_power_only_for_both_lights(self):
        row = hints.payload()['groups'][1]['tools'][0]
        boundary = row['physical_boundary']
        for phrase in ('主灯', '床头灯', 'power', '亮度', '色温'):
            self.assertIn(phrase, boundary)
        self.assertIn('不读取亮度或色温', boundary)
        self.assertIn('不能开关灯', boundary)

    def test_status_label_comes_from_manifest_autonomy_mode(self):
        tools = {
            tool['capability_id']: tool
            for group in hints.payload()['groups']
            for tool in group['tools']
        }
        self.assertEqual(tools['memory.search']['status_label'], '只读 · 可主动查')
        self.assertEqual(tools['todo.write']['status_label'], '可写 · 明确要求或先问')
        self.assertEqual(tools['files.read']['status_label'], '任务内可用')

    def test_model_preview_excludes_engineering_fields(self):
        preview = hints.payload()['prompt_preview']
        self.assertIn('真实能力边界', preview)
        for field in (
            'capability_id', 'mcp__', 'trigger=', 'deny_when=', 'provider_bindings',
            'provider binding', 'lease', 'approval', 'execution fence',
        ):
            self.assertNotIn(field, preview)

    def test_user_text_preserves_whitespace_and_newlines(self):
        original_label = '  灯光\n名字  '
        original_hint = '  第一行\n第二行\n  '
        hints.update_hint(
            'home.light.status',
            display_label=original_label,
            companion_hint=original_hint,
        )
        row = hints.payload()['groups'][1]['tools'][0]
        self.assertEqual(row['display_label'], original_label)
        self.assertEqual(row['companion_hint'], original_hint)

        raw = config_store.get(hints.CONFIG_KEY)
        self.assertEqual(json.loads(raw)['home.light.status']['companion_hint'], original_hint)

    def test_reset_restores_both_default_human_fields(self):
        hints.update_hint('todo.write', display_label='自定义', companion_hint='自定义说明')
        hints.update_hint('todo.write', reset=True)
        row = hints.payload()['groups'][2]['tools'][1]
        self.assertEqual(row['display_label'], hints._DEFAULTS['todo.write']['display_label'])
        self.assertEqual(row['companion_hint'], hints._DEFAULTS['todo.write']['companion_hint'])

    def test_runtime_config_persists_only_human_fields(self):
        hints.update_hint('home.light.status', display_label='自定义灯', companion_hint='自定义说明')
        raw = json.loads(config_store.get(hints.CONFIG_KEY))
        self.assertEqual(set(raw['home.light.status']), {'display_label', 'companion_hint'})
        for row in raw.values():
            self.assertEqual(set(row), {'display_label', 'companion_hint'})

    def test_reset_restores_default_human_copy_in_model_preview(self):
        default_hint = hints._DEFAULTS['todo.write']['companion_hint']
        hints.update_hint('todo.write', display_label='自定义', companion_hint='自定义说明')
        self.assertIn('自定义说明', hints.payload()['prompt_preview'])
        hints.update_hint('todo.write', reset=True)
        preview = hints.payload()['prompt_preview']
        self.assertNotIn('自定义说明', preview)
        self.assertIn(default_hint, preview)

    def test_daily_static_uses_the_same_tool_intuition_block(self):
        import chat.system_builder as builder

        with mock.patch.object(builder, 'read_persona', return_value='persona'), mock.patch.object(
            builder, 'build_stable_note', return_value='stable'
        ):
            static = builder.build_cc_static_parts()
            daily = builder.build_cc_daily_static_parts()
        self.assertEqual(static['tool_companion_intuition'], daily['tool_companion_intuition'])
        self.assertIn('【查看灯光状态】', static['full_system'])
        self.assertIn('【查看灯光状态】', daily['full_system'])

    def test_product_files_do_not_add_private_note_surface(self):
        root = Path(__file__).resolve().parents[1]
        files = (
            root / 'tools' / 'tool_companion_hints.py',
            root / 'chat' / 'system_builder.py',
            root / 'app.py',
            root / 'app' / 'src' / 'screens' / 'ProfileScreen.tsx',
            root / 'app' / 'src' / 'screens' / 'MomentsScreen.tsx',
        )
        text = '\n'.join(path.read_text(encoding='utf-8') for path in files)
        self.assertNotIn('private_' + 'note', text)
        self.assertNotIn('private' + 'Note', text)


if __name__ == '__main__':
    unittest.main()
