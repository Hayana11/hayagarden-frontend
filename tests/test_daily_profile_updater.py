import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chat.background_generation import BackgroundGenerationResult
from migrations.profile_documents import migrate
from tools.daily_profile_updater import (
    _chunks, _message_text, _parse, ensure_profile_document_schema, update_day, update_from_trace,
)
from tools.memory_candidates import CandidateSourceTrace


class DailyProfileUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = str(self.root / "memories.db")
        self.trace = CandidateSourceTrace(
            source_refs=("chat_messages:101", "chat_messages:102", "chat_messages:103"),
            derived_refs=("dayblock:2026-11-07:generation-abc",),
            unresolved_refs=(),
            raw_messages=(
                {"id": 101, "author": "hayana", "content": "我稳定地喜欢巧克力。" + "猫" * 180},
                {"id": 102, "author": "assistant", "content": "我长期重视诚实。"},
                {"id": 103, "author": "user", "content": "我还是喜欢巧克力。"},
            ),
            evidence_text="派生摘要不能代替原始聊天",
        )
        self.authority = SimpleNamespace(provider="api_relay", model_identity="relay/test-model")

    def tearDown(self):
        self.temp.cleanup()

    def result(self, data, authority=None):
        authority = authority or self.authority
        return BackgroundGenerationResult(
            text=json.dumps(data, ensure_ascii=False),
            provider=authority.provider,
            model_identity=authority.model_identity,
            actual_executor="test-background",
        )

    def response(self, *, haya=None, self_doc=None, haya_ids=None, self_ids=None):
        return {
            "SELF": {
                "changed": self_doc is not None,
                "updated_document": self_doc or "",
                "evidence_message_ids": self_ids or [],
            },
            "HAYA": {
                "changed": haya is not None,
                "updated_document": haya or "",
                "evidence_message_ids": haya_ids or [],
            },
        }

    def run_result(self, data, *, trace=None, source_day="2026-11-07", generator=None, chunk_chars=32_000):
        trace = trace or self.trace
        calls = []

        def generate(request, authority):
            calls.append((request, authority))
            if generator:
                return generator(request, authority)
            return self.result(data, authority)

        result = update_from_trace(
            self.db, source_day=source_day, trace=trace, authority=self.authority,
            generate_fn=generate, chunk_chars=chunk_chars,
        )
        return result, calls

    def test_parse_accepts_json_and_standard_json_code_fence(self):
        payload = self.response()
        raw = json.dumps(payload, ensure_ascii=False)
        fence = chr(96) * 3
        self.assertEqual(payload, _parse(raw))
        self.assertEqual(payload, _parse(fence + "json" + chr(10) + raw + chr(10) + fence))

    def test_chunks_keep_complete_messages_and_prompt_has_full_ids_and_text(self):
        chunks = _chunks(list(self.trace.raw_messages), budget=30)
        self.assertEqual(3, len(chunks))
        self.assertTrue(_message_text(chunks[0][0]).endswith(self.trace.raw_messages[0]["content"]))
        self.assertIn("猫" * 180, _message_text(chunks[0][0]))
        _, calls = self.run_result(self.response(), chunk_chars=30)
        self.assertEqual(3, len(calls))
        self.assertIn("[msg:101] 哈娅：", calls[0][0].prompt_text)
        self.assertIn("猫" * 180, calls[0][0].prompt_text)
        self.assertIn("当前 SELF 全文", calls[0][0].prompt_text)
        self.assertIn("当前 HAYA 全文", calls[0][0].prompt_text)

    def test_changed_target_writes_one_revision_with_before_after_evidence_and_authority(self):
        data = self.response(haya="哈娅喜欢巧克力，也明确表达了稳定偏好。", haya_ids=[101, 102])
        result, _ = self.run_result(data)
        self.assertEqual(["HAYA"], result["updated_targets"])
        self.assertEqual(1, result["revision_count"])
        with sqlite3.connect(self.db) as conn:
            doc = conn.execute("SELECT body,version,source_day FROM profile_documents WHERE target='HAYA'").fetchone()
            self.assertEqual(("哈娅喜欢巧克力，也明确表达了稳定偏好。", 1, "2026-11-07"), doc)
            revision = conn.execute(
                "SELECT version,before_body,after_body,source_day,evidence_refs_json,provider,model_identity "
                "FROM profile_document_revisions"
            ).fetchone()
            self.assertEqual((1, "", data["HAYA"]["updated_document"], "2026-11-07",
                              '["chat_messages:101","chat_messages:102"]',
                              "api_relay", "relay/test-model"), revision)
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM profile_documents").fetchone()[0])
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("memory_candidates", tables)
        self.assertNotIn("memory_states", tables)

    def test_speaker_evidence_gate_and_source_scope(self):
        cases = (
            ("HAYA", [102], False),
            ("SELF", [101], False),
            ("HAYA", [101, 102], True),
            ("SELF", [102, 101], True),
            ("HAYA", [999], False),
        )
        for target, ids, retained in cases:
            with self.subTest(target=target, ids=ids), tempfile.TemporaryDirectory() as temp:
                db = str(Path(temp) / "isolated.db")
                data = self.response(
                    haya="HAYA revised" if target == "HAYA" else None,
                    self_doc="SELF revised" if target == "SELF" else None,
                    haya_ids=ids if target == "HAYA" else [],
                    self_ids=ids if target == "SELF" else [],
                )
                result = update_from_trace(
                    db, source_day="2026-11-08", trace=self.trace, authority=self.authority,
                    generate_fn=lambda request, authority: self.result(data, authority),
                )
                self.assertEqual([target] if retained else [], result["updated_targets"])
                with sqlite3.connect(db) as conn:
                    count = conn.execute("SELECT COUNT(*) FROM profile_document_revisions").fetchone()[0]
                self.assertEqual(1 if retained else 0, count)

    def test_no_change_and_identical_document_leave_version_revision_and_updated_at_untouched(self):
        with sqlite3.connect(self.db) as conn:
            ensure_profile_document_schema(conn)
            conn.execute(
                "INSERT INTO profile_documents VALUES ('HAYA','哈娅喜欢巧克力。',3,'unchanged-time','2026-11-06')"
            )
            conn.execute(
                "INSERT INTO profile_document_revisions "
                "(target,version,before_body,after_body,source_day,evidence_refs_json,provider,model_identity,created_at) "
                "VALUES ('HAYA',3,'old','哈娅喜欢巧克力。','2026-11-06','[]','api_relay','relay/test-model','old-time')"
            )
        false_result, _ = self.run_result(self.response())
        identical_result, _ = self.run_result(
            self.response(haya="  哈娅喜欢巧克力。  ", haya_ids=[103]),
            source_day="2026-11-08",
        )
        self.assertEqual([], false_result["updated_targets"])
        self.assertEqual([], identical_result["updated_targets"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(("哈娅喜欢巧克力。", 3, "unchanged-time"),
                             conn.execute("SELECT body,version,updated_at FROM profile_documents WHERE target='HAYA'").fetchone())
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM profile_document_revisions").fetchone()[0])

    def test_blank_changed_document_is_rejected_without_mutating_existing_target(self):
        with sqlite3.connect(self.db) as conn:
            ensure_profile_document_schema(conn)
            conn.execute(
                "INSERT INTO profile_documents VALUES ('HAYA','哈娅喜欢巧克力。',3,'keep-this-time','2026-11-06')"
            )
            conn.execute(
                "INSERT INTO profile_document_revisions "
                "(target,version,before_body,after_body,source_day,evidence_refs_json,provider,model_identity,created_at) "
                "VALUES ('HAYA',3,'older','哈娅喜欢巧克力。','2026-11-06','[]','api_relay','relay/test-model','revision-v3')"
            )

        result, _ = self.run_result(
            self.response(haya="   ", haya_ids=[101]), source_day="2026-11-08",
        )
        self.assertEqual([], result["updated_targets"])
        self.assertEqual(0, result["revision_count"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                ("哈娅喜欢巧克力。", 3, "keep-this-time"),
                conn.execute("SELECT body,version,updated_at FROM profile_documents WHERE target='HAYA'").fetchone(),
            )
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM profile_document_revisions").fetchone()[0])

    def test_same_day_target_is_skipped_on_rerun_and_model_failure_saves_nothing(self):
        first, _ = self.run_result(self.response(haya="HAYA version one", haya_ids=[101]))
        self.assertEqual(["HAYA"], first["updated_targets"])
        second, _ = self.run_result(
            self.response(haya="HAYA should not overwrite", haya_ids=[101]),
        )
        self.assertEqual([], second["updated_targets"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(("HAYA version one", 1),
                             conn.execute("SELECT body,version FROM profile_documents WHERE target='HAYA'").fetchone())
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM profile_document_revisions").fetchone()[0])

        failed_db = str(self.root / "failed.db")
        calls = []

        def fails_second(request, authority):
            calls.append(request)
            if len(calls) == 2:
                raise RuntimeError("temporary provider failure")
            return self.result(self.response(haya="uncommitted", haya_ids=[101]), authority)

        with self.assertRaisesRegex(RuntimeError, "temporary provider failure"):
            update_from_trace(
                failed_db, source_day="2026-11-08", trace=self.trace, authority=self.authority,
                generate_fn=fails_second, chunk_chars=100,
            )
        with sqlite3.connect(failed_db) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM profile_documents").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM profile_document_revisions").fetchone()[0])

    def test_update_day_freezes_current_chat_authority_once(self):
        block = SimpleNamespace(source_day="2026-11-07", created_at="now", generation_id="g1", chunk_id="b1")
        captured = []
        used = []

        def capture():
            captured.append(self.authority)
            return self.authority

        def generate(request, authority):
            used.append(authority)
            return self.result(self.response(), authority)

        with patch("tools.daily_profile_updater.load_ready_dayblocks", return_value=((block,), ())):
            with patch("tools.daily_profile_updater.trace_ready_dayblock", return_value=self.trace):
                result = update_day(
                    "2026-11-07", chat_db_path="ignored", dayblock_db_path="ignored",
                    profile_db_path=self.db, authority_capture=capture,
                    background_generator=generate, chunk_chars=1,
                )
        self.assertEqual(1, len(captured))
        self.assertTrue(used)
        self.assertTrue(all(authority is self.authority for authority in used))
        self.assertEqual("api_relay", result["provider"])
        self.assertEqual("relay/test-model", result["model_identity"])

    def test_no_ready_dayblock_returns_without_creating_documents(self):
        path = str(self.root / "no-block.db")
        with patch("tools.daily_profile_updater.load_ready_dayblocks", return_value=((), ())):
            result = update_day("2026-11-07", dayblock_db_path="unused", profile_db_path=path)
        self.assertEqual("no_ready_dayblock", result["status"])
        self.assertFalse(Path(path).exists())

    def test_profile_updater_does_not_call_candidate_kernel_or_ombre(self):
        import tools.memory_candidates as candidates
        import tools.memory_kernel_store as kernel_store
        import tools.memory_tool as memory_tool

        data = self.response(haya="HAYA revised", haya_ids=[101])
        with patch.object(candidates, "create_candidate") as candidate_create, \
             patch.object(kernel_store, "MemoryKernel") as kernel, \
             patch.object(kernel_store, "_project_one") as ombre, \
             patch.object(memory_tool, "save_memory") as save_memory:
            self.run_result(data)
        candidate_create.assert_not_called()
        kernel.assert_not_called()
        ombre.assert_not_called()
        save_memory.assert_not_called()

    def test_migration_creates_only_two_document_tables(self):
        db = str(self.root / "migration.db")
        Path(db).touch()
        migrate(db)
        with sqlite3.connect(db) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(
                {"profile_documents", "profile_document_revisions", "sqlite_sequence"}, names,
            )
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM profile_documents").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
