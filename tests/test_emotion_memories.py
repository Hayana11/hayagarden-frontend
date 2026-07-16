import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import emotion_memories


class EmotionMemoriesTests(unittest.TestCase):
    def setUp(self):
        self.bucket = tempfile.mkdtemp()
        self.path = os.path.join(self.bucket, 'memory.md')
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('---\nvalence: 0.2\narousal: 0.4\ncreated: 2026-07-10\n---\n\n一段记忆。')

    def tearDown(self):
        os.remove(self.path)
        os.rmdir(self.bucket)

    def _loader(self, path):
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        body = text.split('---', 2)[2].strip()
        meta = {'valence': 0.2, 'arousal': 0.4, 'created': '2026-07-10'}
        return SimpleNamespace(metadata=meta, content=body)

    def test_list_memory_points_normalizes_unipolar(self):
        with mock.patch.object(emotion_memories, 'BUCKET_DIR', self.bucket):
            items = emotion_memories.list_memory_points(load_frontmatter=self._loader)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['path'], self.path)
        self.assertAlmostEqual(items[0]['valence'], -0.6, places=2)

    def test_update_memory_point_keeps_ombre_storage_unipolar(self):
        dumps_calls = []

        class FakeFM:
            @staticmethod
            def load(path):
                return emotion_memories.list_memory_points(load_frontmatter=self._loader)[0] if False else SimpleNamespace(
                    metadata={'valence': 0.2, 'arousal': 0.4, 'created': '2026-07-10'},
                    content='一段记忆。',
                )

            @staticmethod
            def dumps(post):
                dumps_calls.append(post.metadata)
                return 'written'

        with mock.patch.object(emotion_memories, 'BUCKET_DIR', self.bucket):
            with mock.patch.dict('sys.modules', {'frontmatter': FakeFM}):
                updated = emotion_memories.update_memory_point(self.path, -0.6, 0.55)
        self.assertEqual(updated['valence'], -0.6)
        self.assertAlmostEqual(dumps_calls[0]['valence'], 0.2, places=3)
        self.assertEqual(dumps_calls[0]['valence_scale'], 'unipolar')

    def test_update_rejects_path_outside_bucket(self):
        with mock.patch.object(emotion_memories, 'BUCKET_DIR', self.bucket):
            with self.assertRaises(ValueError):
                emotion_memories.update_memory_point('/tmp/other.md', 0.1, 0.2)


if __name__ == '__main__':
    unittest.main()
