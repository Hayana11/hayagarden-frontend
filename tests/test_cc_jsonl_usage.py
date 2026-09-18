"""Heartbeat Measurement Foundation: JSONL replay and fingerprints."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from typing import Any

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cc_resident import ProviderTerminalReceipt, ResidentSession
from tools import cc_jsonl_usage as replay
from tools import cc_usage_observability as obs
from wake.usage import build_wake_cache_info


def _assistant_line(
    request_id,
    *,
    cache_read=0,
    cache_creation=0,
    cache_creation_5m=0,
    cache_creation_1h=0,
    input_tokens=1,
    output_tokens=2,
    model="claude-sonnet-4-6",
):
    return json.dumps({
        "type": "assistant",
        "requestId": request_id,
        "timestamp": "2026-07-23T01:02:03Z",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cache_creation_5m,
                    "ephemeral_1h_input_tokens": cache_creation_1h,
                },
            },
        },
    })


class FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.stderr = io.StringIO()
        self.pid = 4242
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0

    def kill(self):
        self._code = -9

    def wait(self, timeout=None):
        return self._code or 0


class JsonlReplayTests(unittest.TestCase):
    def test_anonymized_historical_fixture(self):
        fixture = Path(ROOT) / "tests" / "fixtures" / "cc_usage_history.jsonl"
        result = replay.replay_jsonl_path(fixture)
        self.assertEqual(
            result["request_ids"],
            ["req_fixture_1h", "req_fixture_5m"],
        )
        self.assertEqual(result["assistant_usage_rows"], 4)
        self.assertEqual(result["duplicate_rows_ignored"], 2)
        self.assertEqual(result["totals"]["cache_creation_1h"], 27982)
        self.assertEqual(result["totals"]["cache_creation_5m"], 1082)

    def test_request_id_dedup_and_ttl_buckets(self):
        one_hour = _assistant_line(
            "req-1", cache_creation=100, cache_creation_1h=100
        )
        five_minute = _assistant_line(
            "req-2",
            cache_read=50,
            cache_creation=20,
            cache_creation_5m=20,
        )
        result = replay.replay_jsonl_lines([
            one_hour,
            one_hour,
            "not-json",
            five_minute,
            five_minute,
        ])
        self.assertEqual(result["request_ids"], ["req-1", "req-2"])
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["duplicate_rows_ignored"], 2)
        self.assertEqual(result["invalid_json_rows"], 1)
        self.assertEqual(result["totals"]["cache_creation"], 120)
        self.assertEqual(result["totals"]["cache_creation_1h"], 100)
        self.assertEqual(result["totals"]["cache_creation_5m"], 20)

    def test_conflicting_duplicate_stays_one_request(self):
        a = _assistant_line("req-1", cache_creation=10, cache_creation_1h=10)
        b = _assistant_line("req-1", cache_creation=12, cache_creation_1h=12)
        result = replay.replay_jsonl_lines([a, b])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["conflicting_duplicate_rows"], 1)
        self.assertEqual(result["totals"]["cache_creation"], 12)
        self.assertEqual(result["totals"]["cache_creation_1h"], 12)
        self.assertEqual(result["totals"]["cache_creation_5m"], 0)

    def test_conflicting_duplicate_never_merges_ttl_buckets(self):
        a = _assistant_line(
            "req-1", cache_creation=100, cache_creation_5m=100, cache_creation_1h=0
        )
        b = _assistant_line(
            "req-1", cache_creation=100, cache_creation_5m=0, cache_creation_1h=100
        )
        result = replay.replay_jsonl_lines([a, b])
        record = result["records"][0]
        self.assertEqual(record["cache_creation"], 100)
        self.assertTrue(
            record["cache_creation_5m"] + record["cache_creation_1h"] == 100
        )
        self.assertEqual(result["totals"]["cache_creation_5m"], record["cache_creation_5m"])
        self.assertEqual(result["totals"]["cache_creation_1h"], record["cache_creation_1h"])

    def test_file_cursor_only_replays_new_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(_assistant_line("req-old") + "\n", encoding="utf-8")
            cursor = path.stat().st_size
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_assistant_line(
                    "req-new", cache_creation=5, cache_creation_5m=5
                ) + "\n")
            result = replay.replay_jsonl_path(path, offset=cursor)
        self.assertEqual(result["request_ids"], ["req-new"])
        self.assertEqual(result["totals"]["cache_creation_5m"], 5)

    def test_history_replay_dedups_across_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jsonl"
            b = Path(tmp) / "b.jsonl"
            a.write_text(_assistant_line("req-shared") + "\n", encoding="utf-8")
            b.write_text(
                _assistant_line("req-shared") + "\n"
                + _assistant_line("req-new") + "\n",
                encoding="utf-8",
            )
            result = replay.replay_jsonl_paths([a, b])
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["request_ids"], ["req-shared", "req-new"])
        self.assertEqual(result["duplicate_rows_ignored"], 1)

    def test_attach_preserves_stream_totals_and_maps_rounds(self):
        usage = {
            "v": 2,
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read": 0,
            "cache_creation": 100,
            "rounds": [{
                "index": 1,
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read": 0,
                "cache_creation": 100,
            }],
        }
        result = replay.replay_jsonl_lines([
            _assistant_line(
                "req-1", cache_creation=100, cache_creation_1h=100
            ),
        ])
        enriched = replay.attach_jsonl_usage(usage, result)
        self.assertEqual(enriched["cache_creation"], 100)
        self.assertEqual(enriched["cache_creation_1h"], 100)
        self.assertEqual(enriched["cache_creation_5m"], 0)
        self.assertEqual(enriched["request_ids"], ["req-1"])
        self.assertEqual(enriched["rounds"][0]["request_id"], "req-1")
        self.assertTrue(enriched["jsonl_usage"]["stream_totals_match"])
        self.assertEqual(enriched["jsonl_usage"]["stream_totals"], {
            "input_tokens": 1, "output_tokens": 2,
            "cache_read": 0, "cache_creation": 100,
        })
        self.assertEqual(enriched["jsonl_usage"]["jsonl_totals"], {
            "input_tokens": 1, "output_tokens": 2,
            "cache_read": 0, "cache_creation": 100,
        })
        self.assertEqual(enriched["_obs_model"], "claude-sonnet-4-6")


class FingerprintAndLeaseTests(unittest.TestCase):
    def _runtime(self, **overrides):
        data = dict(
            resident_generation=3,
            resident_pid=9,
            resident_turn_count=2,
            respawn_reason=None,
            idle_seconds_before_turn=3700,
            is_cold=False,
            static_system="stable system",
            mcp_config_text='{"mcpServers":{"home":{}}}',
            allowed_tools="mcp__home__read,mcp__brain__search",
            tool_schema_sha256="d" * 64,
            tool_schema_source="mcp_list_tools",
            tool_schema_measurement_status="available",
            model="claude-sonnet-4-6",
            thinking_config={"thinking_display": "summarized", "effort": None},
            instance_id="gw-1",
            observed_at="2026-07-23T04:01:00+08:00",
        )
        data.update(overrides)
        return obs.build_runtime(**data)

    def _miss(self, runtime):
        return {
            "resident_turn_count": 2,
            "respawn_reason": None,
            "runtime": runtime,
            "rounds": [{
                "cache_read": 0,
                "cache_creation": 50,
                "context_tokens": 51,
            }],
        }

    def test_all_generation_fingerprints_are_stable_and_sensitive(self):
        a = self._runtime()
        b = self._runtime()
        for key in (
            "gateway_instance_id",
            "resident_generation",
            "static_system_sha256",
            "tools_sha256",
            "mcp_config_sha256",
            "provider_sha256",
            "model_sha256",
            "thinking_sha256",
        ):
            self.assertEqual(a[key], b[key], key)
            self.assertIsNotNone(a[key], key)
        self.assertNotEqual(
            a["static_system_sha256"],
            self._runtime(static_system="changed system")["static_system_sha256"],
        )
        self.assertNotEqual(
            a["tools_sha256"],
            self._runtime(allowed_tools="mcp__home__read")["tools_sha256"],
        )
        self.assertNotEqual(
            a["provider_sha256"],
            self._runtime(provider="api_relay")["provider_sha256"],
        )
        self.assertNotEqual(
            a["model_sha256"],
            self._runtime(model="claude-opus-4-6")["model_sha256"],
        )
        self.assertNotEqual(
            a["thinking_sha256"],
            self._runtime(
                thinking_config={"thinking_display": "full", "effort": None}
            )["thinking_sha256"],
        )

    def test_cold_return_after_lease_is_separate_reason(self):
        current = self._runtime(
            keepwarm_lease_expires_at="2026-07-23T04:00:00+08:00"
        )
        previous = self._runtime(
            observed_at="2026-07-23T03:50:00+08:00",
            keepwarm_lease_expires_at="2026-07-23T04:00:00+08:00",
        )
        usage = self._miss(current)
        self.assertEqual(
            obs.classify_cache_miss_reason(usage, prev_runtime=previous),
            "cold_return_after_lease",
        )
        self.assertIs(
            obs.classify_suspected_cache_expiry(
                usage, prev_runtime=previous
            ),
            True,
        )

    def test_changed_system_wins_over_lease_classification(self):
        current = self._runtime(
            static_system="new",
            keepwarm_lease_expires_at="2026-07-23T04:00:00+08:00",
        )
        previous = self._runtime(
            static_system="old",
            keepwarm_lease_expires_at="2026-07-23T04:00:00+08:00",
        )
        self.assertEqual(
            obs.classify_cache_miss_reason(
                self._miss(current), prev_runtime=previous
            ),
            "system_changed",
        )

    def test_report_counts_cold_return_after_lease(self):
        previous = self._runtime(
            idle_seconds_before_turn=10,
            observed_at="2026-07-23T03:50:00+08:00",
            keepwarm_lease_expires_at="2026-07-23T04:00:00+08:00",
        )
        current = self._runtime(
            observed_at="2026-07-23T04:01:00+08:00",
            keepwarm_lease_expires_at="2026-07-23T04:00:00+08:00",
        )
        hot = {
            "v": 2,
            "provider": "claude_code",
            "resident_turn_count": 2,
            "runtime": previous,
            "num_rounds": 1,
            "rounds": [{"cache_read": 10, "cache_creation": 0}],
        }
        miss = {
            "v": 2,
            "provider": "claude_code",
            "resident_turn_count": 3,
            "runtime": current,
            "num_rounds": 1,
            "rounds": [{"cache_read": 0, "cache_creation": 50}],
        }
        rows = [
            {
                "id": 1,
                "created_at": "2026-07-23 03:50:00",
                "cache_info": json.dumps(hot),
            },
            {
                "id": 2,
                "created_at": "2026-07-23 04:01:00",
                "cache_info": json.dumps(miss),
            },
        ]
        report = obs.aggregate_cc_observability(
            rows,
            days=1,
            now=datetime(2026, 7, 23, 12, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual(report["summary"]["cold_return_after_lease_count"], 1)
        self.assertEqual(
            report["summary"]["cache_miss_reasons"],
            {"cold_return_after_lease": 1},
        )


class ResidentJsonlHookTests(unittest.TestCase):
    def test_resident_attaches_replayed_request_without_model_call(self):
        lines = [
            json.dumps({
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
            }),
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 100,
                        },
                    },
                },
            }),
            json.dumps({"type": "result", "is_error": False}),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)
        replay_result = replay.replay_jsonl_lines([
            _assistant_line(
                "req-1", cache_creation=100, cache_creation_1h=100
            ),
        ])
        with (
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(
                replay, "replay_session_jsonl", return_value=replay_result
            ),
        ):
            events = list(resident.send_turn("hello"))
        done = [payload for event, payload in events if event == "done"]
        self.assertEqual(len(done), 1)
        usage = done[0][2]
        self.assertEqual(usage["request_ids"], ["req-1"])
        self.assertEqual(usage["cache_creation_1h"], 100)
        self.assertEqual(usage["cache_creation_5m"], 0)
        self.assertNotIn("finality_state", usage["jsonl_usage"])

    def test_resident_done_text_prefers_terminal_assistant_content(self):
        terminal_text = "完整的 terminal 正文"
        lines = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "短前缀"},
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": terminal_text}],
                },
            }),
            json.dumps({
                "type": "result",
                "is_error": False,
                "result": terminal_text,
            }),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)

        with (
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(
                replay,
                "replay_session_jsonl",
                return_value=replay.replay_jsonl_lines([]),
            ),
        ):
            events = list(resident.send_turn("hello"))

        self.assertEqual(
            [payload for event, payload in events if event == "text"],
            ["短前缀"],
        )
        done = [payload for event, payload in events if event == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][0], terminal_text)
        receipt = done[0][2].terminal_receipt
        self.assertIsInstance(receipt, ProviderTerminalReceipt)
        self.assertEqual(receipt.terminal_kind, 'provider_result')
        self.assertEqual(receipt.source, 'resident_live_stdout')

    def test_resident_done_text_does_not_duplicate_matching_terminal_content(self):
        text = "stream 与 terminal 相同"
        lines = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text}],
                },
            }),
            json.dumps({
                "type": "result",
                "is_error": False,
                "result": text,
            }),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)

        with (
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(
                replay,
                "replay_session_jsonl",
                return_value=replay.replay_jsonl_lines([]),
            ),
        ):
            events = list(resident.send_turn("hello"))

        self.assertEqual(
            [payload for event, payload in events if event == "text"],
            [text],
        )
        done = [payload for event, payload in events if event == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][0], text)


    def test_resident_captures_session_id_from_camelcase_jsonl_events(self):
        session = "65305691-efff-4fd6-9df5-2fc4ea4aa43f"
        lines = [
            json.dumps({
                "type": "assistant",
                "sessionId": session,
                "message": {
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 100,
                    },
                },
            }),
            json.dumps({"type": "result", "is_error": False}),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)
        replay_result = replay.replay_jsonl_lines([
            _assistant_line(
                "req-1", cache_creation=100, cache_creation_1h=100
            ),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / (session + ".jsonl")
            jsonl_path.write_text(
                _assistant_line("req-1", cache_creation=100, cache_creation_1h=100) + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(replay, "claude_project_slug", return_value=Path(tmp).name),
                mock.patch.object(replay, "replay_session_jsonl", return_value=replay_result),
                mock.patch.object(
                    replay,
                    "snapshot_session_jsonl",
                    return_value={"path": str(jsonl_path), "offset": 0},
                ),
            ):
                events = list(resident.send_turn("hello"))
        self.assertEqual(resident.session_id, session)
        done = [payload for event, payload in events if event == "done"]
        usage = done[0][2]
        self.assertEqual(usage["request_ids"], ["req-1"])

    def test_resident_retries_jsonl_replay_when_file_flushes_late(self):
        session = "199fb8b4-b310-440c-a9c2-7a3292a9d451"
        lines = [
            json.dumps({"type": "assistant", "sessionId": session, "message": {"usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 100,
            }}}),
            json.dumps({"type": "result", "is_error": False}),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)
        replay_result = replay.replay_jsonl_lines([
            _assistant_line("req-1", cache_creation=100, cache_creation_1h=100),
        ])
        with (
            mock.patch.object(
                replay,
                "replay_session_jsonl",
                side_effect=[{"request_count": 0, "request_ids": []}, replay_result],
            ),
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch("cc_resident.time.sleep"),
        ):
            events = list(resident.send_turn("hello"))
        done = [payload for event, payload in events if event == "done"]
        usage = done[0][2]
        self.assertEqual(usage["request_ids"], ["req-1"])
        self.assertEqual(usage["cache_creation_1h"], 100)
        self.assertTrue(usage["jsonl_usage"]["stream_totals_match"])

    def test_resident_idle_heartbeat_when_stdout_idle(self):
        """MF-009 CASE 2: idle stdout yields heartbeat, then normal events follow."""
        lines = [
            json.dumps({
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            }),
            json.dumps({"type": "result", "is_error": False}),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)
        replay_result = replay.replay_jsonl_lines([])
        select_calls = 0

        def fake_select(rlist, wlist, xlist, timeout):
            nonlocal select_calls
            select_calls += 1
            if select_calls == 1:
                return [], [], []
            return [rlist[0]], [], []

        with (
            mock.patch("select.select", fake_select),
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(
                replay, "replay_session_jsonl", return_value=replay_result
            ),
        ):
            events = list(resident.send_turn("hello", idle_heartbeat_sec=0.1))

        heartbeats = [payload for event, payload in events if event == "heartbeat"]
        self.assertEqual(len(heartbeats), 1)
        self.assertGreater(select_calls, 1)
        texts = [payload for event, payload in events if event == "text"]
        self.assertEqual(texts, ["hi"])
        done = [payload for event, payload in events if event == "done"]
        self.assertEqual(len(done), 1)

    def test_resident_no_idle_heartbeat_by_default(self):
        lines = [
            json.dumps({"type": "result", "is_error": False}),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)
        with (
            mock.patch("select.select") as select_mock,
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(
                replay,
                "replay_session_jsonl",
                return_value=replay.replay_jsonl_lines([]),
            ),
        ):
            list(resident.send_turn("hello"))
        select_mock.assert_not_called()

    def test_unified_normal_wake_waits_for_multitool_late_flush(self):
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._session_id = "wake-late-flush"
        usage = {
            "input_tokens": 3,
            "output_tokens": 6,
            "cache_read": 0,
            "cache_creation": 30,
            "rounds": [
                {"index": 1, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
                {"index": 2, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
                {"index": 3, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
            ],
        }
        partial = replay.replay_jsonl_lines([
            _assistant_line("req-1", cache_creation=10, cache_creation_1h=10),
        ])
        complete = replay.replay_jsonl_lines([
            _assistant_line("req-1", cache_creation=10, cache_creation_1h=10),
            _assistant_line("req-2", cache_creation=10, cache_creation_1h=10),
            _assistant_line("req-3", cache_creation=10, cache_creation_1h=10),
        ])
        cursors = []
        with (
            mock.patch.object(
                replay,
                "snapshot_session_jsonl",
                return_value={"path": "/tmp/wake-late-flush.jsonl", "offset": 42},
            ),
            mock.patch.object(
                replay,
                "replay_session_jsonl",
                side_effect=lambda *args, **kwargs: (
                    cursors.append(kwargs["cursor"])
                    or (partial if len(cursors) < 3 else complete)
                ),
            ),
            mock.patch("cc_resident.time.sleep") as sleep_mock,
        ):
            merged = resident._attach_jsonl_usage_with_retry(
                usage,
                finality_profile="unified_normal_wake",
            )

        self.assertTrue(merged["jsonl_usage"]["stream_totals_match"])
        self.assertEqual(merged["jsonl_usage"]["finality_state"], "FINAL")
        self.assertEqual(len(cursors), 3)
        self.assertEqual(cursors[0], cursors[1])
        self.assertEqual(cursors[1], cursors[2])
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [0.05, 0.15],
        )

    def test_unified_normal_wake_never_catches_up_and_stays_pending(self):
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._session_id = "wake-never-final"
        usage = {
            "input_tokens": 3,
            "output_tokens": 6,
            "cache_read": 0,
            "cache_creation": 30,
            "rounds": [
                {"index": 1, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
                {"index": 2, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
                {"index": 3, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
            ],
        }
        partial = replay.replay_jsonl_lines([
            _assistant_line("req-1", cache_creation=10, cache_creation_1h=10),
        ])
        with (
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(replay, "replay_session_jsonl", return_value=partial) as replay_mock,
            mock.patch("cc_resident.time.sleep") as sleep_mock,
        ):
            merged = resident._attach_jsonl_usage_with_retry(
                usage,
                finality_profile="unified_normal_wake",
            )

        self.assertFalse(merged["jsonl_usage"]["stream_totals_match"])
        self.assertEqual(merged["jsonl_usage"]["finality_state"], "FINALITY_PENDING")
        self.assertEqual(replay_mock.call_count, 5)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [0.05, 0.15, 0.35, 0.45],
        )

    def test_unified_normal_wake_true_totals_mismatch_never_releases(self):
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._session_id = "wake-conflict"
        usage = {
            "input_tokens": 3,
            "output_tokens": 6,
            "cache_read": 0,
            "cache_creation": 30,
            "rounds": [
                {"index": 1, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
                {"index": 2, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
                {"index": 3, "input_tokens": 1, "output_tokens": 2,
                 "cache_read": 0, "cache_creation": 10},
            ],
        }
        conflict = replay.replay_jsonl_lines([
            _assistant_line("req-1", input_tokens=2, output_tokens=2,
                            cache_creation=10, cache_creation_1h=10),
            _assistant_line("req-2", input_tokens=1, output_tokens=2,
                            cache_creation=10, cache_creation_1h=10),
            _assistant_line("req-3", input_tokens=1, output_tokens=2,
                            cache_creation=10, cache_creation_1h=10),
        ])
        with (
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(replay, "replay_session_jsonl", return_value=conflict),
            mock.patch("cc_resident.time.sleep"),
        ):
            merged = resident._attach_jsonl_usage_with_retry(
                usage,
                finality_profile="unified_normal_wake",
            )

        self.assertFalse(merged["jsonl_usage"]["stream_totals_match"])
        self.assertEqual(merged["jsonl_usage"]["finality_state"], "FINALITY_PENDING")

    def test_normal_chat_keeps_original_bounded_jsonl_window(self):
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._session_id = "normal-chat"
        usage = {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read": 0,
            "cache_creation": 10,
            "rounds": [{"index": 1, "input_tokens": 1, "output_tokens": 2,
                        "cache_read": 0, "cache_creation": 10}],
        }
        partial = replay.replay_jsonl_lines([
            _assistant_line("req-1", cache_creation=5, cache_creation_1h=5),
        ])
        with (
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None),
            mock.patch.object(replay, "replay_session_jsonl", return_value=partial) as replay_mock,
            mock.patch("cc_resident.time.sleep") as sleep_mock,
        ):
            merged = resident._attach_jsonl_usage_with_retry(usage)

        self.assertFalse(merged["jsonl_usage"]["stream_totals_match"])
        self.assertNotIn("finality_state", merged["jsonl_usage"])
        self.assertEqual(replay_mock.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [0.05, 0.15, 0.35],
        )

    def test_provider_without_authoritative_result_keeps_terminal_failure(self):
        session = "wake-no-result"
        lines = [
            json.dumps({
                "type": "assistant",
                "sessionId": session,
                "message": {
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 10,
                    },
                },
            }),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)
        with (
            mock.patch.object(replay, "snapshot_session_jsonl", return_value=None) as snapshot_mock,
            mock.patch.object(replay, "replay_session_jsonl", return_value=None) as replay_mock,
            mock.patch("cc_resident.time.sleep") as sleep_mock,
        ):
            with self.assertRaises(Exception) as raised:
                list(resident.send_turn(
                    "hello",
                    jsonl_finality_profile="unified_normal_wake",
                ))

        self.assertEqual(getattr(raised.exception, "error_code", None),
                         "result_missing_before_terminal")
        replay_mock.assert_not_called()

    def test_provider_error_skips_wake_extended_replay(self):
        session = "wake-provider-error"
        lines = [
            json.dumps({
                "type": "result",
                "session_id": session,
                "is_error": True,
                "result": "provider failed",
            }),
        ]
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._proc = FakeProc(lines)
        with (
            mock.patch.object(
                replay, "snapshot_session_jsonl", return_value=None,
            ) as snapshot_mock,
            mock.patch.object(
                replay, "replay_session_jsonl", return_value=None,
            ) as replay_mock,
            mock.patch("cc_resident.time.sleep"),
        ):
            with self.assertRaises(Exception):
                list(resident.send_turn(
                    "hello",
                    jsonl_finality_profile="unified_normal_wake",
                ))

        replay_mock.assert_not_called()


    def test_resident_retries_until_all_jsonl_requests_arrive(self):
        resident = ResidentSession("/tmp/cc-test", "", "/tmp/mcp.json")
        resident._session_id = "65305691-efff-4fd6-9df5-2fc4ea4aa43f"
        usage = {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_read": 100,
            "cache_creation": 110,
            "rounds": [
                {
                    "index": 1,
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read": 0,
                    "cache_creation": 100,
                    "context_tokens": 101,
                },
                {
                    "index": 2,
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read": 100,
                    "cache_creation": 10,
                    "context_tokens": 111,
                },
            ],
        }
        partial_replay = replay.replay_jsonl_lines([
            _assistant_line(
                "req-1",
                input_tokens=1,
                output_tokens=2,
                cache_creation=100,
                cache_creation_1h=100,
            ),
        ])
        complete_replay = replay.replay_jsonl_lines([
            _assistant_line(
                "req-1",
                input_tokens=1,
                output_tokens=2,
                cache_creation=100,
                cache_creation_1h=100,
            ),
            _assistant_line(
                "req-2",
                input_tokens=1,
                output_tokens=2,
                cache_read=100,
                cache_creation=10,
                cache_creation_1h=10,
            ),
        ])
        replay_calls = []

        def _track_replay(*args, **kwargs):
            replay_calls.append(kwargs.get("cursor"))
            if len(replay_calls) == 1:
                return partial_replay
            return complete_replay

        with (
            mock.patch.object(replay, "replay_session_jsonl", side_effect=_track_replay),
            mock.patch("cc_resident.time.sleep") as sleep_mock,
        ):
            merged = resident._attach_jsonl_usage_with_retry(usage)
        self.assertEqual(len(replay_calls), 2)
        self.assertEqual(sleep_mock.call_count, 1)
        self.assertEqual(merged["request_ids"], ["req-1", "req-2"])
        self.assertEqual(merged["request_count"], 2)
        self.assertTrue(merged["jsonl_usage"]["stream_totals_match"])
        partial_merged = replay.attach_jsonl_usage(usage, partial_replay)
        self.assertFalse(partial_merged["jsonl_usage"]["stream_totals_match"])


class WakeUsageIdentityTests(unittest.TestCase):
    def test_wake_uses_same_deduped_request_ids_and_ttl_buckets(self):
        rounds = [
            {
                "index": 1,
                "request_id": "req-1",
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read": 0,
                "cache_creation": 100,
                "cache_creation_5m": 0,
                "cache_creation_1h": 100,
                "context_tokens": 101,
            },
            {
                "index": 2,
                "request_id": "req-1",
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read": 100,
                "cache_creation": 5,
                "cache_creation_5m": 5,
                "cache_creation_1h": 0,
                "context_tokens": 106,
            },
            {
                "index": 3,
                "request_id": "req-2",
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read": 100,
                "cache_creation": 5,
                "cache_creation_5m": 5,
                "cache_creation_1h": 0,
                "context_tokens": 106,
            },
        ]

        def payload_builder(**kwargs):
            return dict(kwargs)

        info = build_wake_cache_info(
            rounds,
            elapsed_sec=1,
            cache_supported=True,
            mode="normal",
            model="claude-sonnet-4-6",
            payload_builder=payload_builder,
            provider="claude_code",
        )
        self.assertEqual(info["request_ids"], ["req-1", "req-2"])
        self.assertEqual(info["request_count"], 2)
        self.assertEqual(info["num_rounds"], 2)
        self.assertEqual(info["cache_creation"], 105)
        self.assertEqual(info["cache_creation_1h"], 100)
        self.assertEqual(info["cache_creation_5m"], 5)


class ToolSurfaceFingerprintTests(unittest.TestCase):
    def _live_lists(self, *entries: tuple[str, list[dict[str, Any]]]):
        def provider(_path):
            return list(entries)
        return provider

    def test_live_tools_list_order_affects_hash(self):
        from tools import cc_tool_surface as surface

        schema = {"type": "object", "properties": {}}
        allow = "mcp__home__tool_a,mcp__home__tool_b"
        snap_ab = surface.capture_tool_surface_snapshot(
            allow,
            live_tool_lists_provider=self._live_lists(
                ("home", [
                    {"name": "tool_a", "input_schema": schema},
                    {"name": "tool_b", "input_schema": schema},
                ]),
            ),
        )
        snap_ba = surface.capture_tool_surface_snapshot(
            allow,
            live_tool_lists_provider=self._live_lists(
                ("home", [
                    {"name": "tool_b", "input_schema": schema},
                    {"name": "tool_a", "input_schema": schema},
                ]),
            ),
        )
        self.assertNotEqual(
            snap_ab["tool_schema_sha256"],
            snap_ba["tool_schema_sha256"],
        )

    def test_fresh_capture_reflects_schema_change_across_generations(self):
        from tools import cc_tool_surface as surface

        schema_v1 = {"type": "object", "properties": {"x": {"type": "string"}}}
        schema_v2 = {"type": "object", "properties": {"y": {"type": "integer"}}}
        schemas = [schema_v1, schema_v2]

        def provider(_path):
            schema = schemas.pop(0)
            return [("home", [{"name": "light_on", "input_schema": schema}])]

        allow = "mcp__home__light_on"
        snap1 = surface.capture_tool_surface_snapshot(
            allow, live_tool_lists_provider=provider,
        )
        snap2 = surface.capture_tool_surface_snapshot(
            allow, live_tool_lists_provider=provider,
        )
        self.assertNotEqual(snap1["tool_schema_sha256"], snap2["tool_schema_sha256"])

    def test_spawn_refreshes_tool_surface_snapshot(self):
        from tools import cc_tool_surface as surface

        with tempfile.TemporaryDirectory() as tmp:
            resident = ResidentSession(tmp, "mcp__home__light_on", "/tmp/mcp.json")
            with (
                mock.patch("cc_resident.subprocess.Popen", return_value=FakeProc([])),
                mock.patch(
                    "chat.cc_runtime.require_pinned_claude_version",
                    return_value="2.1.220",
                ),
                mock.patch(
                    "chat.cc_runtime.claude_cmd",
                    side_effect=lambda *a, **k: ["claude", *a],
                ),
                mock.patch.object(
                    surface,
                    "capture_tool_surface_snapshot",
                    side_effect=[
                        {
                            "tool_schema_sha256": "a" * 64,
                            "tool_schema_text": "[]",
                            "tool_schema_source": "mcp_list_tools",
                            "tool_schema_measurement_status": "available",
                            "tool_count": 1,
                        },
                        {
                            "tool_schema_sha256": "b" * 64,
                            "tool_schema_text": "[]",
                            "tool_schema_source": "mcp_list_tools",
                            "tool_schema_measurement_status": "available",
                            "tool_count": 1,
                        },
                    ],
                ),
            ):
                resident._spawn("system", {}, reason="process_dead")
                first = resident.tool_surface_snapshot["tool_schema_sha256"]
                resident._spawn("system", {}, reason="idle")
                second = resident.tool_surface_snapshot["tool_schema_sha256"]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
