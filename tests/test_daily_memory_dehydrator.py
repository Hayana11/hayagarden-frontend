import ast
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chat.background_generation import BackgroundGenerationResult
from tools.daily_memory_dehydrator import (
    _chunks, _message_text, update_day, update_from_trace,
)
from tools.memory_candidates import CandidateSourceTrace, load_candidate
from tools.media_asset_audit import audit_media_assets
from tools.media_assets import save_media_asset

ROOT = Path(__file__).resolve().parents[1]


class DailyMemoryDehydratorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = str(self.root / "candidate-preview.db")
        self.trace = CandidateSourceTrace(
            source_refs=("chat_messages:101", "chat_messages:102"),
            derived_refs=("dayblock:2026-11-07:generation-abc",),
            unresolved_refs=(),
            raw_messages=(
                {"id": 101, "author": "hayana", "content": "哈娅：" + "猫" * 120},
                {"id": 102, "author": "assistant", "content": "费奥多尔：" + "星" * 120},
            ),
            evidence_text="派生摘要只做导航，不作为证据",
        )
        self.authority = SimpleNamespace(provider="api_relay", model_identity="claude-opus-5-5")

    def tearDown(self):
        self.temp.cleanup()

    def result(self, candidates, authority=None):
        authority = authority or self.authority
        return BackgroundGenerationResult(
            text=json.dumps({"candidates": candidates}, ensure_ascii=False),
            provider=authority.provider, model_identity=authority.model_identity,
            actual_executor="test-background",
        )

    def run_trace(self, candidates, *, chunk_chars=32_000, generate=None):
        calls = []

        def generate_fn(request, authority):
            calls.append((request, authority))
            if generate:
                return generate(request, authority)
            return self.result(candidates, authority)

        result = update_from_trace(
            self.db, source_day="2026-11-07", trace=self.trace,
            authority=self.authority, generate_fn=generate_fn, chunk_chars=chunk_chars,
        )
        return result, calls

    def test_no_ready_dayblock_makes_no_model_call_or_candidate(self):
        captures = []
        generator_calls = []
        with patch("tools.daily_memory_dehydrator.load_ready_dayblocks", return_value=((), ())), \
             patch("tools.daily_memory_dehydrator.trace_ready_dayblock") as trace:
            result = update_day(
                "2026-11-07", candidate_db_path=self.db,
                authority_capture=lambda: captures.append(True),
                background_generator=lambda request, authority: generator_calls.append(True),
            )
        self.assertEqual("no_ready_dayblock", result["status"])
        self.assertEqual(0, result["model_calls"])
        self.assertEqual([], captures)
        self.assertEqual([], generator_calls)
        trace.assert_not_called()
        self.assertFalse(Path(self.db).exists())

    def test_empty_model_result_creates_no_candidate(self):
        result, calls = self.run_trace([])
        self.assertEqual(1, len(calls))
        self.assertEqual(0, result["model_candidate_count"])
        self.assertEqual(0, result["candidate_count"])
        self.assertFalse(Path(self.db).exists())

    def test_valid_candidate_persists_only_its_evidence_refs(self):
        text = "持续项目的关键决定：项目下周开始，双方约定先完成原型。"
        result, _ = self.run_trace([{"content": text, "evidence_message_ids": [101]}])
        with sqlite3.connect(self.db) as db:
            candidate_id, = db.execute("SELECT candidate_id FROM memory_candidates").fetchone()
        item = load_candidate(self.db, candidate_id)
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual("memory", item["candidate_kind"])
        self.assertEqual({"content": text}, item["payload"])
        self.assertEqual("chat_messages", item["source_type"])
        self.assertEqual(["chat_messages:101"], item["source_refs"])
        self.assertEqual(["dayblock:2026-11-07:generation-abc"], item["derived_refs"])
        self.assertEqual("daily_dehydrator", item["producer"])
        self.assertFalse(item["explicit_intent"])
        self.assertEqual("resolved", item["provenance_status"])
        self.assertEqual("pending", item["status"])

    def test_out_of_range_evidence_discards_only_its_candidate(self):
        result, _ = self.run_trace([
            {"content": "有原文支持的候选。", "evidence_message_ids": [101]},
            {"content": "引用了不存在消息的候选。", "evidence_message_ids": [999]},
        ])
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT payload_json,source_refs_json FROM memory_candidates").fetchall()
        self.assertEqual(2, result["model_candidate_count"])
        self.assertEqual(1, result["validated_candidate_count"])
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual(1, len(rows))
        self.assertEqual('["chat_messages:101"]', rows[0][1])

    def test_same_day_rerun_keeps_one_candidate_id_and_row(self):
        content = "明确约定：原型完成后再决定是否继续。"
        data = [{"content": content, "evidence_message_ids": [101]}]
        first, _ = self.run_trace(data)
        second, _ = self.run_trace(data)
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT candidate_id FROM memory_candidates").fetchall()
        self.assertEqual(1, first["candidate_count"])
        self.assertEqual(1, second["candidate_count"])
        self.assertEqual(1, len(rows))
        self.assertEqual(first["candidates"][0]["candidate_id"], rows[0][0])
        self.assertEqual(first["candidates"], second["candidates"])

    def test_model_failure_after_first_chunk_writes_zero_candidates(self):
        proposals = [{"content": "首段提到的承诺。", "evidence_message_ids": [101]}]
        calls = []

        def generate(request, authority):
            calls.append(authority)
            if len(calls) == 1:
                return self.result(proposals, authority)
            raise RuntimeError("model unavailable")

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            self.run_trace([], chunk_chars=1, generate=generate)
        self.assertEqual(2, len(calls))
        self.assertFalse(Path(self.db).exists())

    def test_update_freezes_authority_once_for_all_message_chunks(self):
        captures = []
        calls = []
        block = SimpleNamespace(
            source_day="2026-11-07", created_at="2026-11-08T00:00:00Z",
            generation_id="generation-abc", chunk_id="dayblock-chunk-abc",
        )
        frozen = self.authority

        def generate(request, authority):
            calls.append((request, authority))
            return self.result([], authority)

        with patch("tools.daily_memory_dehydrator.load_ready_dayblocks", return_value=((block,), ())), \
             patch("tools.daily_memory_dehydrator.trace_ready_dayblock", return_value=self.trace):
            result = update_day(
                "2026-11-07", candidate_db_path=self.db, chunk_chars=80,
                authority_capture=lambda: captures.append(True) or frozen,
                background_generator=generate,
            )
        self.assertEqual(1, len(captures))
        self.assertEqual(2, len(calls))
        self.assertTrue(all(authority is frozen for _, authority in calls))
        self.assertEqual("api_relay", result["provider"])
        self.assertEqual("claude-opus-5-5", result["model_identity"])
        self.assertIn("[msg:101][author=hayana]", calls[0][0].prompt_text)
        self.assertIn("猫" * 120, calls[0][0].prompt_text)
        self.assertIn("[msg:102][author=assistant]", calls[1][0].prompt_text)
        self.assertEqual(2, result["model_calls"])

    def _image_trace(self, *, duplicate_ref=False):
        image_urls = ["/static/uploads/chat-image.png"]
        if duplicate_ref:
            image_urls.append("/static/uploads/same-bytes.png")
        return CandidateSourceTrace(
            source_refs=("chat_messages:101", "chat_messages:102"),
            derived_refs=("dayblock:2026-11-07:generation-abc",),
            unresolved_refs=(),
            raw_messages=(
                {"id": 101, "author": "hayana", "content": "这张图记录了项目方案。",
                 "attachments": json.dumps([{"type": "image", "url": url} for url in image_urls])},
                {"id": 102, "author": "assistant", "content": "我会记下决定。", "attachments": "[]"},
            ),
            evidence_text="派生摘要只做导航",
        )

    def _run_image_trace(self, trace, evidence_ids, *, annotator=None, media_db=None, media_root=None):
        prompts = []
        result = update_from_trace(
            self.db, source_day="2026-11-07", trace=trace, authority=self.authority,
            generate_fn=lambda request, authority: (
                prompts.append(request.prompt_text)
                or self.result([{"content": "项目方案已确认。", "evidence_message_ids": evidence_ids}], authority)
            ),
            image_resolver=lambda ref: (b"same image bytes", "image/png"),
            image_annotator=annotator or (lambda data, mime: {
                "caption": "屏幕上有一张表格", "ocr_text": "方案 A", "entities": ["屏幕"],
                "model_identity": "relay-vision-test",
            }),
            media_db_path=media_db or str(self.root / "media.db"),
            media_root=media_root or str(self.root / "media-root"),
        )
        return result, prompts

    def test_candidate_image_evidence_promotes_asset_and_stable_ref(self):
        trace = self._image_trace()
        first, _ = self._run_image_trace(trace, [101])
        second, _ = self._run_image_trace(trace, [101])
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT candidate_id,source_refs_json FROM memory_candidates").fetchall()
        with sqlite3.connect(self.root / "media.db") as db:
            assets = db.execute("SELECT asset_id,sha256,caption,ocr_text,entities_json FROM media_assets").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(first["candidates"][0]["candidate_id"], rows[0][0])
        refs = json.loads(rows[0][1])
        self.assertEqual(["chat_messages:101", first["candidates"][0]["media_asset_refs"][0]], refs)
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertEqual(1, len(assets))
        self.assertTrue(assets[0][0].startswith("sha256-"))
        self.assertEqual(assets[0][0][7:], assets[0][1])
        self.assertEqual("屏幕上有一张表格", assets[0][2])
        self.assertEqual("方案 A", assets[0][3])
        self.assertEqual('["屏幕"]', assets[0][4])
        self.assertEqual({"total": 1, "missing_file": 0, "hash_mismatch": 0, "duplicate_sha": 0},
                         audit_media_assets(str(self.root / "media.db"), str(self.root / "media-root")))

    def test_visual_annotation_is_separate_from_original_chat_text(self):
        result, prompts = self._run_image_trace(self._image_trace(), [101])
        self.assertEqual(1, result["candidate_count"])
        prompt = prompts[0]
        self.assertIn("聊天原文：\n这张图记录了项目方案。", prompt)
        self.assertIn("[机器视觉标注，仅供证据理解，不代表用户或助手说过]", prompt)
        self.assertIn("caption: 屏幕上有一张表格", prompt)
        self.assertLess(prompt.index("聊天原文"), prompt.index("[机器视觉标注"))

    def test_vision_failure_keeps_text_candidate_and_blank_asset_annotation(self):
        result, _ = self._run_image_trace(
            self._image_trace(), [101], annotator=lambda _data, _mime: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        self.assertEqual(1, result["candidate_count"])
        with sqlite3.connect(self.root / "media.db") as db:
            caption, ocr, entities = db.execute(
                "SELECT caption,ocr_text,entities_json FROM media_assets"
            ).fetchone()
        self.assertEqual(("", "", "[]"), (caption, ocr, entities))

    def test_uncited_image_is_not_promoted(self):
        result, _ = self._run_image_trace(self._image_trace(), [102])
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual([], result["candidates"][0]["media_asset_refs"])
        self.assertFalse((self.root / "media.db").exists())
        self.assertFalse((self.root / "media-root" / "gallery" / "evidence").exists())

    def test_distinct_refs_with_same_bytes_share_one_asset(self):
        result, _ = self._run_image_trace(self._image_trace(duplicate_ref=True), [101])
        self.assertEqual(1, result["candidate_count"])
        with sqlite3.connect(self.root / "media.db") as db:
            count, = db.execute("SELECT COUNT(*) FROM media_assets").fetchone()
        self.assertEqual(1, count)
        self.assertEqual(1, len(result["candidates"][0]["media_asset_refs"]))

    def test_audit_reports_missing_and_hash_mismatch_without_repair(self):
        db_path = str(self.root / "gallery.db")
        root = self.root / "media-root"
        missing_id = save_media_asset(b"missing bytes", "image/png", db_path=db_path, data_root=root)
        bad_id = save_media_asset(b"original bytes", "image/jpeg", db_path=db_path, data_root=root)
        with sqlite3.connect(db_path) as db:
            paths = dict(db.execute("SELECT asset_id,storage_uri FROM media_assets").fetchall())
        (root / paths[missing_id]).unlink()
        (root / paths[bad_id]).write_bytes(b"corrupted bytes")
        self.assertEqual(
            {"total": 2, "missing_file": 1, "hash_mismatch": 1, "duplicate_sha": 0},
            audit_media_assets(db_path, root),
        )

    def test_same_hash_with_mime_aliases_still_uses_one_asset(self):
        db_path = str(self.root / "gallery.db")
        root = self.root / "media-root"
        first = save_media_asset(b"same bytes", "image/png", db_path=db_path, data_root=root)
        second = save_media_asset(b"same bytes", "image/jpeg", db_path=db_path, data_root=root)
        self.assertEqual(first, second)
        with sqlite3.connect(db_path) as db:
            count, = db.execute("SELECT COUNT(*) FROM media_assets").fetchone()
        self.assertEqual(1, count)

    def test_gallery_db_old_mem_id_is_not_modified_by_media_assets(self):
        db_path = str(self.root / "gallery.db")
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE gallery_photos (pid TEXT PRIMARY KEY, mem_id INTEGER)")
            db.execute("INSERT INTO gallery_photos VALUES ('old-photo', 731)")
        save_media_asset(b"evidence bytes", "image/png", db_path=db_path, data_root=self.root / "media-root")
        with sqlite3.connect(db_path) as db:
            mem_id, = db.execute("SELECT mem_id FROM gallery_photos WHERE pid='old-photo'").fetchone()
        self.assertEqual(731, mem_id)

    def test_messages_are_never_truncated_or_split(self):
        rows = list(self.trace.raw_messages)
        chunks = _chunks(rows, budget=1)
        self.assertEqual(2, len(chunks))
        self.assertEqual(rows[0], chunks[0][0])
        self.assertEqual(rows[1], chunks[1][0])
        self.assertIn("猫" * 120, _message_text(rows[0]))
        self.assertIn("author=hayana", _message_text(rows[0]))

    def test_dehydrator_has_no_kernel_ombre_or_profile_writer_imports(self):
        tree = ast.parse((ROOT / "tools/daily_memory_dehydrator.py").read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module.split(".")[0])
        for forbidden in ("memory_tool", "memory_kernel_store", "memory_kernel", "ombre"):
            self.assertNotIn(forbidden, imports)
        source = (ROOT / "tools/daily_memory_dehydrator.py").read_text()
        self.assertNotIn("profile_documents", source)
        self.assertNotIn("profile_document_revisions", source)

    def test_cron_uses_module_and_deployment_retires_old_schedules(self):
        cron = (ROOT / "deploy/cron.d/hayagarden-daily-memory-dehydrator").read_text()
        self.assertIn("CRON_TZ=UTC", cron)
        self.assertIn("15 19 * * * root cd /opt/frontend &&", cron)
        self.assertIn("python3.11 -m tools.daily_memory_dehydrator", cron)
        self.assertFalse((ROOT / "deploy/cron.d/hayagarden-daily-profile-updater").exists())
        deploy = (ROOT / "scripts/deploy-frontend.sh").read_text()
        self.assertNotIn('install -o root -g root -m 0644 "$staging/deploy/cron.d/hayagarden-daily-profile-updater"', deploy)
        self.assertIn('rm -f "$PROFILE_CRON_PATH"', deploy)
        self.assertIn('install -o root -g root -m 0644 "$staging/deploy/cron.d/hayagarden-daily-memory-dehydrator"', deploy)
        self.assertIn('crontab -l > "$ROOT_CRONTAB_BACKUP"', deploy)
        self.assertIn('awk \'index($0, "/opt/frontend/tools/fact_extractor.py") == 0\'', deploy)
        self.assertIn('crontab "$ROOT_CRONTAB_BACKUP"', deploy)
        self.assertIn('cp -a "$PROFILE_CRON_BACKUP" "$PROFILE_CRON_PATH"', deploy)
        self.assertIn('cp -a "$DEHYDRATOR_CRON_BACKUP" "$DEHYDRATOR_CRON_PATH"', deploy)
        self.assertIn('rm -f "$DEHYDRATOR_CRON_PATH"', deploy)
        self.assertIn('rm -f "$PROFILE_CRON_BACKUP" "$DEHYDRATOR_CRON_BACKUP" "$MAIL_CRON_BACKUP" "$ROOT_CRONTAB_BACKUP"', deploy)
        crontab_lines = [
            "40 5 * * * /usr/bin/python3.11 /opt/frontend/tools/fact_extractor.py >> /var/log/fact_extractor.log 2>&1",
            "0 6 * * * /usr/bin/python3.11 /opt/frontend/tools/daily_summary.py >> /var/log/daily_summary.log 2>&1",
            "# keep this unrelated schedule",
        ]
        filtered = subprocess.run(
            ["awk", 'index($0, "/opt/frontend/tools/fact_extractor.py") == 0'],
            input="\n".join(crontab_lines) + "\n", text=True, capture_output=True, check=True,
        )
        self.assertEqual("\n".join(crontab_lines[1:]) + "\n", filtered.stdout)

    def test_fact_extractor_is_marked_legacy_retired(self):
        source = (ROOT / "tools/fact_extractor.py").read_text()
        self.assertIn("LEGACY RETIRED", source[:200])
        self.assertIn("no longer scheduled", source[:300])


if __name__ == "__main__":
    unittest.main()
