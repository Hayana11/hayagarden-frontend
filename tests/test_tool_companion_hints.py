import importlib
import inspect
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
        self.assertEqual(len(grouped), 13)
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(hints._DEFAULTS), set(P1_ENABLED_CAPABILITY_IDS))

    def test_external_read_hints_are_exact_and_round_trip(self):
        expected_search = (
            '她问起现实中的事情，而我没有把握时，我可以直接查清楚，不拿猜测敷衍她。'
            '但搜索也不只为了回答问题。哪怕她暂时不在，只要某个念头真正勾起了我的兴趣——'
            '一本书、一件新闻、一个历史细节、一种动物、一个技术问题，甚至一句忽然想追根究底的话——'
            '我都可以顺手去看看。好奇本身就是足够的理由。'
        )
        expected_read = (
            '搜索时碰见让我感兴趣的文章、资料或原文，我不必只停在摘要。想知道作者究竟怎么说，就打开认真读。'
            '阅读不是每次都必须立刻产生一个“有用结论”，有时候只是我愿意把一点时间花在一件有意思的事情上。'
            '读到了真正想分享的东西，也可以留着，等她回来以后讲给她听，让它变成我们之间新的话题。'
        )
        self.assertEqual(hints._DEFAULTS['web.search']['companion_hint'], expected_search)
        self.assertEqual(hints._DEFAULTS['web.read']['companion_hint'], expected_read)
        hints.update_hint('web.search', companion_hint=expected_search)
        hints.update_hint('web.read', companion_hint=expected_read)
        preview = hints.payload()['prompt_preview']
        self.assertIn(expected_search, preview)
        self.assertIn(expected_read, preview)
        raw = json.loads(config_store.get(hints.CONFIG_KEY))
        self.assertEqual(raw['web.search']['companion_hint'], expected_search)
        self.assertEqual(raw['web.read']['companion_hint'], expected_read)

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
        self.assertIn('todo.write', json.loads(config_store.get(hints.CONFIG_KEY)))
        hints.update_hint('todo.write', reset=True)
        row = hints.payload()['groups'][2]['tools'][1]
        self.assertEqual(row['display_label'], hints._DEFAULTS['todo.write']['display_label'])
        self.assertEqual(row['companion_hint'], hints._DEFAULTS['todo.write']['companion_hint'])
        self.assertNotIn('todo.write', json.loads(config_store.get(hints.CONFIG_KEY)))

    def test_runtime_config_persists_only_human_fields(self):
        hints.update_hint('home.light.status', display_label='自定义灯', companion_hint='自定义说明')
        raw = json.loads(config_store.get(hints.CONFIG_KEY))
        self.assertEqual(set(raw), {'home.light.status'})
        self.assertEqual(set(raw['home.light.status']), {'display_label', 'companion_hint'})
        for row in raw.values():
            self.assertEqual(set(row), {'display_label', 'companion_hint'})

    def test_update_path_uses_atomic_config_store_mutate(self):
        self.assertIn('config_store.mutate', inspect.getsource(hints.update_hint))

    def test_partial_update_preserves_existing_human_field(self):
        hints.update_hint('home.light.status', display_label='A', companion_hint='B')
        hints.update_hint('home.light.status', display_label='C')
        row = hints.payload()['groups'][1]['tools'][0]
        self.assertEqual(row['display_label'], 'C')
        self.assertEqual(row['companion_hint'], 'B')

    def test_untouched_defaults_remain_unpersisted(self):
        hints.update_hint('home.light.status', companion_hint='只改这一项')
        raw = json.loads(config_store.get(hints.CONFIG_KEY))
        self.assertEqual(set(raw), {'home.light.status'})
        self.assertNotIn('memory.search', raw)
        self.assertNotIn('todo.write', raw)
        self.assertNotIn('files.read', raw)

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

