"""阶段 1A：Claude Code Usage 行李透视单元测试（全部 mock 模型与网络）。"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import cc_usage_observability as obs
from cc_resident import empty_usage, normalize_cache_info, summarize_rounds
from tools.cc_jsonl_usage import attach_jsonl_usage, replay_jsonl_lines, replay_jsonl_path

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
    """完整运行时指纹。model/effort 默认给真实值；测 unknown 时显式传 null。"""
    base = {
        "gateway_instance_id": "gw-1",
        "resident_generation": 3,
        "static_system_sha256": "a" * 64,
        "tools_sha256": "d" * 64,
        "mcp_config_sha256": "b" * 64,
        "allowed_tools_sha256": "c" * 64,
        "tool_schema_sha256": "d" * 64,
        "tool_schema_source": "mcp_list_tools",
        "tool_schema_measurement_status": "available",
        "model": "claude-sonnet",
        "effort": "high",
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

    def test_cold_user_message_not_double_counted(self):
        """history_bootstrap 已含最后用户消息时，known_visible 不得再加一遍 user。"""
        system = "STATIC_SYSTEM_TEXT"
        user = "你好呀这是用户最后一句"
        history = (
            "以下是你们今天到目前为止的对话记录：\n\n"
            "哈娅：更早的话\n费奥多尔：回复\n哈娅：" + user + "\n\n"
            "请回复最后一条消息。"
        )
        content = "cold_once\n\n" + history
        bd = obs.build_context_breakdown(
            full_system=system,
            cold_once_text="cold_once",
            history_bootstrap_text=history,
            user_text=user,
            final_content=content,
            is_cold=True,
            first_round_context_tokens=50_000,
        )
        expected_known = (
            obs.estimate_tokens_heuristic_cjk1_ascii4_v1(system)
            + obs.estimate_tokens_heuristic_cjk1_ascii4_v1(content)
        )
        self.assertEqual(bd["known_visible_context_tokens_estimate"], expected_known)
        # 分项仍记录 user，但不参与 known_visible 相加
        self.assertGreater(bd["user_tokens_estimate"], 0)
        # 若错误双计，known 会更大、unattributed 更小
        wrong_double = expected_known + bd["user_tokens_estimate"]
        self.assertLess(bd["known_visible_context_tokens_estimate"], wrong_double)


class CompatibilityTests(unittest.TestCase):
    def test_v1_v2_empty_invalid(self):
        self.assertEqual(obs.parse_cache_info_row("")[0], "empty")
        self.assertEqual(obs.parse_cache_info_row(None)[0], "empty")
        self.assertEqual(obs.parse_cache_info_row("{")[0], "invalid_json")
        kind, data = obs.parse_cache_info_row('{"cache_read":1,"cache_creation":2}')
        self.assertEqual(kind, "ambiguous_legacy")
        self.assertEqual(data["cache_read"], 1)
        kind, data = obs.parse_cache_info_row(
            {"v": 2, "provider": "claude_code", "num_rounds": 1, "rounds": []}
        )
        self.assertEqual(kind, "valid_v2")
        kind, _ = obs.parse_cache_info_row(
            {"v": 2, "provider": "api_relay", "cache_read": 99, "cache_creation": 1}
        )
        self.assertEqual(kind, "other_provider")
        kind, _ = obs.parse_cache_info_row({"v": 2, "cache_read": 1})
        self.assertEqual(kind, "ambiguous_legacy")
        kind, _ = obs.parse_cache_info_row({"v": 2, "provider": "", "cache_read": 1})
        self.assertEqual(kind, "ambiguous_legacy")
        kind, _ = obs.parse_cache_info_row({"v": 2, "provider": None, "cache_read": 1})
        self.assertEqual(kind, "ambiguous_legacy")
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
        # 未采集 tool result → null，不是 0
        self.assertIsNone(bd["tool_result_tokens_estimate"])
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

    def test_tool_result_null_when_not_measured(self):
        bd = obs.build_context_breakdown(
            full_system="s",
            final_content="hi",
            tool_result_text="",
            tool_result_measured=False,
        )
        self.assertIsNone(bd["tool_result_tokens_estimate"])
        measured = obs.build_context_breakdown(
            full_system="s",
            final_content="hi",
            tool_result_text="tool output here",
            tool_result_measured=True,
        )
        self.assertIsInstance(measured["tool_result_tokens_estimate"], int)
        self.assertGreater(measured["tool_result_tokens_estimate"], 0)


class OneShotClassificationTests(unittest.TestCase):
    def test_wake_task_dream_in_one_shot(self):
        from chat.system_builder import format_one_shot
        text = format_one_shot({
            "wake_nonmessage_background": "- [01:00] 巡夜",
            "wake_message_background": "",
            "wake_reply_bridge": "【连续对话·紧邻上一句】\n桥",
            "task_feedback": "## 任务结果\n完成",
            "dream_flash": "忽然想起来",
            "feedback_ids": [1],
            "dream_id": 2,
            "wake_ids": [9],
        })
        # bridge 不进 format_one_shot，由热轮单独紧贴用户消息
        self.assertNotIn("连续对话", text)
        self.assertNotIn("桥", text)
        self.assertIn("巡夜", text)
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

    def test_null_model_effort_unknown(self):
        """生产传入 model=None/effort=None 时不得判 true。"""
        rt = _fp(idle_seconds_before_turn=6900, model=None, effort=None)
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
        prev = _fp(model=None, effort=None)
        self.assertIsNone(obs.classify_suspected_cache_expiry(usage, prev_runtime=prev))
        # 仅 model null
        rt2 = _fp(idle_seconds_before_turn=6900, model=None, effort="high")
        usage2 = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 0, "cache_creation": 1, "context_tokens": 2,
            }],
            respawn_reason=None,
            resident_turn_count=8,
            runtime=rt2,
        )
        self.assertIsNone(
            obs.classify_suspected_cache_expiry(usage2, prev_runtime=_fp(model=None, effort="high"))
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

    def test_partial_tool_surface_miss_is_unknown(self):
        rt = _fp(idle_seconds_before_turn=6900, tool_schema_measurement_status="partial")
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
        prev = _fp(tool_schema_measurement_status="partial")
        self.assertEqual(
            obs.classify_cache_miss_reason(usage, prev_runtime=prev),
            "unknown",
        )
        self.assertIsNone(obs.classify_suspected_cache_expiry(usage, prev_runtime=prev))

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
        """匿名真实口径 fixture：26 Claude turns / 27 rounds / 指定 totals。

        另附 api_relay / ambiguous_legacy 行（不计 Claude 总量）。
        """
        cold_creations = [19413, 19212, 19513, 20387]
        self.assertAlmostEqual(sum(cold_creations) / 4.0, 19631.25)
        rows = []
        n = 0

        def add(usage):
            nonlocal n
            n += 1
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
                    "history_bootstrap_tokens_estimate": 800,
                    "static_system_tokens_estimate": 5000,
                    "unattributed_bootstrap_tokens_estimate": 80,
                    "known_visible_context_tokens_estimate": 6200,
                    "tool_schema_tokens_estimate": None,
                    "tool_result_tokens_estimate": None,
                },
            ))
        # 短间隔 miss → expiry false
        add(_usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 80,
                "cache_read": 0, "cache_creation": 40878, "context_tokens": 40879,
            }],
            respawn_reason=None,
            resident_turn_count=2,
            runtime=_fp(idle_seconds_before_turn=20, resident_generation=4, is_cold=False),
            breakdown={
                "static_system_tokens_estimate": 5000,
                "cold_once_tokens_estimate": 0,  # 生产热轮形状；聚合不得稀释冷启动平均
                "history_bootstrap_tokens_estimate": 0,
                "tool_result_tokens_estimate": None,
            },
        ))
        # 长 idle + 完整指纹 → expiry true
        add(_usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 90,
                "cache_read": 0, "cache_creation": 59557, "context_tokens": 59558,
            }],
            respawn_reason=None,
            resident_turn_count=3,
            runtime=_fp(idle_seconds_before_turn=6900, resident_generation=4, is_cold=False),
            breakdown={
                "static_system_tokens_estimate": 5000,
                "cold_once_tokens_estimate": 0,
                "history_bootstrap_tokens_estimate": 0,
                "tool_result_tokens_estimate": None,
            },
        ))
        # 长 idle 但 model/effort 缺失 → expiry unknown（仍计 Claude 总量）
        add(_usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 2,
                "cache_read": 0, "cache_creation": 5, "context_tokens": 6,
            }],
            respawn_reason=None,
            resident_turn_count=4,
            runtime=_fp(
                idle_seconds_before_turn=6900,
                resident_generation=4,
                is_cold=False,
                model=None,
                effort=None,
            ),
            breakdown={
                "static_system_tokens_estimate": 5000,
                "cold_once_tokens_estimate": 0,
                "history_bootstrap_tokens_estimate": 0,
                "tool_result_tokens_estimate": None,
            },
        ))

        # accounted Claude: turns=7 rounds=7 input=11 output=572 creation=178965 read=0
        remain_input = 79 - 11
        remain_output = 12042 - 572
        remain_creation = 225622 - 178965
        remain_read = 1076858

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
            resident_turn_count=5,
            runtime=_fp(idle_seconds_before_turn=5, resident_generation=4, is_cold=False),
            breakdown={
                "static_system_tokens_estimate": 5000,
                "user_tokens_estimate": 4,
                "cold_once_tokens_estimate": 0,
                "history_bootstrap_tokens_estimate": 0,
                "tool_result_tokens_estimate": None,
            },
            last_round_context=in_a2 + r_a2,
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
                resident_turn_count=6 + i,
                runtime=_fp(idle_seconds_before_turn=5, resident_generation=4, is_cold=False),
                breakdown={
                    "static_system_tokens_estimate": 5000,
                    "user_tokens_estimate": 2,
                    "cold_once_tokens_estimate": 0,
                    "history_bootstrap_tokens_estimate": 0,
                    "tool_result_tokens_estimate": None,
                },
            ))

        # 污染行：不计 Claude 总量
        n += 1
        rows.append({
            "id": n,
            "created_at": "2026-07-18 11:00:00",
            "cache_info": json.dumps({
                "v": 2,
                "provider": "api_relay",
                "cache_read": 999999,
                "cache_creation": 888888,
                "input_tokens": 777,
                "output_tokens": 666,
                "num_rounds": 1,
                "rounds": [{"cache_read": 999999, "cache_creation": 888888}],
            }),
        })
        n += 1
        rows.append({
            "id": n,
            "created_at": "2026-07-18 11:00:01",
            "cache_info": json.dumps({
                "cache_read": 10, "cache_creation": 5, "input_tokens": 1, "output_tokens": 2,
            }),
        })
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
        self.assertEqual(s["no_respawn_cache_miss_count"], 3)
        self.assertEqual(s["normal_hot_turn_count"], 1)  # 仅 first_round.read>0 的双 round 热轮
        self.assertEqual(s["unclassified_turn_count"], 18)  # read=0/create=0 filler
        self.assertEqual(s["suspected_cache_expiry_count"], 1)
        self.assertGreaterEqual(s["suspected_cache_expiry_unknown_count"], 1)
        day = report["daily"][-1]
        self.assertEqual(day["unclassified_turn_count"], 18)
        self.assertEqual(day["normal_hot_turn_count"], 1)

        cold_sum = 19413 + 19212 + 19513 + 20387
        expected_share = cold_sum / 225622
        self.assertAlmostEqual(
            s["cold_start_first_round_creation_share_of_total_creation"], expected_share
        )
        self.assertEqual(
            s["cold_start_creation_share"],
            s["cold_start_first_round_creation_share_of_total_creation"],
        )
        self.assertNotEqual(s["cold_start_first_round_creation_share_of_total_creation"], 1.0)
        self.assertEqual(s["cold_start_first_round_creation_sum"], cold_sum)
        self.assertAlmostEqual(s["cold_start_avg_first_round_creation"], 19631.25)
        self.assertIn("idle", s["respawns_by_reason"])
        self.assertEqual(s["respawns_by_reason"]["idle"], 4)

        av = report["breakdown_averages"]["static_system_tokens_estimate"]
        self.assertIn("sample_count", av)
        self.assertGreater(av["sample_count"], 0)
        self.assertEqual(av["value"], 5000.0)

        # 冷启动专属字段不被热轮 0 稀释
        cold_avg = report["breakdown_averages"]["cold_once_tokens_estimate"]
        self.assertEqual(cold_avg["sample_count"], 4)
        self.assertEqual(cold_avg["value"], 1200.0)

        tool_avg = report["breakdown_averages"]["tool_schema_tokens_estimate"]
        self.assertIsNone(tool_avg["value"])
        self.assertEqual(tool_avg["sample_count"], 0)
        tool_res = report["breakdown_averages"]["tool_result_tokens_estimate"]
        self.assertIsNone(tool_res["value"])
        self.assertEqual(tool_res["sample_count"], 0)

        cov = report["coverage"]
        self.assertEqual(cov["valid_v2_rows"], 26)
        self.assertEqual(cov["other_provider_rows"], 1)
        self.assertEqual(cov["ambiguous_legacy_rows"], 1)
        self.assertEqual(cov["total_candidate_rows"], 28)
        self.assertEqual(cov["v2_coverage_denominator"], 26)
        self.assertEqual(cov["all_candidate_coverage_denominator"], 28)
        self.assertEqual(cov["v2_coverage_pct"], 100.0)
        self.assertLess(cov["all_candidate_coverage_pct"], 100.0)

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
                rc = mod.main([
                    "--db", db, "--days", "3", "--format", "json",
                    "--now", NOW.isoformat(),
                ])
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
        with mock.patch("subprocess.Popen", side_effect=lambda *a, **k: FakeProc([])), \
             mock.patch("chat.cc_runtime.require_managed_claude_runtime", return_value="2.1.280"), \
             mock.patch("chat.cc_runtime.claude_cmd_for_version", side_effect=lambda _version, *a, **k: ["/managed/2.1.280", *a]), \
             mock.patch("chat.cc_model.cc_model_runtime_compatibility", return_value=(True, None)):
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
            json.dumps({"type": "result", "is_error": False, "result": "ok", "stop_reason": "end_turn"}),
        ]
        with mock.patch("subprocess.Popen", return_value=FakeProc(lines)), \
             mock.patch("chat.cc_runtime.require_managed_claude_runtime", return_value="2.1.280"), \
             mock.patch("chat.cc_runtime.claude_cmd_for_version", side_effect=lambda _version, *a, **k: ["/managed/2.1.280", *a]):
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
            json.dumps({"type": "result", "is_error": False, "result": "ok", "stop_reason": "end_turn"}),
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
            is_cold=True,
        )
        self.assertEqual(bd["rolling_summary_tokens_estimate"], 0)
        self.assertIs(bd["rolling_summary_source_present"], False)


class ProviderIsolationTests(unittest.TestCase):
    def test_api_relay_not_in_claude_totals(self):
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-18 10:00:00",
                "cache_info": json.dumps({
                    "v": 2,
                    "provider": "claude_code",
                    "num_rounds": 1,
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cache_read": 10,
                    "cache_creation": 20,
                    "rounds": [{
                        "index": 1, "input_tokens": 3, "output_tokens": 4,
                        "cache_read": 10, "cache_creation": 20, "context_tokens": 33,
                    }],
                    "runtime": _fp(idle_seconds_before_turn=1),
                }),
            },
            {
                "id": 2,
                "created_at": "2026-07-18 10:01:00",
                "cache_info": json.dumps({
                    "v": 2,
                    "provider": "api_relay",
                    "num_rounds": 1,
                    "input_tokens": 1000,
                    "output_tokens": 2000,
                    "cache_read": 3000,
                    "cache_creation": 4000,
                    "rounds": [{"cache_read": 3000, "cache_creation": 4000}],
                }),
            },
        ]
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        self.assertEqual(report["summary"]["total_user_turns"], 1)
        self.assertEqual(report["summary"]["input_tokens"], 3)
        self.assertEqual(report["summary"]["cache_creation"], 20)
        self.assertEqual(report["coverage"]["other_provider_rows"], 1)
        self.assertEqual(report["coverage"]["valid_v2_rows"], 1)

    def test_ambiguous_legacy_not_in_claude_totals(self):
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-18 10:00:00",
                "cache_info": json.dumps({
                    "v": 2,
                    "provider": "claude_code",
                    "num_rounds": 1,
                    "input_tokens": 5,
                    "output_tokens": 6,
                    "cache_read": 7,
                    "cache_creation": 8,
                    "rounds": [{
                        "index": 1, "input_tokens": 5, "output_tokens": 6,
                        "cache_read": 7, "cache_creation": 8, "context_tokens": 20,
                    }],
                }),
            },
            {
                "id": 2,
                "created_at": "2026-07-18 10:01:00",
                "cache_info": json.dumps({
                    "cache_read": 999, "cache_creation": 888, "input_tokens": 77, "output_tokens": 66,
                }),
            },
        ]
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        self.assertEqual(report["summary"]["total_user_turns"], 1)
        self.assertEqual(report["summary"]["input_tokens"], 5)
        self.assertEqual(report["summary"]["cache_read"], 7)
        self.assertEqual(report["coverage"]["ambiguous_legacy_rows"], 1)


class HotZeroDilutionTests(unittest.TestCase):
    def test_hot_cold_once_zero_does_not_dilute(self):
        rows = []
        for i, cold_val in enumerate((1000, 2000)):
            rows.append({
                "id": i + 1,
                "created_at": "2026-07-18 10:0%d:00" % i,
                "cache_info": json.dumps(_usage(
                    rounds=[{
                        "index": 1, "complete": True,
                        "input_tokens": 1, "output_tokens": 1,
                        "cache_read": 0, "cache_creation": 100, "context_tokens": 101,
                    }],
                    respawn_reason="idle",
                    resident_turn_count=1,
                    runtime=_fp(resident_generation=i + 1),
                    breakdown={
                        "cold_once_tokens_estimate": cold_val,
                        "history_bootstrap_tokens_estimate": 50,
                        "static_system_tokens_estimate": 10,
                    },
                )),
            })
        # 热轮生产形状：cold_once=0
        for i in range(8):
            rows.append({
                "id": 10 + i,
                "created_at": "2026-07-18 11:%02d:00" % i,
                "cache_info": json.dumps(_usage(
                    rounds=[{
                        "index": 1, "complete": True,
                        "input_tokens": 1, "output_tokens": 1,
                        "cache_read": 50, "cache_creation": 0, "context_tokens": 51,
                    }],
                    respawn_reason=None,
                    resident_turn_count=2 + i,
                    runtime=_fp(idle_seconds_before_turn=3, resident_generation=2),
                    breakdown={
                        "cold_once_tokens_estimate": 0,
                        "history_bootstrap_tokens_estimate": 0,
                        "static_system_tokens_estimate": 10,
                    },
                )),
            })
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        cold_avg = report["breakdown_averages"]["cold_once_tokens_estimate"]
        self.assertEqual(cold_avg["sample_count"], 2)
        self.assertEqual(cold_avg["value"], 1500.0)


class RealisticPercentileTests(unittest.TestCase):
    def test_median_p90_from_realistic_last_round_context(self):
        # 真实量级序列，避免被 filler context=4 掩盖逻辑错误
        contexts = [12000, 18000, 25000, 32000, 45000, 52000, 61000, 78000, 90000, 110000]
        rows = []
        for i, ctx in enumerate(contexts):
            rows.append({
                "id": i + 1,
                "created_at": "2026-07-18 12:%02d:00" % i,
                "cache_info": json.dumps(_usage(
                    rounds=[{
                        "index": 1, "complete": True,
                        "input_tokens": 2, "output_tokens": 20,
                        "cache_read": ctx - 2, "cache_creation": 0, "context_tokens": ctx,
                    }],
                    respawn_reason=None,
                    resident_turn_count=2,
                    runtime=_fp(idle_seconds_before_turn=1),
                    last_round_context=ctx,
                )),
            })
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        self.assertEqual(report["summary"]["median_last_round_context"], 48500.0)
        # _percentile: k=(n-1)*p = 8.1, f=8, c=9
        expected_p90 = contexts[8] * (9 - 8.1) + contexts[9] * (8.1 - 8)
        self.assertAlmostEqual(report["summary"]["p90_last_round_context"], expected_p90)
        self.assertGreater(report["summary"]["median_last_round_context"], 1000)


class EmptyCacheInfoCoverageTests(unittest.TestCase):
    def test_null_and_empty_enter_candidate_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "m.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE chat_messages ("
                "id INTEGER PRIMARY KEY, author TEXT, content TEXT, "
                "created_at TEXT, cache_info TEXT)"
            )
            rows = [
                (1, "assistant", "a", "2026-07-18 10:00:00", None),
                (2, "assistant", "b", "2026-07-18 10:01:00", ""),
                (3, "assistant", "c", "2026-07-18 10:02:00", json.dumps({
                    "v": 2, "provider": "claude_code", "num_rounds": 1,
                    "input_tokens": 1, "output_tokens": 1,
                    "cache_read": 0, "cache_creation": 10,
                    "rounds": [{"index": 1, "input_tokens": 1, "output_tokens": 1,
                                "cache_read": 0, "cache_creation": 10, "context_tokens": 11}],
                    "context_breakdown": {"static_system_tokens_estimate": 1},
                    "runtime": _fp(),
                })),
                (4, "assistant", "d", "2026-07-18 10:03:00", "{not-json"),
            ]
            conn.executemany(
                "INSERT INTO chat_messages (id, author, content, created_at, cache_info) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
            conn.commit()
            conn.close()
            report = obs.build_report_from_db(db, days=1, now=NOW)
            cov = report["coverage"]
            self.assertEqual(cov["total_candidate_rows"], 4)
            self.assertEqual(cov["empty_cache_info_rows"], 2)
            self.assertEqual(cov["invalid_json_rows"], 1)
            self.assertEqual(cov["valid_v2_rows"], 1)
            self.assertEqual(cov["all_candidate_coverage_denominator"], 4)
            self.assertEqual(cov["all_candidate_coverage_pct"], 25.0)


class CreationClassificationTests(unittest.TestCase):
    def test_three_class_creation_and_later_rounds(self):
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-18 10:00:00",
                "cache_info": json.dumps(_usage(
                    rounds=[
                        {"index": 1, "complete": True, "input_tokens": 1, "output_tokens": 1,
                         "cache_read": 0, "cache_creation": 100, "context_tokens": 101},
                        {"index": 2, "complete": True, "input_tokens": 1, "output_tokens": 1,
                         "cache_read": 50, "cache_creation": 20, "context_tokens": 71},
                    ],
                    respawn_reason="idle",
                    resident_turn_count=1,
                    runtime=_fp(resident_generation=1),
                )),
            },
            {
                "id": 2,
                "created_at": "2026-07-18 10:01:00",
                "cache_info": json.dumps(_usage(
                    rounds=[{
                        "index": 1, "complete": True, "input_tokens": 1, "output_tokens": 1,
                        "cache_read": 0, "cache_creation": 40, "context_tokens": 41,
                    }],
                    respawn_reason=None,
                    resident_turn_count=2,
                    runtime=_fp(idle_seconds_before_turn=10, resident_generation=1),
                )),
            },
            {
                "id": 3,
                "created_at": "2026-07-18 10:02:00",
                "cache_info": json.dumps(_usage(
                    rounds=[
                        {"index": 1, "complete": True, "input_tokens": 1, "output_tokens": 1,
                         "cache_read": 80, "cache_creation": 5, "context_tokens": 86},
                        {"index": 2, "complete": True, "input_tokens": 1, "output_tokens": 1,
                         "cache_read": 90, "cache_creation": 7, "context_tokens": 98},
                    ],
                    respawn_reason=None,
                    resident_turn_count=3,
                    runtime=_fp(idle_seconds_before_turn=5, resident_generation=1),
                )),
            },
            {
                "id": 4,
                "created_at": "2026-07-18 10:03:00",
                "cache_info": json.dumps(_usage(
                    rounds=[{
                        "index": 1, "complete": True, "input_tokens": 1, "output_tokens": 1,
                        "cache_read": 0, "cache_creation": 0, "context_tokens": 1,
                    }],
                    respawn_reason=None,
                    resident_turn_count=4,
                    runtime=_fp(idle_seconds_before_turn=5, resident_generation=1),
                )),
            },
        ]
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        s = report["summary"]
        self.assertEqual(s["cold_start_count"], 1)
        self.assertEqual(s["no_respawn_cache_miss_count"], 1)
        self.assertEqual(s["normal_hot_turn_count"], 1)
        self.assertEqual(s["unclassified_turn_count"], 1)
        self.assertEqual(s["cold_start_first_round_creation_sum"], 100)
        self.assertEqual(s["no_respawn_miss_first_round_creation_sum"], 40)
        self.assertEqual(s["normal_hot_first_round_creation_sum"], 5)
        self.assertEqual(s["unclassified_first_round_creation_sum"], 0)
        self.assertEqual(s["later_rounds_creation_sum"], 27)  # 20 + 7
        self.assertEqual(s["cold_start_avg_first_round_creation"], 100.0)
        self.assertEqual(s["no_respawn_miss_avg_first_round_creation"], 40.0)
        self.assertEqual(s["normal_hot_avg_first_round_creation"], 5.0)
        total = 100 + 20 + 40 + 5 + 7
        self.assertAlmostEqual(
            s["cold_start_first_round_creation_share_of_total_creation"], 100 / total
        )
        self.assertAlmostEqual(
            s["later_rounds_creation_share_of_total_creation"], 27 / total
        )
        self.assertEqual(s["unclassified_first_round_creation_share_of_total_creation"], 0.0)
        self.assertEqual(s["respawns_by_reason"], {"idle": 1})
        day = report["daily"][-1]
        self.assertEqual(day["cold_start_first_round_creation_sum"], 100)
        self.assertEqual(day["later_rounds_creation_sum"], 27)
        self.assertEqual(day["unclassified_turn_count"], 1)
        self.assertEqual(day["respawns_by_reason"], {"idle": 1})

    def test_read_zero_create_zero_not_normal_hot(self):
        usage = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 0, "cache_creation": 0, "context_tokens": 1,
            }],
            respawn_reason=None,
            resident_turn_count=3,
            runtime=_fp(idle_seconds_before_turn=5),
        )
        self.assertFalse(obs.is_cold_start(usage))
        self.assertFalse(obs.is_no_respawn_cache_miss(usage))
        self.assertFalse(obs.is_normal_hot(usage))

    def test_normal_hot_requires_read_gt_zero(self):
        hot = _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": 12, "cache_creation": 0, "context_tokens": 13,
            }],
            respawn_reason=None,
            resident_turn_count=3,
            runtime=_fp(idle_seconds_before_turn=5),
        )
        self.assertTrue(obs.is_normal_hot(hot))
        no_rounds = _usage(
            rounds=[],
            respawn_reason=None,
            resident_turn_count=3,
            runtime=_fp(idle_seconds_before_turn=5),
        )
        self.assertFalse(obs.is_normal_hot(no_rounds))


class ContentSnapshotGuardTests(unittest.TestCase):
    def test_list_content_inplace_mutation_detected(self):
        content = [{"type": "text", "text": "hello"}]
        snap = obs.snapshot_prompt_content(content)
        self.assertTrue(obs.prompt_content_unchanged(snap, content))
        content[0]["text"] = "MUTATED"
        self.assertFalse(obs.prompt_content_unchanged(snap, content))
        # 同对象赋值无法防原地修改
        same_ref = content
        self.assertTrue(same_ref is content)


class CanonicalStaticBuilderTests(unittest.TestCase):
    def test_parts_match_full_system_bytewise(self):
        from chat.system_builder import build_cc_static_parts, build_cc_static_system
        with mock.patch("chat.system_builder.read_persona", return_value="P"), \
             mock.patch("chat.system_builder.build_stable_note", return_value="N"), \
             mock.patch("chat.system_builder._build_tool_companion_intuition", return_value="T"), \
             mock.patch("chat.system_builder._CC_SAVE_INSTR", "S"):
            parts = build_cc_static_parts()
            full = build_cc_static_system()
        self.assertEqual(parts["full_system"], full)
        self.assertEqual(parts["full_system"], "P\n\nN\n\nT\n\nS")
        self.assertEqual(parts["persona"], "P")
        self.assertEqual(parts["stable_note"], "N")
        self.assertEqual(parts["tool_companion_intuition"], "T")
        self.assertEqual(parts["save_instr"], "S")


class DaysClampTests(unittest.TestCase):
    def test_clamp_1_to_90(self):
        self.assertEqual(obs.clamp_report_days(0), 1)
        self.assertEqual(obs.clamp_report_days(14), 14)
        self.assertEqual(obs.clamp_report_days(91), 90)
        self.assertEqual(obs.clamp_report_days("x", default=14), 14)


class ProviderInterleaveExpiryTests(unittest.TestCase):
    def _claude_hot(self, *, idle, generation=4, read=100, creation=0, turn=5):
        return _usage(
            rounds=[{
                "index": 1, "complete": True,
                "input_tokens": 1, "output_tokens": 1,
                "cache_read": read, "cache_creation": creation,
                "context_tokens": 1 + read + creation,
            }],
            respawn_reason=None,
            resident_turn_count=turn,
            runtime=_fp(idle_seconds_before_turn=idle, resident_generation=generation),
        )

    def _claude_miss(self, *, idle, generation=4, creation=59557, turn=6):
        return self._claude_hot(
            idle=idle, generation=generation, read=0, creation=creation, turn=turn,
        )

    def test_other_provider_does_not_break_claude_fingerprint_chain(self):
        """Claude → api_relay → 同指纹长 idle Claude miss → provider_changed。"""
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-18 10:00:00",
                "cache_info": json.dumps(self._claude_hot(idle=10)),
            },
            {
                "id": 2,
                "created_at": "2026-07-18 10:30:00",
                "cache_info": json.dumps({
                    "v": 2,
                    "provider": "api_relay",
                    "num_rounds": 1,
                    "input_tokens": 50,
                    "output_tokens": 50,
                    "cache_read": 0,
                    "cache_creation": 999,
                    "rounds": [{"cache_read": 0, "cache_creation": 999}],
                }),
            },
            {
                "id": 3,
                "created_at": "2026-07-18 12:00:00",
                "cache_info": json.dumps(self._claude_miss(idle=6900)),
            },
        ]
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        self.assertEqual(report["coverage"]["other_provider_rows"], 1)
        self.assertEqual(report["summary"]["total_user_turns"], 2)
        self.assertEqual(report["summary"]["no_respawn_cache_miss_count"], 1)
        self.assertEqual(report["summary"]["suspected_cache_expiry_count"], 0)
        self.assertEqual(report["summary"]["suspected_cache_expiry_unknown_count"], 0)
        self.assertEqual(
            report["summary"]["cache_miss_reasons"],
            {"provider_changed": 1},
        )

    def test_ambiguous_legacy_still_clears_fingerprint_chain(self):
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-18 10:00:00",
                "cache_info": json.dumps(self._claude_hot(idle=10)),
            },
            {
                "id": 2,
                "created_at": "2026-07-18 10:30:00",
                "cache_info": json.dumps({"cache_read": 1, "cache_creation": 2}),
            },
            {
                "id": 3,
                "created_at": "2026-07-18 12:00:00",
                "cache_info": json.dumps(self._claude_miss(idle=6900)),
            },
        ]
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        self.assertEqual(report["coverage"]["ambiguous_legacy_rows"], 1)
        self.assertEqual(report["summary"]["suspected_cache_expiry_count"], 0)
        self.assertEqual(report["summary"]["suspected_cache_expiry_unknown_count"], 1)

    def test_empty_cache_info_clears_fingerprint_chain(self):
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-18 10:00:00",
                "cache_info": json.dumps(self._claude_hot(idle=10)),
            },
            {
                "id": 2,
                "created_at": "2026-07-18 10:30:00",
                "cache_info": "",
            },
            {
                "id": 3,
                "created_at": "2026-07-18 12:00:00",
                "cache_info": json.dumps(self._claude_miss(idle=6900)),
            },
        ]
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        self.assertEqual(report["coverage"]["empty_cache_info_rows"], 1)
        self.assertEqual(report["summary"]["suspected_cache_expiry_count"], 0)
        self.assertEqual(report["summary"]["suspected_cache_expiry_unknown_count"], 1)

    def test_invalid_json_clears_fingerprint_chain(self):
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-18 10:00:00",
                "cache_info": json.dumps(self._claude_hot(idle=10)),
            },
            {
                "id": 2,
                "created_at": "2026-07-18 10:30:00",
                "cache_info": "{not-json",
            },
            {
                "id": 3,
                "created_at": "2026-07-18 12:00:00",
                "cache_info": json.dumps(self._claude_miss(idle=6900)),
            },
        ]
        report = obs.aggregate_cc_observability(rows, days=1, now=NOW)
        self.assertEqual(report["coverage"]["invalid_json_rows"], 1)
        self.assertEqual(report["summary"]["suspected_cache_expiry_count"], 0)
        self.assertEqual(report["summary"]["suspected_cache_expiry_unknown_count"], 1)


class TurnMeasurementTests(unittest.TestCase):
    def test_classify_turn_tags_semantic_labels(self):
        usage = {
            "num_rounds": 2,
            "rounds": [{"cache_read": 1000, "cache_creation": 0, "input_tokens": 0}],
            "resident_turn_count": 3,
        }
        breakdown = {
            "memory_recall_tokens_estimate": 120,
            "wake_reply_bridge_tokens_estimate": 80,
            "state_mode": "delta",
            "state_tokens_estimate": 40,
            "tool_result_tokens_estimate": 500,
            "files_tokens_estimate": 0,
            "non_text_block_count": 0,
        }
        tags = obs.classify_turn_tags(breakdown=breakdown, usage=usage, is_cold=False)
        self.assertIn("resident_hot", tags)
        self.assertIn("has_recall", tags)
        self.assertIn("has_tools", tags)
        self.assertIn("wake_reply", tags)
        self.assertIn("has_state_delta", tags)

    def test_build_turn_measurement_provider_block(self):
        usage = {
            "num_rounds": 1,
            "cache_read": 9000,
            "cache_creation": 100,
            "input_tokens": 50,
            "output_tokens": 200,
            "resident_turn_count": 1,
            "respawn_reason": "process_dead",
        }
        breakdown = obs.finalize_breakdown_with_usage(
            obs.build_context_breakdown(
                persona="p",
                stable_note="s",
                full_system="p\n\ns",
                is_cold=True,
                first_round_context_tokens=12000,
            ),
            usage,
            is_cold=True,
        )
        tm = obs.build_turn_measurement(breakdown=breakdown, usage=usage, is_cold=True)
        self.assertIn("resident_cold_start", tm["turn_tags"])
        self.assertEqual(tm["provider"]["cache_read"], 9000)
        self.assertIsNotNone(tm["components_estimate"]["persona"])


class ProviderBudgetReplayTests(unittest.TestCase):
    """Deterministic redacted replays of the existing gen35/46/48 JSONL usage."""

    _GENERATION_ROWS = (
        (35, 41338, 41608, 82948),
        (46, 0, 101487, 101489),
        (48, 43104, 66412, 109518),
    )

    @staticmethod
    def _assistant_jsonl(request_id, cache_read, cache_creation):
        return json.dumps({
            "type": "assistant",
            "requestId": request_id,
            "timestamp": "2026-10-14T00:00:00.000Z",
            "message": {
                "model": "claude-opus-5-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": cache_creation,
                    },
                },
            },
        })

    def test_gen35_gen46_gen48_first_round_context_is_deterministic(self):
        for generation, cache_read, cache_creation, expected_context in self._GENERATION_ROWS:
            result = replay_jsonl_lines([
                self._assistant_jsonl(
                    "req-generation-%s" % generation,
                    cache_read,
                    cache_creation,
                ),
            ])
            self.assertEqual(result["request_count"], 1)
            record = result["records"][0]
            context = (
                record["input_tokens"]
                + record["cache_read"]
                + record["cache_creation"]
            )
            self.assertEqual(context, expected_context)
            self.assertEqual(
                record["cache_read"] + record["cache_creation"],
                expected_context - 2,
            )

    def test_visible_payload_and_components_are_not_double_counted(self):
        system = "STATIC_SYSTEM"
        canonical = "CANONICAL_HISTORY"
        user = "CURRENT_USER"
        content = canonical + "\n" + user
        tool_schema = '{"name":"tool","input_schema":{}}'
        breakdown = obs.build_context_breakdown(
            full_system=system,
            history_bootstrap_text=canonical,
            user_text=user,
            final_content=content,
            tool_schema_text=tool_schema,
            tool_schema_source="static_registry",
            is_cold=True,
        )
        usage = summarize_rounds([{
            "index": 1,
            "complete": True,
            "input_tokens": 2,
            "output_tokens": 1,
            "cache_read": 0,
            "cache_creation": 1000,
            "context_tokens": 1002,
        }])
        finalized = obs.finalize_breakdown_with_usage(
            breakdown,
            usage,
            is_cold=True,
        )
        expected_known = (
            obs.estimate_tokens_heuristic_cjk1_ascii4_v1(system)
            + obs.estimate_tokens_heuristic_cjk1_ascii4_v1(content)
        )
        self.assertEqual(
            finalized["known_visible_context_tokens_estimate"],
            expected_known,
        )
        self.assertEqual(
            finalized["unattributed_bootstrap_tokens_estimate"],
            max(
                0,
                1002
                - expected_known
                - finalized["tool_schema_tokens_estimate"],
            ),
        )
        # The canonical history is already inside final_content. It must not
        # be added again through the explanatory history component.
        self.assertNotEqual(
            finalized["known_visible_context_tokens_estimate"],
            expected_known
            + obs.estimate_tokens_heuristic_cjk1_ascii4_v1(canonical),
        )
        self.assertEqual(
            finalized["provider_first_round_context_tokens"],
            1002,
        )
        self.assertEqual(
            finalized["tool_schema_measurement_status"],
            "available",
        )


class DailyProviderBudgetWiringTests(unittest.TestCase):
    def test_capacity_keeps_bootstrap_identity_without_cold_tag(self):
        from chat import daily_runtime

        plan = type("Plan", (), {})()
        plan.manifest = {
            "capacity_swap": True,
            "context_plan_id": "plan-1",
            "context_plan_hash": "hash-1",
            "context_plan_source_hash": "source-1",
            "model": "claude-opus-5-5",
        }
        plan.assembly = {
            "context_plan_representation_blocks": [
                {"body": "CANONICAL_HISTORY"},
            ],
            "state": "STATE",
        }
        plan.user_content = "CURRENT_USER"
        plan.resident_generation = 48
        plan.is_cold = False
        plan.is_respawn = False
        plan._provider_prompt_system_snapshot = "STATIC_SYSTEM"
        plan._provider_prompt_content_snapshot = (
            "CANONICAL_HISTORY\nCURRENT_USER"
        )
        plan._provider_prompt_bootstrap_kind = "capacity"
        resident = type("Resident", (), {})()
        resident.generation = 48
        resident.resident_pid = 1
        resident.session_id = "session-1"
        resident.tool_surface_snapshot = {}
        resident.allowed_tools = ""
        resident.mcp_config_path = None
        usage = {
            "rounds": [{
                "index": 1,
                "complete": True,
                "input_tokens": 2,
                "output_tokens": 1,
                "cache_read": 0,
                "cache_creation": 1000,
                "context_tokens": 1002,
            }],
            "num_rounds": 1,
            "input_tokens": 2,
            "output_tokens": 1,
            "cache_read": 0,
            "cache_creation": 1000,
            "resident_turn_count": 1,
        }

        observed = daily_runtime._attach_daily_provider_budget_observation(
            plan,
            resident=resident,
            static_system="STATIC_SYSTEM",
            usage=usage,
        )

        self.assertEqual(
            observed["context_breakdown"]["bootstrap_kind"],
            "capacity",
        )
        self.assertEqual(observed["runtime"]["bootstrap_kind"], "capacity")
        self.assertFalse(observed["runtime"]["is_cold"])
        self.assertEqual(
            observed["context_breakdown"]["provider_first_round_context_tokens"],
            1002,
        )


class DailyProviderBudgetContractTests(unittest.TestCase):
    @staticmethod
    def _plan(kind):
        plan = type("Plan", (), {})()
        plan.manifest = {
            "capacity_swap": kind == "capacity",
            "context_plan_id": "plan-1",
            "context_plan_hash": "hash-1",
            "context_plan_source_hash": "source-1",
            "model": "claude-opus-5-5",
        }
        plan.assembly = {
            "context_plan_representation_blocks": [
                {"body": "CANONICAL_HISTORY"},
            ],
            "state": "STATE",
        }
        plan.user_content = "CURRENT_USER"
        plan.resident_generation = 48
        plan.is_cold = kind == "cold"
        plan.is_respawn = kind == "respawn"
        plan._provider_prompt_system_snapshot = "STATIC_SYSTEM"
        plan._provider_prompt_content_snapshot = (
            "CANONICAL_HISTORY\nCURRENT_USER"
        )
        plan._provider_prompt_bootstrap_kind = kind
        return plan

    @staticmethod
    def _resident():
        resident = type("Resident", (), {})()
        resident.generation = 48
        resident.resident_pid = 4242
        resident.session_id = "session-1"
        resident.model_identity = "claude-opus-5-5"
        resident.tool_surface_snapshot = {}
        resident.allowed_tools = ""
        resident.mcp_config_path = None
        resident._last_provider_content = "REALITY_VISIBLE_PAYLOAD"
        return resident

    @staticmethod
    def _usage(receipt):
        from cc_resident import ResidentTurnUsage

        return ResidentTurnUsage(
            {
                "rounds": [{
                    "index": 1,
                    "complete": True,
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_read": 0,
                    "cache_creation": 1000,
                    "context_tokens": 1002,
                }],
                "num_rounds": 1,
                "input_tokens": 2,
                "output_tokens": 1,
                "cache_read": 0,
                "cache_creation": 1000,
                "resident_turn_count": 1,
                "_obs_effort": "high",
            },
            terminal_receipt=receipt,
            candidate_cache_refresh_at=12.5,
            candidate_cache_refresh_monotonic=22.5,
        )

    def test_presend_snapshot_failure_does_not_gate_provider_send(self):
        from chat import daily_runtime

        plan = self._plan("hot")
        from tools import cc_usage_observability as obs
        with mock.patch.object(
            obs,
            "snapshot_prompt_content",
            side_effect=RuntimeError("isolated snapshot failure"),
        ):
            daily_runtime._capture_daily_provider_prompt_snapshot(
                plan,
                content=[{"type": "text", "text": "USER_SECRET"}],
                effective_system="SYSTEM_SECRET",
                actual_cold=False,
            )

        self.assertIsNone(plan._provider_prompt_content_snapshot)
        self.assertEqual(plan._provider_prompt_bootstrap_kind, "hot")
        sent = []

        def fake_send_turn(content, **kwargs):
            sent.append((content, kwargs))
            return iter([("done", ("ok", "", {}, {}))])

        fake_send_turn("USER_SECRET", turn_lease={"turn_id": "t1"})
        self.assertEqual(sent[0][0], "USER_SECRET")
        self.assertEqual(sent[0][1]["turn_lease"], {"turn_id": "t1"})

    def test_done_keeps_terminal_receipt_cache_candidate_and_cleans_snapshots(self):
        from chat import daily_runtime

        for kind in ("cold", "respawn", "capacity", "hot"):
            plan = self._plan(kind)
            resident = self._resident()
            receipt = object()
            usage = self._usage(receipt)

            def fake_ensure(*args, **kwargs):
                yield ("text", "partial")
                yield ("done", ("final", "thinking", usage, {"claim": kind}))

            with mock.patch.object(
                daily_runtime,
                "ensure_resident_and_stream",
                side_effect=fake_ensure,
            ):
                events = list(daily_runtime.stream_daily_resident_turn(
                    plan,
                    resident=resident,
                    env={},
                    static_system="STATIC_SYSTEM",
                ))

            self.assertEqual([event for event, _ in events], ["text", "done"])
            done = events[-1][1]
            self.assertEqual(done[0:2], ("final", "thinking"))
            self.assertEqual(done[3], {"claim": kind})
            self.assertIs(done[2], usage)
            self.assertIs(usage.terminal_receipt, receipt)
            self.assertEqual(usage._candidate_cache_refresh_at, 12.5)
            self.assertEqual(usage._candidate_cache_refresh_monotonic, 22.5)
            self.assertNotIn("_obs_effort", usage)
            self.assertIsNone(plan._provider_prompt_content_snapshot)
            self.assertIsNone(plan._provider_prompt_system_snapshot)
            self.assertIsNone(resident._last_provider_content)
            self.assertEqual(
                done[2]["context_breakdown"]["bootstrap_kind"],
                kind,
            )
            self.assertEqual(
                done[2]["runtime"]["is_cold"],
                kind in ("cold", "respawn"),
            )

    def test_failed_observation_returns_original_usage_and_receipt(self):
        from chat import daily_runtime
        from cc_resident import ResidentTurnUsage

        receipt = object()
        usage = ResidentTurnUsage(
            {"input_tokens": 3},
            terminal_receipt=receipt,
        )
        plan = type("Plan", (), {})()
        plan.manifest = {}
        resident = type("Resident", (), {})()

        returned = daily_runtime._attach_daily_provider_budget_observation(
            plan,
            resident=resident,
            static_system="STATIC_SYSTEM",
            usage=usage,
        )

        self.assertIs(returned, usage)
        self.assertIs(usage.terminal_receipt, receipt)
        self.assertEqual(dict(usage), {"input_tokens": 3})

    def test_generator_close_releases_snapshots_without_done(self):
        from chat import daily_runtime

        plan = self._plan("hot")
        resident = self._resident()

        def fake_ensure(*args, **kwargs):
            yield ("text", "partial")

        with mock.patch.object(
            daily_runtime,
            "ensure_resident_and_stream",
            side_effect=fake_ensure,
        ):
            stream = daily_runtime.stream_daily_resident_turn(
                plan,
                resident=resident,
                env={},
                static_system="STATIC_SYSTEM",
            )
            next(stream)
            stream.close()

        self.assertIsNone(plan._provider_prompt_content_snapshot)
        self.assertIsNone(plan._provider_prompt_system_snapshot)
        self.assertIsNone(resident._last_provider_content)

    def test_generator_error_releases_snapshots_without_replacing_exception(self):
        from chat import daily_runtime

        plan = self._plan("hot")
        resident = self._resident()

        def fake_ensure(*args, **kwargs):
            yield ("text", "partial")
            raise ValueError("provider stream failed")

        with mock.patch.object(
            daily_runtime,
            "ensure_resident_and_stream",
            side_effect=fake_ensure,
        ):
            stream = daily_runtime.stream_daily_resident_turn(
                plan,
                resident=resident,
                env={},
                static_system="STATIC_SYSTEM",
            )
            with self.assertRaisesRegex(ValueError, "provider stream failed"):
                list(stream)

        self.assertIsNone(plan._provider_prompt_content_snapshot)
        self.assertIsNone(plan._provider_prompt_system_snapshot)
        self.assertIsNone(resident._last_provider_content)

    def test_observation_persistence_contains_no_raw_payload(self):
        from chat import daily_runtime

        plan = self._plan("cold")
        plan._provider_prompt_system_snapshot = "SYSTEM_SECRET"
        plan._provider_prompt_content_snapshot = "USER_SECRET"
        resident = self._resident()
        resident.tool_surface_snapshot = {
            "tool_schema_text": "TOOL_SECRET",
            "tool_schema_source": "static_registry",
            "tool_schema_sha256": "tool-hash",
            "tool_schema_measurement_status": "available",
            "tool_count": 1,
        }
        usage = self._usage(object())

        observed = daily_runtime._attach_daily_provider_budget_observation(
            plan,
            resident=resident,
            static_system="STATIC_SYSTEM",
            usage=usage,
        )
        serialized = json.dumps(dict(observed), ensure_ascii=False)
        self.assertNotIn("SYSTEM_SECRET", serialized)
        self.assertNotIn("USER_SECRET", serialized)
        self.assertNotIn("TOOL_SECRET", serialized)
        self.assertFalse(
            observed["context_breakdown"]["provider_envelope_known_visible_complete"],
        )


class FirstSendAccountingR2Tests(unittest.TestCase):
    @staticmethod
    def _receipt_fixture(path):
        payload = json.dumps({
            "type": "assistant",
            "requestId": "req-1",
            "message": {
                "model": "claude-opus-5-5",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 100,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 100,
                    },
                },
            },
        })
        path.write_text(payload + "\n", encoding="utf-8")
        replay = replay_jsonl_path(path)
        usage = {
            "rounds": [{
                "index": 1,
                "request_id": "req-1",
                "complete": True,
                "input_tokens": 2,
                "output_tokens": 1,
                "cache_read": 10,
                "cache_creation": 100,
                "context_tokens": 112,
            }],
            "input_tokens": 2,
            "output_tokens": 1,
            "cache_read": 10,
            "cache_creation": 100,
            "resident_turn_count": 1,
            "_obs_resident_generation": 7,
            "_obs_claude_session_id": "session-1",
        }
        usage = attach_jsonl_usage(usage, replay)
        path_hash = obs.sha256_text(str(path))
        turn = {
            "context_id": 1,
            "context_epoch": 2,
            "resident_generation": 3,
            "current_user_message_id": 99,
            "resident_turn_count": 1,
            "spawn_generation": 7,
            "provider_session_id": "session-1",
            "transcript_claude_session_id": "session-1",
            "transcript_process_generation": 7,
            "transcript_path_sha256": path_hash,
            "transcript_start_offset": 0,
            "transcript_end_offset": replay["end_offset"],
        }
        spawn = {
            "actual_spawn": True,
            "spawn_generation": 7,
            "spawn_identity_sha256": "spawn-hash",
            "spawn_argv_sha256": "argv-hash",
            "spawn_tool_flags_observation_status": "COMPLETE",
        }
        plan = {
            "source": "materialized_context_plan",
            "plan_id": "plan-1",
            "plan_hash": "plan-hash",
            "source_hash": "source-hash",
            "selected_token_estimate": 10,
            "fixed_token_estimate": 20,
            "reserve_budget": 8,
        }
        return usage, turn, spawn, plan

    def test_exact_jsonl_range_binds_but_unobservable_boundary_stays_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            usage, turn, spawn, plan = self._receipt_fixture(
                Path(directory) / "session.jsonl"
            )
            receipt = obs.build_first_send_accounting_receipt(
                plan_identity=plan,
                spawn_identity=spawn,
                turn_identity=turn,
                usage=usage,
                known_components=[{
                    "kind": "invariant_system",
                    "observation_status": "COMPLETE",
                    "identity_sha256": "system-hash",
                    "token_estimate": 10,
                }, {
                    "kind": "tool_schema",
                    "observation_status": "PARTIAL",
                    "identity_sha256": "schema-hash",
                    "token_estimate": None,
                }],
                known_context_tokens=30,
                manifest_assertions={"manifest_plan_hash_matches": False},
                observed_at="now",
            )
        self.assertEqual(receipt["binding_status"], "COMPLETE")
        self.assertEqual(receipt["coverage_status"], "PARTIAL")
        self.assertEqual(receipt["observation_status"], "PARTIAL")
        self.assertIsNotNone(receipt["jsonl_binding"]["event_start_offset"])
        self.assertIsNotNone(receipt["jsonl_binding"]["event_end_offset"])
        self.assertFalse(receipt["manifest_assertions"]["manifest_plan_hash_matches"])

    def test_missing_materialized_identity_is_unknown_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            usage, turn, spawn, plan = self._receipt_fixture(
                Path(directory) / "session.jsonl"
            )
            receipt = obs.build_first_send_accounting_receipt(
                plan_identity={},
                spawn_identity=spawn,
                turn_identity=turn,
                usage=usage,
                known_components=[],
                known_context_tokens=None,
                manifest_assertions={},
                observed_at="now",
            )
        self.assertEqual(receipt["binding_status"], "UNKNOWN")
        self.assertEqual(receipt["observation_status"], "UNKNOWN")

    def test_generation_or_request_mismatch_never_binds_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            usage, turn, spawn, plan = self._receipt_fixture(
                Path(directory) / "session.jsonl"
            )
            turn["transcript_process_generation"] = 8
            receipt = obs.build_first_send_accounting_receipt(
                plan_identity=plan,
                spawn_identity=spawn,
                turn_identity=turn,
                usage=usage,
                known_components=[],
                known_context_tokens=30,
                manifest_assertions={},
                observed_at="now",
            )
        self.assertNotEqual(receipt["binding_status"], "COMPLETE")

    def test_request_id_or_event_range_mismatch_downgrades(self):
        with tempfile.TemporaryDirectory() as directory:
            usage, turn, spawn, plan = self._receipt_fixture(
                Path(directory) / "session.jsonl"
            )
            usage["rounds"][0]["request_id"] = "wrong-request"
            turn["transcript_start_offset"] = 1
            receipt = obs.build_first_send_accounting_receipt(
                plan_identity=plan,
                spawn_identity=spawn,
                turn_identity=turn,
                usage=usage,
                known_components=[],
                known_context_tokens=30,
                manifest_assertions={},
                observed_at="now",
            )
        self.assertNotEqual(receipt["binding_status"], "COMPLETE")

    def test_exact_replay_is_idempotent(self):
        first = {"observation_status": "PARTIAL", "plan_identity": {"plan_id": "one"}}
        observed = obs.attach_observation(
            {"first_send_accounting": first},
            context_breakdown={},
            runtime={},
            first_send_accounting=first,
        )
        self.assertEqual(observed["first_send_accounting"], first)

    def test_unknown_child_prevents_complete_even_with_complete_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            usage, turn, spawn, plan = self._receipt_fixture(
                Path(directory) / "session.jsonl"
            )
            receipt = obs.build_first_send_accounting_receipt(
                plan_identity=plan,
                spawn_identity=spawn,
                turn_identity=turn,
                usage=usage,
                known_components=[{
                    "kind": "provider_internal",
                    "observation_status": "UNKNOWN",
                }],
                known_context_tokens=30,
                manifest_assertions={},
                observed_at="now",
            )
        self.assertEqual(receipt["binding_status"], "COMPLETE")
        self.assertEqual(receipt["coverage_status"], "UNKNOWN")
        self.assertEqual(receipt["observation_status"], "UNKNOWN")

    def test_materialized_plan_is_selected_without_manifest_or_receipt_fallback(self):
        from chat import daily_runtime

        materialized = object()
        plan = types.SimpleNamespace(
            manifest={"context_plan_consumer": "canonical"},
            continuity_plan=materialized,
            capacity_context_plan=object(),
            hot_desired_plan=object(),
        )
        self.assertIs(
            daily_runtime._materialized_context_plan_for_first_send(plan),
            materialized,
        )

    def test_conflicting_replay_downgrades_without_replacing_evidence(self):
        first = {"observation_status": "PARTIAL", "plan_identity": {"plan_id": "one"}}
        second = {"observation_status": "COMPLETE", "plan_identity": {"plan_id": "two"}}
        observed = obs.attach_observation(
            {"first_send_accounting": first},
            context_breakdown={},
            runtime={},
            first_send_accounting=second,
        )
        self.assertEqual(
            observed["first_send_accounting"]["plan_identity"]["plan_id"],
            "one",
        )
        self.assertEqual(observed["first_send_accounting"]["observation_status"], "UNKNOWN")
        self.assertEqual(
            observed["first_send_accounting"]["conflict_code"],
            "conflicting_first_send_evidence",
        )

    def test_observer_and_logger_failure_preserve_usage(self):
        from chat import daily_runtime

        plan = types.SimpleNamespace(
            manifest={},
            assembly={},
            _provider_prompt_system_snapshot="SYSTEM",
            _provider_prompt_content_snapshot="CONTENT",
        )
        resident = types.SimpleNamespace(
            _last_provider_content="CONTENT",
            _provider_prompt_snapshot_failed=False,
            tool_surface_snapshot={},
            allowed_tools="",
            mcp_config_path=None,
        )
        usage = {"resident_turn_count": 1}
        with mock.patch.object(
            obs,
            "build_context_breakdown",
            side_effect=RuntimeError("observer failure"),
        ), mock.patch.object(
            daily_runtime.logger,
            "exception",
            side_effect=RuntimeError("logger failure"),
        ):
            returned = daily_runtime._attach_daily_provider_budget_observation(
                plan,
                resident=resident,
                static_system="SYSTEM",
                usage=usage,
            )
        self.assertIs(returned, usage)
        self.assertEqual(returned, {"resident_turn_count": 1})


class RealityAdapterContractTests(unittest.TestCase):
    def test_reality_snapshot_failure_does_not_fallback_to_pre_reality_payload(self):
        with mock.patch("tools.ombre_adapter.warmup_async"):
            import gateway

        from tools import cc_usage_observability as obs

        class FakeResident:
            def __init__(self):
                self.received = None

            def send_turn(self, content, **kwargs):
                self.received = (content, kwargs)
                return iter([("done", ("ok", "", {}, {}))])

        base = [{"type": "text", "text": "USER"}]
        lease = {"turn_id": "lease-1"}
        fake = FakeResident()
        adapter = gateway._RequestRealityResident(fake, "REALITY")
        with mock.patch.object(
            obs,
            "snapshot_prompt_content",
            side_effect=RuntimeError("isolated Reality snapshot failure"),
        ):
            list(adapter.send_turn(base, turn_lease=lease))

        self.assertIsNone(adapter._last_provider_content)
        self.assertTrue(adapter._provider_prompt_snapshot_failed)
        self.assertIsNotNone(fake.received)
        self.assertIs(fake.received[1]["turn_lease"], lease)
        self.assertIn("REALITY", fake.received[0][-1]["text"])

    def test_reality_adapter_preserves_visible_payload_and_kwargs(self):
        with mock.patch("tools.ombre_adapter.warmup_async"):
            import gateway

        class FakeResident:
            def __init__(self):
                self.received = None

            def send_turn(self, content, **kwargs):
                self.received = (content, kwargs)
                return iter([("done", ("ok", "", {}, {}))])

        base = [{"type": "text", "text": "USER"}]
        lease = {"turn_id": "lease-1"}
        fake = FakeResident()
        adapter = gateway._RequestRealityResident(fake, "REALITY")
        stream = adapter.send_turn(base, turn_lease=lease)
        list(stream)

        self.assertEqual(fake.received[0], adapter._last_provider_content)
        self.assertIs(fake.received[1]["turn_lease"], lease)
        self.assertEqual(fake.received[0][0], {"type": "text", "text": "USER"})
        self.assertIn("REALITY", fake.received[0][-1]["text"])


if __name__ == "__main__":
    unittest.main()
