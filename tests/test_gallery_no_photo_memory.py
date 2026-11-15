from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.gallery_provenance import TrustedGalleryTurn
from chat.gallery_service import save_gallery_image


class GalleryPhotoRetirementTests(unittest.TestCase):
    def test_new_gallery_save_keeps_gallery_meaning_without_photo_memory(self):
        import gallery_store

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        memory_write = mock.Mock()
        fake_memory_tool = SimpleNamespace(save_memory=memory_write)
        turn = TrustedGalleryTurn("turn", 123, 2, 1, 7, "chat")
        meaning = {
            "summary": "值得保留的作品说明", "emotion": "温暖",
            "keywords": ["画作"], "importance": 60,
        }
        describe = mock.Mock(return_value="画面中有一幅水彩画")
        generate = mock.Mock(return_value=meaning)
        with (
            mock.patch.dict(sys.modules, {"tools.memory_tool": fake_memory_tool}),
            mock.patch.object(gallery_store, "save_attachment", return_value={"pid": "photo-1"}),
            mock.patch.object(gallery_store, "get", return_value={"pid": "photo-1"}),
            mock.patch.object(gallery_store, "set_meaning") as set_meaning,
        ):
            result = save_gallery_image(
                turn, db_path=str(Path(temp.name) / "chat.db"),
                attachment="attachment://12345678", note="留作作品",
                describe_fn=describe, meaning_fn=generate,
            )

        self.assertTrue(result["visual_description_available"])
        self.assertTrue(result["summary_available"])
        self.assertEqual(2, set_meaning.call_count)
        self.assertEqual("画面中有一幅水彩画", set_meaning.call_args_list[0].kwargs["visual_description"])
        self.assertEqual("值得保留的作品说明", set_meaning.call_args_list[1].kwargs["summary"])
        self.assertNotIn("mem_id", set_meaning.call_args_list[1].kwargs)
        memory_write.assert_not_called()

    def test_gallery_service_has_no_candidate_or_kernel_memory_writer(self):
        source = Path(__file__).resolve().parents[1] / "chat" / "gallery_service.py"
        text = source.read_text(encoding="utf-8")
        save_path = text.split("def save_gallery_image(", 1)[1].split("def recall_gallery_photo(", 1)[0]
        self.assertNotIn("memory_tool", save_path)
        self.assertNotIn("save_memory", save_path)
        self.assertNotIn("create_candidate", save_path)


if __name__ == "__main__":
    unittest.main()
