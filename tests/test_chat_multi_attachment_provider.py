"""Focused regressions for canonical current-turn attachment delivery."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chat.attachment_contract import (
    AttachmentValidationError,
    provider_current_turn_attachment_parts,
    provider_current_turn_attachments,
)
from chat.cc_vision_bridge import build_claude_user_content


class ChatMultiAttachmentProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.static_dir = Path(self.tmp.name)
        (self.static_dir / "uploads" / "files").mkdir(parents=True)
        self.image_bytes = b"fake-image"

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name, body):
        stored = "abcdef12_" + name
        (self.static_dir / "uploads" / "files" / stored).write_bytes(body)
        return {
            "type": "file",
            "url": "/static/uploads/files/" + stored,
            "name": name,
        }

    def _image(self, name):
        return {"type": "image", "url": "/static/uploads/" + name, "name": name}

    def _content(self, parts):
        return build_claude_user_content(
            text="用户说明",
            attachment_parts=parts,
            resolve_fn=lambda _ref: (self.image_bytes, "image/png"),
        )

    def test_single_image_is_one_image_block(self):
        content = self._content(provider_current_turn_attachment_parts(
            [self._image("one.png")],
            static_dir=str(self.static_dir),
        ))
        self.assertEqual([b["type"] for b in content].count("image"), 1)

    def test_two_images_are_two_image_blocks(self):
        content = self._content(provider_current_turn_attachment_parts(
            [self._image("one.png"), self._image("two.png")],
            static_dir=str(self.static_dir),
        ))
        self.assertEqual([b["type"] for b in content].count("image"), 2)

    def test_four_images_are_four_image_blocks(self):
        content = self._content(provider_current_turn_attachment_parts(
            [self._image("1.png"), self._image("2.png"),
             self._image("3.png"), self._image("4.png")],
            static_dir=str(self.static_dir),
        ))
        self.assertEqual([b["type"] for b in content].count("image"), 4)

    def test_current_turn_four_images_are_not_history_last_two(self):
        parts = provider_current_turn_attachment_parts(
            [self._image("1.png"), self._image("2.png"),
             self._image("3.png"), self._image("4.png")],
            static_dir=str(self.static_dir),
        )
        self.assertEqual(len(parts), 4)
        self.assertEqual([part["type"] for part in parts], ["image"] * 4)

    def test_one_text_file_delivers_body(self):
        parts = provider_current_turn_attachment_parts(
            [self._file("note.txt", b"alpha")],
            static_dir=str(self.static_dir),
        )
        self.assertIn("alpha", parts[0]["text"])
        self.assertIn("note.txt", parts[0]["text"])

    def test_two_text_files_keep_bodies_and_names(self):
        parts = provider_current_turn_attachment_parts(
            [self._file("a.txt", b"AAA"), self._file("b.md", b"BBB")],
            static_dir=str(self.static_dir),
        )
        joined = "\n".join(part["text"] for part in parts)
        self.assertIn("a.txt", joined)
        self.assertIn("AAA", joined)
        self.assertIn("b.md", joined)
        self.assertIn("BBB", joined)

    def test_mixed_text_and_two_images_preserves_all_content(self):
        parts = provider_current_turn_attachment_parts(
            [self._image("one.png"), self._file("note.txt", b"BODY"),
             self._image("two.png")],
            static_dir=str(self.static_dir),
        )
        content = self._content(parts)
        self.assertEqual([b["type"] for b in content], ["text", "image", "text", "image"])
        self.assertIn("BODY", content[2]["text"])

    def test_attachments_are_used_when_legacy_scalars_are_empty(self):
        items = provider_current_turn_attachments(
            json.dumps([self._image("one.png"), self._image("two.png")]),
            legacy_image_url="",
            legacy_file_url="",
            legacy_file_name="",
        )
        self.assertEqual([item["url"] for item in items],
                         ["/static/uploads/one.png", "/static/uploads/two.png"])

    def test_legacy_single_image_is_compatible(self):
        parts = provider_current_turn_attachment_parts(
            [], legacy_image_url="/static/uploads/legacy.png",
            static_dir=str(self.static_dir),
        )
        self.assertEqual([part["type"] for part in parts], ["image"])

    def test_legacy_single_file_is_compatible(self):
        item = self._file("legacy.txt", b"legacy body")
        parts = provider_current_turn_attachment_parts(
            [], legacy_file_url=item["url"], legacy_file_name=item["name"],
            static_dir=str(self.static_dir),
        )
        self.assertIn("legacy body", parts[0]["text"])

    def test_binary_file_is_honest_degrade(self):
        item = self._file("scan.pdf", b"not parsed")
        parts = provider_current_turn_attachment_parts(
            [item], static_dir=str(self.static_dir),
        )
        self.assertIn("不读取正文", parts[0]["text"])
        self.assertNotIn("not parsed", parts[0]["text"])

    def test_more_than_four_attachments_fail_closed(self):
        with self.assertRaises(AttachmentValidationError):
            provider_current_turn_attachments(
                [self._image("%d.png" % n) for n in range(5)]
            )

    def test_invalid_canonical_reference_fails_closed(self):
        with self.assertRaises(AttachmentValidationError):
            provider_current_turn_attachments([{
                "type": "image",
                "url": "/static/uploads/../secret.png",
                "name": "secret.png",
            }])

    def test_attachment_order_is_stable(self):
        value = [self._image("first.png"), self._file("middle.txt", b"M"),
                 self._image("last.png")]
        first = provider_current_turn_attachment_parts(value, static_dir=str(self.static_dir))
        second = provider_current_turn_attachment_parts(value, static_dir=str(self.static_dir))
        self.assertEqual(first, second)
        self.assertEqual([part["type"] for part in first], ["image", "text", "image"])


    def test_daily_hot_cold_respawn_each_keep_all_current_images(self):
        from chat.daily_runtime import format_resident_turn_content

        attachments = [self._image("%d.png" % n) for n in range(4)]
        with mock.patch(
            "chat.cc_vision_bridge.resolve_image_bytes",
            return_value=(self.image_bytes, "image/png"),
        ):
            for is_cold, is_respawn in ((False, False), (True, False), (False, True)):
                content = format_resident_turn_content(
                    assembly={"state": "", "current_day_history": []},
                    user_content="[image]",
                    user_image_url="",
                    user_attachments=attachments,
                    attachment_static_dir=str(self.static_dir),
                    is_cold=is_cold,
                    is_respawn=is_respawn,
                )
                self.assertEqual(
                    [block["type"] for block in content].count("image"), 4
                )

    def test_daily_text_file_contains_body_in_hot_cold_respawn(self):
        from chat.daily_runtime import format_resident_turn_content

        attachment = self._file("daily.txt", b"daily body")
        for is_cold, is_respawn in ((False, False), (True, False), (False, True)):
            content = format_resident_turn_content(
                assembly={"state": "", "current_day_history": []},
                user_content="请读文件",
                user_image_url="",
                user_attachments=[attachment],
                attachment_static_dir=str(self.static_dir),
                is_cold=is_cold,
                is_respawn=is_respawn,
            )
            self.assertIn("daily body", "\n".join(
                block.get("text", "") for block in content if block["type"] == "text"
            ))

    def test_empty_string_metadata_without_legacy_is_empty(self):
        self.assertEqual(provider_current_turn_attachments(""), [])

    def test_whitespace_metadata_without_legacy_is_empty(self):
        self.assertEqual(provider_current_turn_attachments("  \n\t"), [])

    def test_empty_json_array_metadata_is_empty(self):
        self.assertEqual(provider_current_turn_attachments("[]"), [])

    def test_empty_string_keeps_legacy_image(self):
        items = provider_current_turn_attachments(
            "", legacy_image_url="/static/uploads/legacy.png",
        )
        self.assertEqual(items, [{
            "type": "image",
            "url": "/static/uploads/legacy.png",
            "name": "",
        }])

    def test_empty_string_keeps_legacy_file(self):
        item = self._file("legacy.txt", b"legacy body")
        items = provider_current_turn_attachments(
            "", legacy_file_url=item["url"], legacy_file_name=item["name"],
        )
        self.assertEqual(items, [item])

    def test_nonempty_malformed_json_fails_closed(self):
        with self.assertRaises(AttachmentValidationError):
            provider_current_turn_attachments("{")

    def test_json_object_metadata_fails_closed(self):
        with self.assertRaises(AttachmentValidationError):
            provider_current_turn_attachments("{}")

    def test_canonical_and_same_legacy_scalar_are_not_duplicated(self):
        item = self._image("same.png")
        items = provider_current_turn_attachments(
            [item], legacy_image_url=item["url"],
        )
        self.assertEqual(items, [item])

    def test_daily_prior_attachments_use_explicit_bounded_degrade_policy(self):
        from chat.daily_runtime import (
            DAILY_HISTORY_ATTACHMENT_POLICY,
            format_resident_turn_content,
        )

        image_one = self._image("one.png")
        image_two = self._image("two.png")
        text_file = self._file("round-a.txt", b"private round-a body")
        history = [{
            "role": "user",
            "content": "Round A text",
            "image_url": "",
            "file_url": "",
            "file_name": "",
            "attachments": [image_one, image_two, text_file],
        }, {
            "role": "assistant",
            "content": "Round A reply",
            "attachments": [],
        }]
        for is_cold, is_respawn in ((True, False), (False, True)):
            content = format_resident_turn_content(
                assembly={"state": "", "current_day_history": history},
                user_content="Round B text",
                user_image_url="",
                user_attachments=[],
                is_cold=is_cold,
                is_respawn=is_respawn,
            )
            self.assertEqual(DAILY_HISTORY_ATTACHMENT_POLICY, "explicit_metadata_degrade_v1")
            self.assertIn("Round A text", content)
            self.assertIn("one.png", content)
            self.assertIn("two.png", content)
            self.assertIn("round-a.txt", content)
            self.assertIn("显式降级为元数据标记", content)
            self.assertIn("本轮不重读历史图片或文件正文", content)
            self.assertNotIn("private round-a body", content)

    def test_forge_selected_carryover_keeps_images_and_text_file_body(self):
        from chat.context_window_forge import _user_content_for_row

        image_one = self._image("one.png")
        image_two = self._image("two.png")
        text_file = self._file("carryover.txt", b"carryover body")
        row = {
            "id": 101,
            "content": "Round A text",
            "image_url": "",
            "file_url": "",
            "file_name": "",
            "attachments": [image_one, text_file, image_two],
        }
        with mock.patch(
            "chat.cc_vision_bridge.resolve_image_bytes",
            return_value=(self.image_bytes, "image/png"),
        ):
            content = _user_content_for_row(
                row, attachment_static_dir=str(self.static_dir),
            )
        self.assertEqual(
            [block["type"] for block in content],
            ["text", "image", "text", "image"],
        )
        self.assertIn("carryover body", content[2]["text"])
        self.assertNotIn("data", content[0]["text"])


if __name__ == "__main__":
    unittest.main()
