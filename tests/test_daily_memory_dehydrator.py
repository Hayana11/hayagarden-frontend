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
        self.assertIn('rm -f "$PROFILE_CRON_BACKUP" "$DEHYDRATOR_CRON_BACKUP" "$ROOT_CRONTAB_BACKUP"', deploy)
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
