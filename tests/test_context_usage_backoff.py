import datetime as dt
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

from tools import context_usage_collector as collector


class Clock(dt.datetime):
    current = dt.datetime(2026, 8, 30, 12, tzinfo=dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz else cls.current.replace(tzinfo=None)


class ClaudeOfficialBackoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cache = self.root / "official.json"
        Clock.current = dt.datetime(2026, 8, 30, 12, tzinfo=dt.timezone.utc)
        self.old_quota = {
            "five_hour": {"used_percentage": 40, "remaining_percentage": 60},
            "seven_day": {"used_percentage": 55, "remaining_percentage": 45},
            "updated_at": "2026-08-30T10:00:00Z",
        }
        self.codex_entry = {"fetched_at": "2026-08-30T11:00:00Z", "quota": self.old_quota}
        self.block = {"endTime": "2026-08-30T15:00:00Z", "projection": {"remainingMinutes": 180}}
        for patch in (
            mock.patch.object(collector, "OFFICIAL_CACHE_PATH", self.cache),
            mock.patch.object(collector, "OFFICIAL_MIN_INTERVAL_SEC", 720),
            mock.patch.object(collector.dt, "datetime", Clock),
            mock.patch.object(collector, "read_claude_oauth_token", return_value="fake-secret-token"),
            mock.patch.object(collector, "scan_claude_projects", return_value=([], None)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        fallback_patch = mock.patch.object(collector, "read_ccusage_block", return_value=self.block)
        self.fallback = fallback_patch.start()
        self.addCleanup(fallback_patch.stop)

    def collect(self):
        return collector.collect_claude(self.root, "UTC", use_official=True)

    def read_cache(self):
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def seed(self, with_quota=False, **metadata):
        entry = {"fetched_at": "2026-08-30T10:00:00Z", "quota": self.old_quota} if with_quota else {}
        entry.update(metadata)
        self.cache.write_text(json.dumps({"claude": entry, "codex": self.codex_entry}), encoding="utf-8")

    def http_error(self, status=429, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        error = urllib.error.HTTPError(collector.CLAUDE_USAGE_URL, status, "private-error-message", headers, io.BytesIO(b"private-response-body"))
        self.addCleanup(error.close)
        return error

    def assert_fallback(self, agent):
        self.assertEqual(agent["quota_source"], "ccusage_blocks")
        window = agent["quota"]["five_hour"]
        self.assertEqual(window["remaining_minutes"], 180)
        self.assertEqual(window["remaining_basis"], "time_until_reset")
        self.assertNotIn("used_percentage", window)
        self.assertNotIn("remaining_percentage", window)
        self.assertEqual(agent["quota"]["seven_day"], {})

    def test_429_without_cache_persists_metadata_and_falls_back(self):
        error = self.http_error()
        with mock.patch.object(collector._NO_REDIRECT_OPENER, "open", side_effect=error) as http:
            self.assert_fallback(self.collect())
        http.assert_called_once()
        self.assertEqual(self.read_cache()["claude"], {
            "last_attempt_at": "2026-08-30T12:00:00Z",
            "last_status": 429,
            "next_retry_at": "2026-08-30T12:12:00Z",
        })
        self.assertEqual(error.fp.tell(), 0)  # Do not even read the error body.
        raw = self.cache.read_text(encoding="utf-8")
        for secret in ("fake-secret-token", "Authorization", "private-error-message", "private-response-body"):
            self.assertNotIn(secret, raw)

    def test_no_quota_cooldown_skips_fetch_and_preserves_metadata(self):
        self.seed(last_status=429, next_retry_at="2026-08-30T12:12:00Z")
        before = self.cache.read_bytes()
        Clock.current += dt.timedelta(minutes=5)
        with mock.patch.object(collector, "fetch_claude_official_usage") as fetch:
            self.assert_fallback(self.collect())
        fetch.assert_not_called()
        self.assertEqual(self.cache.read_bytes(), before)

    def test_no_quota_cooldown_allows_local_unavailable_fallback(self):
        self.seed(next_retry_at="2026-08-30T12:12:00Z")
        self.fallback.return_value = None
        with mock.patch.object(collector, "fetch_claude_official_usage") as fetch:
            agent = self.collect()
        fetch.assert_not_called()
        self.assertEqual(agent["quota_source"], "unavailable")
        self.assertEqual(agent["quota"]["five_hour"], {})

    def test_429_preserves_last_good_and_codex_entry(self):
        self.seed(with_quota=True)
        with mock.patch.object(collector._NO_REDIRECT_OPENER, "open", side_effect=self.http_error()):
            agent = self.collect()
        self.assertEqual(agent["quota_source"], "claude_oauth_usage")
        self.assertEqual(agent["quota"], self.old_quota)
        entry = self.read_cache()["claude"]
        self.assertEqual(entry["quota"], self.old_quota)
        self.assertEqual(entry["fetched_at"], "2026-08-30T10:00:00Z")
        self.assertEqual(entry["last_status"], 429)
        self.assertEqual(entry["next_retry_at"], "2026-08-30T12:12:00Z")
        self.assertEqual(self.read_cache()["codex"], self.codex_entry)
        self.fallback.assert_not_called()

    def test_cooldown_with_last_good_skips_fetch(self):
        self.seed(with_quota=True, next_retry_at="2026-08-30T12:12:00Z")
        with mock.patch.object(collector, "fetch_claude_official_usage") as fetch:
            agent = self.collect()
        fetch.assert_not_called()
        self.fallback.assert_not_called()
        self.assertEqual(agent["quota_source"], "claude_oauth_usage")
        self.assertEqual(agent["quota"], self.old_quota)

    def test_exact_expiry_retries_and_success_clears_backoff(self):
        for with_quota in (False, True):
            with self.subTest(with_quota=with_quota):
                self.seed(with_quota=with_quota, last_status=429, last_attempt_at="2026-08-30T11:48:00Z", next_retry_at="2026-08-30T12:00:00Z")
                payload = {"five_hour": {"utilization": 12}, "seven_day": {"utilization": 34}}
                with mock.patch.object(collector, "_http_get_json_result", return_value=(payload, 200)) as http:
                    agent = self.collect()
                http.assert_called_once()
                self.assertEqual(agent["quota_source"], "claude_oauth_usage")
                self.assertEqual(agent["quota"]["five_hour"]["used_percentage"], 12)
                self.assertEqual(agent["quota"]["seven_day"]["used_percentage"], 34)
                self.assertEqual(self.read_cache()["claude"], {"fetched_at": "2026-08-30T12:00:00Z", "quota": agent["quota"]})
                self.assertEqual(self.read_cache()["codex"], self.codex_entry)
                with mock.patch.object(collector, "fetch_claude_official_usage") as fetch:
                    self.assertEqual(self.collect()["quota"], agent["quota"])
                fetch.assert_not_called()

    def test_old_cache_without_metadata_still_throttles(self):
        self.seed(with_quota=True, fetched_at="2026-08-30T11:59:00Z")
        with mock.patch.object(collector, "fetch_claude_official_usage") as fetch:
            agent = self.collect()
        fetch.assert_not_called()
        self.assertEqual(agent["quota"], self.old_quota)
        self.assertEqual(agent["quota_source"], "claude_oauth_usage")

    def test_retry_after_parsing_floor_and_cap(self):
        for value, seconds in ((None, 720), ("60", 720), ("1800", 1800),
                               ("Sun, 30 Aug 2026 12:30:00 GMT", 1800),
                               ("Sun, 30 Aug 2026 11:30:00 GMT", 720),
                               ("999999999999", 86400), ("-1", 720),
                               ("nan", 720), ("inf", 720), ("1e30", 720),
                               ("1.5", 720), ("x" * 129, 720), ("junk", 720)):
            with self.subTest(value=value):
                self.seed()
                with mock.patch.object(collector._NO_REDIRECT_OPENER, "open", side_effect=self.http_error(retry_after=value)):
                    self.assert_fallback(self.collect())
                retry_at = dt.datetime.fromisoformat(self.read_cache()["claude"]["next_retry_at"].replace("Z", "+00:00"))
                self.assertEqual((retry_at - Clock.current).total_seconds(), seconds)

    def test_transient_failures_get_short_cooldown(self):
        for status in (None, 408, 500, 502, 503, 529):
            with self.subTest(status=status):
                self.seed()
                error = urllib.error.URLError("private-network-detail") if status is None else self.http_error(status)
                with mock.patch.object(collector._NO_REDIRECT_OPENER, "open", side_effect=error):
                    self.assert_fallback(self.collect())
                entry = self.read_cache()["claude"]
                self.assertEqual(entry["last_status"], status)
                self.assertEqual(entry["next_retry_at"], "2026-08-30T12:12:00Z")
                with mock.patch.object(collector, "fetch_claude_official_usage") as fetch:
                    self.assert_fallback(self.collect())
                fetch.assert_not_called()

    def test_permanent_errors_are_visible_without_sensitive_details(self):
        for status in (400, 401, 403, 404, 302):
            with self.subTest(status=status):
                self.seed()
                with mock.patch.object(collector._NO_REDIRECT_OPENER, "open", side_effect=self.http_error(status)), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    self.assert_fallback(self.collect())
                self.assertIn(f"HTTP {status}", stderr.getvalue())
                self.assertNotIn("private", stderr.getvalue())
                self.assertNotIn("fake-secret-token", stderr.getvalue())
                self.assertEqual(self.read_cache()["claude"]["last_status"], status)

    def test_invalid_payload_gets_visible_short_cooldown(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=({"error": "private-body"}, 200)), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assert_fallback(self.collect())
        self.assertIn("HTTP 200", stderr.getvalue())
        self.assertNotIn("private-body", stderr.getvalue())
        self.assertEqual(self.read_cache()["claude"]["next_retry_at"], "2026-08-30T12:12:00Z")

    def test_failed_atomic_write_preserves_previous_cache_and_warns(self):
        self.seed(with_quota=True)
        before = self.cache.read_bytes()
        with mock.patch.object(collector._NO_REDIRECT_OPENER, "open", side_effect=self.http_error()), mock.patch.object(Path, "replace", side_effect=OSError("private-path")), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.collect()["quota"], self.old_quota)
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.cache])
        self.assertIn("failed to persist retry cooldown", stderr.getvalue())
        self.assertNotIn("private-path", stderr.getvalue())

    def test_fresh_process_reads_persisted_no_cache_cooldown(self):
        script = '''
import json, sys, urllib.error
from pathlib import Path
from unittest import mock
from tools import context_usage_collector as c
c.OFFICIAL_CACHE_PATH = Path(sys.argv[1])
c.OFFICIAL_MIN_INTERVAL_SEC = 720
with mock.patch.object(c, "read_claude_oauth_token", return_value="fake"), mock.patch.object(c, "scan_claude_projects", return_value=([], None)), mock.patch.object(c, "read_ccusage_block", return_value={"projection": {"remainingMinutes": 180}}):
    if sys.argv[2] == "first":
        with mock.patch.object(c._NO_REDIRECT_OPENER, "open", side_effect=urllib.error.HTTPError(c.CLAUDE_USAGE_URL, 429, "simulated", {}, None)) as http:
            agent = c.collect_claude(Path("missing"), "UTC", use_official=True)
        assert http.call_count == 1
    else:
        with mock.patch.object(c, "fetch_claude_official_usage", side_effect=AssertionError("cooldown must skip fetch")) as fetch:
            agent = c.collect_claude(Path("missing"), "UTC", use_official=True)
        fetch.assert_not_called()
    assert agent["quota_source"] == "ccusage_blocks"
    assert "remaining_percentage" not in agent["quota"]["five_hour"]
print("ok")
'''
        for phase in ("first", "second"):
            result = subprocess.run([sys.executable, "-c", script, str(self.cache), phase], cwd=Path(collector.__file__).resolve().parents[1], capture_output=True, text=True, timeout=15, env=dict(os.environ))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ok")
        self.assertEqual(self.read_cache()["claude"]["last_status"], 429)
        self.assertNotIn("quota", self.read_cache()["claude"])


if __name__ == "__main__":
    unittest.main()
