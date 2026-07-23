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

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cc_resident import ResidentSession
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


if __name__ == "__main__":
    unittest.main()
