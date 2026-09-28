from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.gallery_provenance import GalleryProvenanceError, TrustedGalleryTurn, resolve_trusted_gallery_turn
from chat.gallery_service import GalleryServiceError, browser_singleflight, recall_gallery_photo, screenshot_chat
from tools.capability_manifest import P1_ENABLED_CAPABILITY_IDS, get_capability
from tools.cc_capability_adapter import FORBIDDEN_BUILTIN_TOOLS, physical_surface_names
from tools.cc_tool_surface import _CAPABILITY_PROXY_TOOL_SCHEMAS
from tools.lease_signer import DEFAULT_ALLOWED_CAPABILITIES, TURN_LEASE_FIELDS


GALLERY_IDS = ("gallery.save", "gallery.recall", "gallery.screenshot")
GALLERY_BINDINGS = (
    "mcp__capability__gallery_save",
    "mcp__capability__gallery_recall",
    "mcp__capability__gallery_screenshot",
)


def _provenance_db(path: Path, *, owner="turn-1", expires="2099-01-01 00:00:00", author="hayana", duplicate=False):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE daily_resident_turn_leases (
          context_id INTEGER, resident_generation INTEGER, lease_owner TEXT,
          request_message_id INTEGER, acquired_at TEXT, expires_at TEXT);
        CREATE TABLE daily_contexts (id INTEGER, chat_id TEXT, context_epoch INTEGER);
        CREATE TABLE daily_message_contexts (
          message_id INTEGER, context_id INTEGER, context_epoch INTEGER,
          resident_generation INTEGER, role TEXT);
        CREATE TABLE chat_messages (
          id INTEGER, author TEXT, content TEXT, attachments TEXT, image_url TEXT);
    """)
    conn.execute("INSERT INTO daily_contexts VALUES (7, 'chat-exact', 3)")
    conn.execute("INSERT INTO chat_messages VALUES (41, ?, 'look', ?, '')", (
        author,
        json.dumps([
            {"type": "image", "url": "/static/uploads/first.png"},
            {"type": "image", "url": "/static/uploads/second.png"},
        ]),
    ))
    conn.execute("INSERT INTO daily_message_contexts VALUES (41, 7, 3, 9, 'user')")
    conn.execute("INSERT INTO daily_resident_turn_leases VALUES (7, 9, ?, 41, '2026-01-01', ?)", (owner, expires))
    if duplicate:
        conn.execute("INSERT INTO daily_resident_turn_leases VALUES (7, 9, ?, 41, '2026-01-01', ?)", (owner, expires))
    conn.commit()
    conn.close()


class GalleryCapabilityCutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_lease_and_physical_surface_are_exact(self):
        self.assertTrue(set(GALLERY_IDS).issubset(P1_ENABLED_CAPABILITY_IDS))
        self.assertEqual(tuple(get_capability(cid)["provider_bindings"]["claude_code"] for cid in GALLERY_IDS), GALLERY_BINDINGS)
        self.assertTrue(set(GALLERY_IDS).issubset(DEFAULT_ALLOWED_CAPABILITIES["chat"]))
        self.assertTrue(set(GALLERY_IDS).isdisjoint(DEFAULT_ALLOWED_CAPABILITIES["wake"]))
        self.assertTrue(set(GALLERY_IDS).isdisjoint(DEFAULT_ALLOWED_CAPABILITIES["task"]))
        self.assertEqual(TURN_LEASE_FIELDS, (
            "lease_version", "turn_id", "turn_mode", "issued_from", "allowed_capabilities",
            "approval_ids", "task_contract_id", "issued_at",
        ))
        surface = set(physical_surface_names())
        self.assertTrue(set(GALLERY_BINDINGS).issubset(surface))
        self.assertTrue(set(FORBIDDEN_BUILTIN_TOOLS).isdisjoint(surface))

    def test_gallery_schemas_do_not_accept_model_provenance(self):
        save = _CAPABILITY_PROXY_TOOL_SCHEMAS[GALLERY_BINDINGS[0]]
        self.assertEqual(set(save["properties"]), {"attachment", "image_index", "note", "album", "first_impression"})
        self.assertEqual(save["properties"]["image_index"], {"type": "integer", "minimum": 0, "maximum": 3})
        self.assertEqual(save["properties"]["first_impression"]["maxLength"], 800)
        recall = _CAPABILITY_PROXY_TOOL_SCHEMAS[GALLERY_BINDINGS[1]]
        self.assertEqual(recall["properties"]["inspect_question"]["maxLength"], 500)
        self.assertEqual(_CAPABILITY_PROXY_TOOL_SCHEMAS[GALLERY_BINDINGS[2]]["properties"]["viewpoint"]["enum"], ["fyodor", "hayana"])
        self.assertFalse({"source_msg_id", "source_chat_id", "message_id", "conversation_id"} & set(save["properties"]))

    def test_provenance_resolves_only_exact_live_provider_turn(self):
        db = self.root / "memories.db"
        _provenance_db(db)
        turn = resolve_trusted_gallery_turn(db, "turn-1", now="2026-09-28 12:00:00")
        self.assertEqual(turn, TrustedGalleryTurn("turn-1", 41, 7, 3, 9, "chat-exact"))
        with self.assertRaisesRegex(GalleryProvenanceError, "missing or ambiguous"):
            resolve_trusted_gallery_turn(db, "wrong-turn", now="2026-09-28 12:00:00")

    def test_provenance_expired_ambiguous_and_wrong_author_fail_closed(self):
        for variant in ("expired", "ambiguous", "wrong-author"):
            with self.subTest(variant=variant):
                db = self.root / (variant + ".db")
                _provenance_db(db, expires="2020-01-01 00:00:00" if variant == "expired" else "2099-01-01 00:00:00", author="fyodor" if variant == "wrong-author" else "hayana", duplicate=variant == "ambiguous")
                with self.assertRaises(GalleryProvenanceError):
                    resolve_trusted_gallery_turn(db, "turn-1", now="2026-09-28 12:00:00")

    def test_current_turn_image_never_falls_back_and_requires_index(self):
        from chat.gallery_context import CurrentTurnImageError, resolve_current_turn_image
        db = self.root / "memories.db"
        _provenance_db(db)
        turn = resolve_trusted_gallery_turn(db, "turn-1", now="2026-09-28 12:00:00")
        def factory():
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            return conn
        with self.assertRaisesRegex(CurrentTurnImageError, "多张图片"):
            resolve_current_turn_image(turn.request_message_id, turn.chat_id, get_db_fn=factory)
        self.assertEqual(resolve_current_turn_image(41, turn.chat_id, get_db_fn=factory, image_index=0)["ref"], "/static/uploads/first.png")
        self.assertEqual(resolve_current_turn_image(41, turn.chat_id, get_db_fn=factory, image_index=1)["ref"], "/static/uploads/second.png")
        with self.assertRaisesRegex(CurrentTurnImageError, "不存在"):
            resolve_current_turn_image(999, turn.chat_id, get_db_fn=factory, image_index=0)

    def test_capability_recall_is_strict_and_has_no_accounting(self):
        photo = {"pid": "p1", "summary": "记忆", "visual_description": "蓝色围巾", "first_impression": "温暖", "emotion": "安心", "keywords": '["围巾"]', "source_msg_id": 1, "source_chat_id": "c", "mem_id": 9}
        import gallery_store
        picker = mock.Mock(return_value=photo)
        mark = mock.Mock()
        with mock.patch.object(gallery_store, "pick_for_recall", picker), mock.patch.object(gallery_store, "get", return_value=photo), mock.patch.object(gallery_store, "mark_sent", mark):
            result = recall_gallery_photo(keyword="围巾")
            inspected = recall_gallery_photo(pid="p1", inspect_question="小字是什么", describe_fn=lambda pid, question=None: "原图小字")
        self.assertTrue(result["semantic_memory_only"])
        self.assertEqual(result["delivery_marker"], "[[gallery:p1]]")
        picker.assert_called_once_with(keyword="围巾", emotion=None, strict=True)
        mark.assert_not_called()
        self.assertTrue(inspected["original_reloaded"])
        self.assertEqual(inspected["inspection_answer"], "原图小字")

    def test_screenshot_validates_viewpoint_and_registers_attachment(self):
        import attachment_store
        output = SimpleNamespace(stdout=json.dumps({"ok": True, "shot": str(self.root / "shot.png")}), stderr="", returncode=0)
        with mock.patch.object(attachment_store, "save", return_value="aid-1"):
            result = screenshot_chat("hayana", repo_root=self.root, attachments_root=self.root, run_fn=mock.Mock(return_value=output))
        self.assertEqual(result, {"ok": True, "viewpoint": "hayana", "attachment": "attachment://aid-1"})
        with self.assertRaisesRegex(GalleryServiceError, "fyodor or hayana"):
            screenshot_chat("other", repo_root=self.root, attachments_root=self.root)

    def test_browser_lock_serializes_and_releases_after_timeout(self):
        lock = self.root / "browser.lock"
        entered = threading.Event()
        release = threading.Event()
        def holder():
            with browser_singleflight(lock, timeout=1):
                entered.set()
                release.wait(1)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(GalleryServiceError, "busy"):
            with browser_singleflight(lock, timeout=0.05):
                pass
        release.set()
        thread.join(1)
        with browser_singleflight(lock, timeout=0.2):
            pass


if __name__ == "__main__":
    unittest.main()

