"""阶段 1A：Claude Code Usage 行李透视单元测试（全部 mock 模型与网络）。"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import cc_usage_observability as obs
from cc_resident import empty_usage, normalize_cache_info, summarize_rounds

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=TZ)


class FakeProc:
    def __init__(self, lines):
        import io
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.stderr = io.StringIO()
        self._code = None
        self.pid = 4242

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0

    def kill(self):
        self._code = -9

    def wait(self, timeout=None):
        return self._code or 0


def _fp(**overrides):
    base = {
        "gateway_instance_id": "gw-1",
        "resident_generation": 3,
        "static_system_sha256": "a" * 64,
        "mcp_config_sha256": "b" * 64,
        "allowed_tools_sha256": "c" * 64,
        "model": None,
        "effort": None,
    }
    base.update(overrides)
    return base


def _usage(
    *,
    rounds,
    respawn_reason=None,
    resident_turn_count=2,
    runtime=None,
    breakdown=None,
    last_round_context=None,
):
    u = summarize_rounds(
        rounds,
        resident_turn_count=resident_turn_count,
        respawn_reason=respawn_reason,
    )
    if last_round_context is not None:
        u["last_round_context"] = last_round_context
    if runtime is not None:
        u["runtime"] = runtime
    if breakdown is not None:
        u["context_breakdown"] = breakdown
        u["observation_version"] = 1
    return u


class TokenEstimateTests(unittest.TestCase):
    def test_cjk_and_ascii_stable(self):
        self.assertEqual(obs.estimate_tokens_heuristic_cjk1_ascii4_v1("你好"), 2)
        self.assertEqual(obs.estimate_tokens_heuristic_cjk1_ascii4_v1("abcd"), 1)
        self.assertEqual(obs.estimate_tokens_heuristic_cjk1_ascii4_v1("abcde"), 2)
        mixed = "你好abcd"
        a = obs.estimate_tokens_heuristic_cjk1_ascii4_v1(mixed)
        b = obs.estimate_tokens_heuristic_cjk1_ascii4_v1(mixed)
        self.assertEqual(a, b)
        self.assertEqual(a, 2 + 1)
        self.assertEqual(obs.ESTIMATION_METHOD, "heuristic_cjk1_ascii4_v1")


class FingerprintTests(unittest.TestCase):
    def test_stable_and_changes_with_config(self):
        a = obs.sha256_text("STATIC_A")
        b = obs.sha256_text("STATIC_A")
        c = obs.sha256_text("STATIC_B")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        rt1 = obs.build_runtime(
            resident_generation=1,
            resident_pid=10,
            resident_turn_count=1,
            respawn_reason="process_dead",
            idle_seconds_before_turn=None,
            is_cold=True,
            static_system="S1",
            mcp_config_text='{"mcpServers":{}}',
            allowed_tools="mcp__home__light_on",
            instance_id="gw",
        )
        rt2 = obs.build_runtime(
            resident_generation=1,
            resident_pid=10,
            resident_turn_count=1,
            respawn_reason="process_dead",
            idle_seconds_before_turn=None,
            is_cold=True,
            static_system="S1",
            mcp_config_text='{"mcpServers":{"x":1}}',
            allowed_tools="mcp__home__light_on",
            instance_id="gw",
        )
        self.assertEqual(rt1["static_system_sha256"], rt2["static_system_sha256"])
        self.assertNotEqual(rt1["mcp_config_sha256"], rt2["mcp_config_sha256"])


class InstrumentationByteSafetyTests(unittest.TestCase):
    def test_measurement_does_not_mutate_prompt_bytes(self):
        system = "角色设定\n\n说明书\n\n[[SAVE: x]]"
        content = "以下是对话\n\n哈娅：你好"
        persona, note, save = "角色设定", "说明书", "[[SAVE: x]]"
        system_before = system.encode("utf-8")
        content_before = content
        bd = obs.build_context_breakdown(
            persona=persona,
            stable_note=note,
            save_instr=save,
            full_system=system,
            cold_once_text="长期记忆",
            history_bootstrap_text=content,
            state_text="【当前状态】灯关",
            state_mode="snapshot",
            memory_recall_text="",
            group_delta_text="",
            one_shot_text="wake",
            user_text="你好",
            final_content=content,
            is_cold=True,
            first_round_context_tokens=1000,
        )
        self.assertEqual(system.encode("utf-8"), system_before)
        self.assertEqual(content, content_before)
        self.assertEqual(bd["estimation_method"], "heuristic_cjk1_ascii4_v1")
        self.assertEqual(bd["confidence"], "low")


class CompatibilityTests(unittest.TestCase):
    def test_v1_v2_empty_invalid(self):
        self.assertEqual(obs.parse_cache_info_row("")[0], "empty")
        self.assertEqual(obs.parse_cache_info_row(None)[0], "empty")
        self.assertEqual(obs.parse_cache_info_row("{")[0], "invalid_json")
        kind, data = obs.parse_cache_info_row('{"cache_read":1,"cache_creation":2}')
        self.assertEqual(kind, "legacy")
        self.assertEqual(data["cache_read"], 1)
        kind, data = obs.parse_cache_info_row(
            {"v": 2, "provider": "claude_code", "num_rounds": 1, "rounds": []}
        )
        self.assertEqual(kind, "valid_v2")
        norm = normalize_cache_info(
            {
                "v": 2,
                "provider": "claude_code",
                "num_rounds": 1,
                "rounds": [],
                "observation_version": 1,
                "context_breakdown": {"confidence": "low"},
                "runtime": {"gateway_instance_id": "x"},
            }
        )
        self.assertEqual(norm["observation_version"], 1)
        self.assertEqual(norm["context_breakdown"]["confidence"], "low")
        self.assertEqual(norm["runtime"]["gateway_instance_id"], "x")


class ColdStartVsCacheMissTests(unittest.TestCase):
    def test_not_confused(self):
        cold = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 0, "cache_creation": 19413, "context_tokens": 19414,
            }],
            respawn_reason="idle",
            resident_turn_count=1,
            runtime=_fp(idle_seconds_before_turn=10, resident_generation=1, is_cold=True),
        )
        self.assertTrue(obs.is_cold_start(cold))
        self.assertFalse(obs.is_no_respawn_cache_miss(cold))

        miss = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 0, "cache_creation": 40878, "context_tokens": 40879,
            }],
            respawn_reason=None,
            resident_turn_count=5,
            runtime=_fp(idle_seconds_before_turn=30, resident_generation=3, is_cold=False),
        )
        self.assertFalse(obs.is_cold_start(miss))
        self.assertTrue(obs.is_no_respawn_cache_miss(miss))
        self.assertIs(obs.classify_suspected_cache_expiry(miss, prev_runtime=_fp()), False)


class MultiRoundClassificationTests(unittest.TestCase):
    def test_uses_first_round(self):
        usage = _usage(
            rounds=[
                {
                    "index": 1, "complete": True,
                    "input_tokens": 1, "output_tokens": 10,
                    "cache_read": 0, "cache_creation": 100, "context_tokens": 101,
                },
                {
                    "index": 2, "complete": True,
                    "input_tokens": 2, "output_tokens": 20,
                    "cache_read": 5000, "cache_creation": 0, "context_tokens": 5002,
                },
            ],
            respawn_reason=None,
            resident_turn_count=4,
            runtime=_fp(idle_seconds_before_turn=10, is_cold=False),
        )
        self.assertTrue(obs.is_no_respawn_cache_miss(usage))
        self.assertEqual(usage["cache_read"], 5000)
        self.assertEqual(usage["last_round_context"], 5002)


class CumulativeReadNotContextTests(unittest.TestCase):
    def test_sum_read_not_context(self):
        rounds = [
            {
                "index": i, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 78000, "cache_creation": 0, "context_tokens": 78001,
            }
            for i in range(1, 4)
        ]
        usage = summarize_rounds(rounds)
        self.assertEqual(usage["cache_read"], 234000)
        self.assertEqual(usage["last_round_context"], 78001)
        self.assertLess(usage["last_round_context"], usage["cache_read"])


class ToolSchemaTests(unittest.TestCase):
    def test_unavailable_is_null(self):
        bd = obs.build_context_breakdown(
            full_system="s",
            final_content="hi",
            tool_schema_text=None,
            allowed_tool_count=3,
        )
        self.assertIsNone(bd["tool_schema_tokens_estimate"])
        self.assertEqual(bd["tool_schema_measurement_status"], "unavailable")
        self.assertIsNone(bd["tool_count"])
        self.assertEqual(bd["allowed_tool_count"], 3)
        # 不得用工具名冒充 schema
        fake = obs.resolve_tool_schema(
            tool_schema_text="mcp__home__light_on",
            tool_schema_source="tool_names",
            tool_count=1,
        )
        self.assertIsNone(fake["tool_schema_tokens_estimate"])
        self.assertEqual(fake["tool_schema_measurement_status"], "unavailable")
        ok = obs.resolve_tool_schema(
            tool_schema_text='{"name":"light_on","inputSchema":{}}',
            tool_schema_source="mcp_list_tools",
            tool_count=1,
        )
        self.assertEqual(ok["tool_schema_measurement_status"], "available")
        self.assertIsInstance(ok["tool_schema_tokens_estimate"], int)


class OneShotClassificationTests(unittest.TestCase):
    def test_wake_task_dream_in_one_shot(self):
        from chat.system_builder import format_one_shot
        text = format_one_shot({
            "wake_feedback": "## 你醒着的时候\n巡夜",
            "task_feedback": "## 任务结果\n完成",
            "dream_flash": "忽然想起来",
            "feedback_ids": [1],
            "dream_id": 2,
        })
        bd = obs.build_context_breakdown(
            full_system="s",
            one_shot_text=text,
            final_content=text,
            user_text="hi",
        )
        self.assertGreater(bd["one_shot_tokens_estimate"], 0)
        self.assertIn("巡夜", text)
        self.assertIn("完成", text)
        self.assertIn("忽然想起来", text)


class ExpiryFingerprintTests(unittest.TestCase):
    def test_missing_fingerprint_unknown(self):
        usage = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 0, "cache_creation": 59557, "context_tokens": 59558,
            }],
            respawn_reason=None,
            resident_turn_count=8,
            runtime={
                "idle_seconds_before_turn": 6900,
                # 缺指纹
            },
        )
        self.assertIsNone(obs.classify_suspected_cache_expiry(usage, prev_runtime=None))
        self.assertIsNone(
            obs.classify_suspected_cache_expiry(usage, prev_runtime={"gateway_instance_id": "x"})
        )

    def test_requires_same_resident_generation(self):
        rt = _fp(idle_seconds_before_turn=6900, resident_generation=3)
        usage = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 0, "cache_creation": 59557, "context_tokens": 59558,
            }],
            respawn_reason=None,
            resident_turn_count=8,
            runtime=rt,
        )
        prev = _fp(resident_generation=2)
        self.assertIs(obs.classify_suspected_cache_expiry(usage, prev_runtime=prev), False)
        self.assertIs(
            obs.classify_suspected_cache_expiry(usage, prev_runtime=_fp(resident_generation=3)),
            True,
        )

    def test_requires_all_runtime_fingerprints(self):
        rt = _fp(idle_seconds_before_turn=6900, model="m1")
        usage = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 0, "cache_creation": 59557, "context_tokens": 59558,
            }],
            respawn_reason=None,
            resident_turn_count=8,
            runtime=rt,
        )
        prev = _fp(model="m2")
        self.assertIs(obs.classify_suspected_cache_expiry(usage, prev_runtime=prev), False)
        self.assertIs(
            obs.classify_suspected_cache_expiry(usage, prev_runtime=_fp(model="m1")),
            True,
        )


class ReportAggregateTests(unittest.TestCase):
    def _exact_fixture(self):
        """匿名真实口径 fixture：26 turns / 27 rounds / 指定 totals。"""
        cold_creations = [19413, 19212, 19513, 20387]
        self.assertAlmostEqual(sum(cold_creations) / 4.0, 19631.25)
        rows = []
        n = 0

        def add(usage):
            nonlocal n
            n += 1
            # 单调时间戳，保证聚合时序与插入顺序一致
            rows.append({
                "id": n,
                "created_at": "2026-07-18 10:%02d:%02d" % ((n - 1) // 60, (n - 1) % 60),
                "cache_info": json.dumps(usage, ensure_ascii=False),
            })

        for i, cc in enumerate(cold_creations):
            add(_usage(
                rounds=[{
                    "index": 1, "complete": True,
                    "input_tokens": 2, "output_tokens": 100,
                    "cache_read": 0, "cache_creation": cc, "context_tokens": 2 + cc,
                }],
                respawn_reason="idle",
                resident_turn_count=1,
                runtime=_fp(idle_seconds_before_turn=None, resident_generation=i + 1, is_cold=True),
                breakdown={
                    "cold_once_tokens_estimate": 1200,
                    "static_system_tokens_estimate": 5000,
                    "unattributed_bootstrap_tokens_estimate": 80,
                    "known_visible_context_tokens_estimate": 6200,
                    "tool_schema_tokens_estimate": None,
                },
            ))
        add(_usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 80,
                "cache_read": 0, "cache_creation": 40878, "context_tokens": 40879,
            }],
            respawn_reason=None,
            resident_turn_count=2,
            runtime=_fp(idle_seconds_before_turn=20, resident_generation=4, is_cold=False),
            breakdown={"static_system_tokens_estimate": 5000},
        ))
        add(_usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 90,
                "cache_read": 0, "cache_creation": 59557, "context_tokens": 59558,
            }],
            respawn_reason=None,
            resident_turn_count=3,
            runtime=_fp(idle_seconds_before_turn=6900, resident_generation=4, is_cold=False),
            breakdown={"static_system_tokens_estimate": 5000},
        ))
        # legacy：无指纹 → expiry unknown
        n += 1
        rows.append({
            "id": n,
            "created_at": "2026-07-18 10:%02d:%02d" % ((n - 1) // 60, (n - 1) % 60),
            "cache_info": json.dumps({
                "cache_read": 10, "cache_creation": 5, "input_tokens": 1, "output_tokens": 2,
            }),
        })

        # accounted: turns=7 rounds=7 input=11 output=572 creation=178965 read=10
        remain_input = 79 - 11
        remain_output = 12042 - 572
        remain_creation = 225622 - 178965
        remain_read = 1076858 - 10

        in_a1, in_a2 = 1, 1
        out_a1, out_a2 = 100, 100
        r_a1 = remain_read // 2
        r_a2 = remain_read - r_a1
        add(_usage(
            rounds=[
                {
                    "index": 1, "complete": True,
                    "input_tokens": in_a1, "output_tokens": out_a1,
                    "cache_read": r_a1, "cache_creation": remain_creation,
                    "context_tokens": in_a1 + r_a1 + remain_creation,
                },
                {
                    "index": 2, "complete": True,
                    "input_tokens": in_a2, "output_tokens": out_a2,
                    "cache_read": r_a2, "cache_creation": 0,
                    "context_tokens": in_a2 + r_a2,
                },
            ],
            respawn_reason=None,
            resident_turn_count=4,
            runtime=_fp(idle_seconds_before_turn=5, resident_generation=4, is_cold=False),
            breakdown={"static_system_tokens_estimate": 5000, "user_tokens_estimate": 4},
        ))
        left_input = remain_input - (in_a1 + in_a2)
        left_output = remain_output - (out_a1 + out_a2)
        for i in range(18):
            if i < 17:
                inp = left_input // (18 - i)
                outp = left_output // (18 - i)
            else:
                inp = left_input
                outp = left_output
            left_input -= inp
            left_output -= outp
            add(_usage(
                rounds=[{
                    "index": 1, "complete": True,
                    "input_tokens": inp, "output_tokens": outp,
                    "cache_read": 0, "cache_creation": 0,
                    "context_tokens": inp,
                }],
                respawn_reason=None,
                resident_turn_count=5 + i,
                runtime=_fp(idle_seconds_before_turn=5, resident_generation=4, is_cold=False),
                breakdown={"static_system_tokens_estimate": 5000, "user_tokens_estimate": 2},
            ))
        return rows

    def test_daily_pads_empty_dates_and_weighted_share(self):
        rows = self._exact_fixture()
        report = obs.aggregate_cc_observability(rows, days=3, now=NOW)
        self.assertEqual(len(report["daily"]), 3)
        dates = [d["date"] for d in report["daily"]]
        self.assertEqual(dates, ["2026-07-16", "2026-07-17", "2026-07-18"])
        self.assertEqual(report["daily"][0]["total_user_turns"], 0)
        self.assertEqual(report["daily"][1]["total_user_turns"], 0)

        s = report["summary"]
        self.assertEqual(s["total_user_turns"], 26)
        self.assertEqual(s["total_model_rounds"], 27)
        self.assertEqual(s["input_tokens"], 79)
        self.assertEqual(s["cache_read"], 1076858)
        self.assertEqual(s["cache_creation"], 225622)
        self.assertEqual(s["output_tokens"], 12042)
        self.assertEqual(s["cold_start_count"], 4)
        self.assertEqual(s["no_respawn_cache_miss_count"], 2)
        self.assertEqual(s["suspected_cache_expiry_count"], 1)
        self.assertGreaterEqual(s["suspected_cache_expiry_unknown_count"], 1)

        cold_sum = 19413 + 19212 + 19513 + 20387
        expected_share = cold_sum / 225622
        self.assertAlmostEqual(s["cold_start_creation_share"], expected_share)
        # 禁止用每日百分比平均：只有一天有数据时仍是 token 加权总和
        self.assertNotEqual(s["cold_start_creation_share"], 1.0)

        av = report["breakdown_averages"]["static_system_tokens_estimate"]
        self.assertIn("sample_count", av)
        self.assertGreater(av["sample_count"], 0)
        self.assertEqual(av["value"], 5000.0)

        # null 不参与平均
        tool_avg = report["breakdown_averages"]["tool_schema_tokens_estimate"]
        self.assertIsNone(tool_avg["value"])
        self.assertEqual(tool_avg["sample_count"], 0)

        cov = report["coverage"]
        self.assertEqual(cov["total_candidate_rows"], 26)
        self.assertGreater(cov["valid_v2_rows"], 0)
        self.assertEqual(cov["legacy_rows"], 1)
        self.assertIn("breakdown_coverage_pct", cov)

    def test_api_cli_same_aggregate(self):
        rows = self._exact_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "memories.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE chat_messages ("
                "id INTEGER PRIMARY KEY, author TEXT, content TEXT, "
                "created_at TEXT, cache_info TEXT)"
            )
            for r in rows:
                conn.execute(
                    "INSERT INTO chat_messages (id, author, content, created_at, cache_info) "
                    "VALUES (?, 'assistant', 'x', ?, ?)",
                    (r["id"], r["created_at"], r["cache_info"]),
                )
            conn.commit()
            conn.close()
            from_db = obs.build_report_from_db(db, days=3, now=NOW)
            from_rows = obs.aggregate_cc_observability(rows, days=3, now=NOW)
            self.assertEqual(from_db["summary"], from_rows["summary"])
            self.assertEqual(from_db["coverage"], from_rows["coverage"])
            self.assertEqual(from_db["breakdown_averages"], from_rows["breakdown_averages"])

            # CLI（与 API 共用同一聚合纯函数）
            import importlib.util
            import io
            cli_path = Path(ROOT) / "scripts" / "report_cc_usage.py"
            spec = importlib.util.spec_from_file_location("report_cc_usage", cli_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = mod.main(["--db", db, "--days", "3", "--format", "json"])
            self.assertEqual(rc, 0)
            cli_report = json.loads(buf.getvalue())
            self.assertEqual(cli_report["summary"], from_rows["summary"])


class UnattributedTests(unittest.TestCase):
    def test_cold_only_and_limitations(self):
        bd = obs.build_context_breakdown(
            full_system="system" * 100,
            cold_once_text="cold" * 50,
            history_bootstrap_text="hist" * 50,
            user_text="hi",
            final_content="payload",
            is_cold=True,
            first_round_context_tokens=5000,
            tool_schema_text=None,
        )
        self.assertIsInstance(bd["unattributed_bootstrap_tokens_estimate"], int)
        hot = obs.finalize_breakdown_with_usage(bd, empty_usage(), is_cold=False)
        self.assertIsNone(hot["unattributed_bootstrap_tokens_estimate"])
        text = " ".join(obs.LIMITATIONS)
        self.assertIn("Claude Code 内部提示", text)
        self.assertIn("不得直接解释为 MCP", text)


class ResidentGenerationTests(unittest.TestCase):
    def test_spawn_increments_generation(self):
        from cc_resident import ResidentSession
        sess = ResidentSession("/tmp", "tool_a", "/tmp/cc-tools.json")
        self.assertEqual(sess.generation, 0)
        with mock.patch("subprocess.Popen", return_value=FakeProc([])):
            sess._spawn("S", {}, reason="process_dead")
            sess._spawn("S", {}, reason="idle")
        self.assertEqual(sess.generation, 2)


class IdleBeforeLastUsedTests(unittest.TestCase):
    def test_idle_computed_before_last_used_update(self):
        from cc_resident import ResidentSession
        sess = ResidentSession("/tmp", "", "/tmp/cc-tools.json")
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess"}),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "message_start",
                    "message": {"usage": {
                        "input_tokens": 1, "output_tokens": 1,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 10,
                    }},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
            }),
            json.dumps({"type": "result", "is_error": False, "result": "ok"}),
        ]
        with mock.patch("subprocess.Popen", return_value=FakeProc(lines)):
            sess._spawn("S", {}, reason="process_dead")
        # first success
        out = list(sess.send_turn("hello"))
        self.assertEqual(out[-1][0], "done")
        usage = out[-1][1][2]
        self.assertIsNone(usage.get("_obs_idle_seconds_before_turn"))
        sess._last_used = 1000.0
        lines2 = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "message_start",
                    "message": {"usage": {
                        "input_tokens": 1, "output_tokens": 1,
                        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0,
                    }},
                },
            }),
            json.dumps({
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "yo"}},
            }),
            json.dumps({"type": "result", "is_error": False, "result": "ok"}),
        ]
        sess._proc = FakeProc(lines2)
        with mock.patch("time.time", return_value=1600.0):
            out2 = list(sess.send_turn("next"))
        usage2 = out2[-1][1][2]
        self.assertEqual(usage2["_obs_idle_seconds_before_turn"], 600.0)


class ApiRouteSmokeTests(unittest.TestCase):
    def test_route_exists_and_daily_cost_untouched(self):
        source = (Path(ROOT) / "app.py").read_text(encoding="utf-8")
        self.assertIn("/api/usage/cc-observability", source)
        # daily-cost 函数体仍在，且本任务未改其聚合语义入口
        self.assertIn("def usage_daily_cost", source)
        self.assertIn("def usage_cc_observability", source)


class RollingSummaryAbsentTests(unittest.TestCase):
    def test_not_in_cc_prompt(self):
        bd = obs.build_context_breakdown(
            full_system="s",
            rolling_summary_text="很长的滚动摘要",
            rolling_summary_in_prompt=False,
            final_content="x",
        )
        self.assertEqual(bd["rolling_summary_tokens_estimate"], 0)
        self.assertIs(bd["rolling_summary_source_present"], False)


if __name__ == "__main__":
    unittest.main()
