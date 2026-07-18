import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import user_profile


class UserProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile_path = Path(self.tmp.name) / 'profile.json'
        handle, self.db_path = tempfile.mkstemp(suffix='.db', dir=self.tmp.name)
        os.close(handle)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
        conn.commit()
        conn.close()
        self.patches = [
            mock.patch.object(user_profile, 'PROFILE_PATH', self.profile_path),
            mock.patch.object(user_profile, 'DB_PATH', self.db_path),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_normalize_filters_empty_memories_and_caps(self):
        profile = user_profile.normalize_profile({
            'fullName': '  Hayana  ',
            'nickname': '哈娅',
            'savedMemories': [
                {'content': '喜欢红茶'},
                {'content': '   '},
                '第二段记忆',
                *[{'content': f'm{i}'} for i in range(220)],
            ],
            'preferences': {'enabled': False, 'content': '  温柔一点  '},
        })
        self.assertEqual(profile['fullName'], 'Hayana')
        self.assertEqual(profile['nickname'], '哈娅')
        self.assertEqual(profile['preferences']['enabled'], False)
        self.assertEqual(profile['preferences']['content'], '温柔一点')
        self.assertEqual(len(profile['savedMemories']), 200)
        self.assertEqual(profile['savedMemories'][0]['content'], '喜欢红茶')

    def test_write_and_read_roundtrip(self):
        written = user_profile.write_profile({
            'fullName': 'Хаяна',
            'nickname': '哈娅',
            'savedMemories': [{'content': '讨厌熬夜', 'enabled': True}],
            'preferences': {'enabled': True, 'content': '用自然语气回复'},
        })
        self.assertTrue(self.profile_path.exists())
        loaded = user_profile.read_profile()
        self.assertEqual(loaded['fullName'], written['fullName'])
        self.assertEqual(loaded['nickname'], '哈娅')
        self.assertEqual(loaded['savedMemories'][0]['content'], '讨厌熬夜')
        self.assertIn('用自然语气回复', user_profile.build_profile_context(loaded))

    def test_migrate_legacy_haya_note(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO settings (key,value) VALUES (?,?)',
            ('haya_profile', json.dumps({'note': '她喜欢巧克力'}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()

        profile = user_profile.read_profile()
        self.assertEqual(profile['preferences']['content'], '她喜欢巧克力')
        self.assertTrue(self.profile_path.exists())
        ctx = user_profile.build_profile_context(profile)
        self.assertIn('她喜欢巧克力', ctx)

    def test_build_profile_context_name_and_preferences_only(self):
        ctx = user_profile.build_profile_context({
            'fullName': 'Hayana',
            'nickname': '哈娅',
            'savedMemories': [
                {'content': '不应再注入', 'enabled': True},
            ],
            'preferences': {'enabled': True, 'content': '温柔一点'},
        })
        self.assertIn('Hayana', ctx)
        self.assertIn('哈娅', ctx)
        self.assertIn('温柔一点', ctx)
        self.assertNotIn('不应再注入', ctx)

        disabled = user_profile.build_profile_context({
            'fullName': 'Hayana',
            'preferences': {'enabled': False, 'content': '不该出现'},
        })
        self.assertIn('Hayana', disabled)
        self.assertNotIn('不该出现', disabled)


if __name__ == '__main__':
    unittest.main()
