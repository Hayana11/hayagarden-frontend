from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import memory_runtime_bridge as bridge


CONTENT = "用户希望以后保留这个稳定偏好"


def make_posts_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT,
            layer TEXT,
            tags TEXT DEFAULT '',
            importance INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            resolved INTEGER DEFAULT 0,
            recall_count INTEGER DEFAULT 0,
            last_recalled_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


class MemoryRuntimeBridgeTests(unittest.TestCase):
    def _env(self, db_path: str, receipt_path: str, **extra):
        values = {
            "MEMORY_RUNTIME_RECEIPT_DB_PATH": receipt_path,
            "MEMORY_RUNTIME_POSTS_DB_PATH": db_path,
        }
        values.update(extra)
        return mock.patch.dict(os.environ, values, clear=False)

    def test_e2e_persist_recall_handoff_and_idempotency(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = str(Path(root) / "posts.db")
            receipt_path = str(Path(root) / "receipts.db")
            make_posts_db(db_path)
            provenance = {
                "request_id": "req-turn-a",
                "session_id": "window-a",
                "turn_id": "turn-a",
            }
            with self._env(db_path, receipt_path):
                with mock.patch(
                    "tools.ombre_adapter.hold_memory",
                    return_value={"id": "ombre-shadow"},
                ):
                    persisted = bridge.persist_memory(
                        db_path,
                        content=CONTENT,
                        source="save_memory",
                        provenance=provenance,
                        layer="long-term",
                        tags="e2e",
                        write_ombre=True,
                    )
                repeated = bridge.persist_memory(
                    db_path,
                    content=CONTENT,
                    source="save_memory",
                    provenance=provenance,
                    layer="long-term",
                    tags="e2e",
                    write_ombre=False,
                )
                self.assertEqual(persisted["status"], "CREATED")
                self.assertEqual(repeated["status"], "DEDUPLICATED")
                self.assertEqual(
                    persisted["receipt_id"], repeated["receipt_id"]
                )
                conn = sqlite3.connect(db_path)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                    1,
                )
                conn.close()

                timed_memo_a = bridge.persist_memory(
                    db_path,
                    content="[网页窗口 10-00] memo",
                    source="session_memo",
                    provenance={"session_id": "window-a", "turn_id": "turn-memo"},
                    idempotency_content="same user turn\\nsame assistant turn",
                    write_ombre=False,
                )
                timed_memo_b = bridge.persist_memory(
                    db_path,
                    content="[网页窗口 10-01] memo",
                    source="session_memo",
                    provenance={"session_id": "window-a", "turn_id": "turn-memo"},
                    idempotency_content="same user turn\\nsame assistant turn",
                    write_ombre=False,
                )
                self.assertEqual(
                    timed_memo_a["receipt_id"], timed_memo_b["receipt_id"]
                )
                conn = sqlite3.connect(db_path)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                    2,
                )
                conn.close()

                recalled = bridge.verify_read_after_write(
                    db_path,
                    persisted["receipt_id"],
                    query="保留 稳定偏好",
                    provenance={
                        "request_id": "req-turn-b",
                        "session_id": "window-b",
                        "turn_id": "turn-b",
                    },
                )
                self.assertEqual(recalled["status"], "VERIFIED")
                unverified = bridge.verify_handoff_recall(
                    "unrelated handoff",
                    memory_receipt_id=persisted["receipt_id"],
                    expected_content=CONTENT,
                    posts_db_path=db_path,
                )
                self.assertEqual(unverified["status"], "UNVERIFIED")
                from chat import system_builder

                with mock.patch(
                    "tools.ombre_adapter.get_handoff",
                    return_value=f"new window handoff: {CONTENT}",
                ):
                    handoff_text = system_builder._ombre_handoff_sync()
                self.assertIn(CONTENT, handoff_text)
                conn = sqlite3.connect(receipt_path)
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT status
                        FROM memory_runtime_receipt_events
                        WHERE receipt_id=? AND stage=?
                        """,
                        (
                            persisted["receipt_id"],
                            "new_window_handoff_recalled",
                        ),
                    ).fetchone()[0],
                    "VERIFIED",
                )
                conn.close()
                print(
                    "MEMORY_RUNTIME_E2E "
                    + json.dumps(
                        {
                            "turn_a_memory_persisted": persisted["receipt_id"],
                            "turn_b_memory_recalled_injected": recalled["event_id"],
                            "new_window_handoff_recalled": bridge.stage_receipt_id(
                                persisted["receipt_id"],
                                "new_window_handoff_recalled",
                            ),
                        },
                        sort_keys=True,
                    )
                )

    def test_auto_candidate_defaults_to_shadow_and_write_requires_gate(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = str(Path(root) / "posts.db")
            receipt_path = str(Path(root) / "receipts.db")
            make_posts_db(db_path)
            provenance = {
                "request_id": "req-auto",
                "session_id": "window-a",
                "turn_id": "turn-auto",
            }
            with self._env(
                db_path,
                receipt_path,
                MEMORY_RUNTIME_AUTO_CANDIDATE_MODE="shadow",
            ):
                shadow = bridge.observe_auto_candidate(
                    db_path,
                    user_message="我希望以后保留这个偏好",
                    assistant_message="好的，我会在后续对话中参考它。",
                    provenance=provenance,
                )
                duplicate_shadow = bridge.observe_auto_candidate(
                    db_path,
                    user_message="我希望以后保留这个偏好",
                    assistant_message="好的，我会在后续对话中参考它。",
                    provenance=provenance,
                )
                self.assertEqual(shadow["status"], "SHADOW")
                self.assertEqual(
                    shadow["receipt_id"], duplicate_shadow["receipt_id"]
                )
                conn = sqlite3.connect(db_path)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                    0,
                )
                conn.close()

                gated = bridge.observe_auto_candidate(
                    db_path,
                    user_message="我希望以后保留这个新的偏好",
                    assistant_message="好的，我会在后续对话中参考它。",
                    provenance={**provenance, "turn_id": "turn-auto-write"},
                )
                self.assertEqual(gated["status"], "SHADOW")

                unknown_mode = bridge.observe_auto_candidate(
                    db_path,
                    user_message="我希望以后保留这个未知模式偏好",
                    assistant_message="好的，我会在后续对话中参考它。",
                    provenance={**provenance, "turn_id": "turn-auto-unknown"},
                )
                self.assertEqual(unknown_mode["status"], "SHADOW")

            with self._env(
                db_path,
                receipt_path,
                MEMORY_RUNTIME_AUTO_CANDIDATE_MODE="write",
                MEMORY_RUNTIME_AUTO_CANDIDATE_ENABLED="yes",
            ):
                alias_enabled = bridge.observe_auto_candidate(
                    db_path,
                    user_message="我希望以后保留这个别名偏好",
                    assistant_message="好的，我会在后续对话中参考它。",
                    provenance={**provenance, "turn_id": "turn-auto-alias"},
                )
                self.assertEqual(alias_enabled["status"], "SHADOW")

            with self._env(
                db_path,
                receipt_path,
                MEMORY_RUNTIME_AUTO_CANDIDATE_MODE="write",
                MEMORY_RUNTIME_AUTO_CANDIDATE_ENABLED="1",
            ):
                written = bridge.observe_auto_candidate(
                    db_path,
                    user_message="我希望以后保留这个新的偏好",
                    assistant_message="好的，我会在后续对话中参考它。",
                    provenance={**provenance, "turn_id": "turn-auto-write-committed"},
                )
                self.assertEqual(written["status"], "CREATED")
                conn = sqlite3.connect(db_path)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                    1,
                )
                conn.close()

    def test_candidate_rejects_sensitive_or_unbounded_turns(self):
        self.assertEqual(
            bridge.derive_auto_candidate(
                "我的 API key 是 secret-token",
                "我会记住它。",
            )[1],
            "sensitive_pattern",
        )
        self.assertEqual(
            bridge.derive_auto_candidate("你好", "好的。")[1],
            "too_short",
        )
        self.assertEqual(
            bridge.derive_auto_candidate(
                "今天只是随便聊聊",
                "好的，我会陪你聊天。",
            )[1],
            "no_safe_intent",
        )

    def test_ombre_failure_is_fail_open_after_legacy_commit(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = str(Path(root) / "posts.db")
            receipt_path = str(Path(root) / "receipts.db")
            make_posts_db(db_path)
            with self._env(db_path, receipt_path):
                with mock.patch(
                    "tools.ombre_adapter.hold_memory",
                    side_effect=RuntimeError("sidecar unavailable"),
                ):
                    result = bridge.persist_memory(
                        db_path,
                        content="legacy remains authoritative",
                        source="session_memo",
                        provenance={"turn_id": "turn-fail-open"},
                        write_ombre=True,
                    )
                self.assertEqual(result["status"], "CREATED")
                self.assertEqual(result["ombre_status"], "FAILED")
                conn = sqlite3.connect(db_path)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                    1,
                )
                conn.close()

    def test_runtime_call_sites_are_wired_without_kernel_or_http_cutover(self):
        root = Path(__file__).resolve().parents[1]
        gateway = (root / "gateway.py").read_text(encoding="utf-8")
        system_builder = (
            root / "chat" / "system_builder.py"
        ).read_text(encoding="utf-8")
        self.assertIn("persist_memory", gateway)
        self.assertIn("observe_auto_candidate", gateway)
        self.assertIn("record_legacy_recall", gateway)
        self.assertIn("record_handoff_observation", system_builder)
        self.assertNotIn("OMBRE_ADAPTER_BACKEND", gateway)


if __name__ == "__main__":
    unittest.main()
