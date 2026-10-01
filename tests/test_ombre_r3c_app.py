"""R3C app PATCH contract tests."""

from __future__ import annotations

import sys
import unittest
from unittest import mock

sys.path.insert(0, "/opt/frontend-r3c")

import app


class EmotionPatchRouteTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()

    def test_patch_uses_canonical_bucket_id_and_returns_it(self):
        item = {"bucket_id": "abc123def456", "valence": 0.1}
        with mock.patch.object(app, "require_owner"), mock.patch(
            "emotion_memories.update_memory_point", return_value=item
        ) as update:
            response = self.client.patch(
                "/api/brain/emotions",
                json={"bucket_id": "abc123def456", "valence": 0.1, "arousal": 0.2},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["bucket_id"], "abc123def456")
        update.assert_called_once_with("abc123def456", 0.1, 0.2)

    def test_legacy_path_only_extracts_valid_basename_id(self):
        item = {"bucket_id": "abc123def456"}
        with mock.patch.object(app, "require_owner"), mock.patch(
            "emotion_memories.update_memory_point", return_value=item
        ) as update:
            response = self.client.patch(
                "/api/brain/emotions",
                json={
                    "path": "/untrusted/anywhere/name_ABC123DEF456.md",
                    "valence": 0,
                    "arousal": 0.4,
                },
            )
        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with("abc123def456", 0.0, 0.4)

    def test_invalid_path_is_not_authorized(self):
        with mock.patch.object(app, "require_owner"), mock.patch(
            "emotion_memories.update_memory_point"
        ) as update:
            response = self.client.patch(
                "/api/brain/emotions",
                json={"path": "/untrusted/file.md", "valence": 0, "arousal": 0.4},
            )
        self.assertEqual(response.status_code, 400)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
