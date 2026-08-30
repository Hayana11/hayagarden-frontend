import datetime as dt
from email.message import Message
import io
import http.server
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from flask import Flask

import context_usage_store
from context_usage_routes import create_context_usage_blueprint
from tools import context_usage_collector as collector


class ContextUsageStoreTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_store_whitelists_fields_and_merges_agents(self):
        accepted, snapshot = context_usage_store.save_report({
            "generated_at": "2026-07-15T06:00:00Z",
            "oauth_token": "top-secret",
            "agents": [{
                "id": "claude",
                "name": "Claude Code",
                "quota_source": "ccusage_blocks",
                "path": "C:/Users/private/.claude/projects",
                "quota": {
                    "updated_at": "2026-07-15T06:00:00Z",
                    "five_hour": {
                        "remaining_percentage": 75,
                        "remaining_minutes": 180,
                        "resets_at": "2026-07-15T09:00:00Z",
                    },
                },
                "active_sessions": [{
                    "latest_context_tokens": 12345,
                    "context_window_tokens": 200000,
                    "raw_prompt": "never store me",
                }],
            }],
        }, self.db_path)

        self.assertEqual(accepted, ["claude"])
        self.assertEqual(snapshot["agents"][0]["quota"]["five_hour"]["used_percentage"], 25)
        context_usage_store.save_report({
            "generated_at": "2026-07-15T06:01:00Z",
            "agents": [{
                "id": "codex",
                "quota_source": "codex_session_jsonl",
                "quota": {
                    "updated_at": "2026-07-15T06:01:00Z",
                    "five_hour": {"used_percent": 18, "resets_at": 1784098800},
                    "seven_day": {"used_percent": 42},
                },
            }],
        }, self.db_path)
        merged = context_usage_store.get_snapshot(self.db_path)
        self.assertEqual([agent["id"] for agent in merged["agents"]], ["claude", "codex"])

        conn = sqlite3.connect(self.db_path)
        stored = "\n".join(row[0] for row in conn.execute("SELECT snapshot_json FROM context_usage_snapshots"))
        conn.close()
        self.assertNotIn("top-secret", stored)
        self.assertNotIn("C:/Users/private", stored)
        self.assertNotIn("never store me", stored)

    def test_older_report_does_not_replace_newer_snapshot(self):
        def report(updated_at, used):
            return context_usage_store.save_report({
                "generated_at": updated_at,
                "agents": [{
                    "id": "codex",
                    "quota_source": "codex_session_jsonl",
                    "quota": {"updated_at": updated_at, "five_hour": {"used_percentage": used}},
                }],
            }, self.db_path)

        report("2026-07-15T07:00:00Z", 40)
        accepted, _ = report("2026-07-15T06:00:00Z", 10)
        self.assertEqual(accepted, [])
        snapshot = context_usage_store.get_snapshot(self.db_path)
        self.assertEqual(snapshot["agents"][0]["quota"]["five_hour"]["used_percentage"], 40)


class ContextUsageRouteTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self.token = "report-secret"
        app = Flask(__name__)
        app.register_blueprint(create_context_usage_blueprint(
            db_path=self.db_path,
            report_token_getter=lambda: self.token,
        ))
        self.client = app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_report_requires_bearer_and_get_returns_redacted_snapshot(self):
        payload = {
            "generated_at": "2026-07-15T06:00:00Z",
            "agents": [{"id": "codex", "quota": {"five_hour": {"used_percentage": 20}}}],
        }
        self.assertEqual(self.client.post("/api/context-usage/report", json=payload).status_code, 401)
        self.assertEqual(self.client.post(
            "/api/context-usage/report",
            json=payload,
            headers={"Authorization": "Bearer wrong"},
        ).status_code, 401)
        saved = self.client.post(
            "/api/context-usage/report",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["accepted"], ["codex"])

        fetched = self.client.get("/api/context-usage")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.headers["Cache-Control"], "no-store")
        self.assertEqual(fetched.get_json()["agents"][0]["id"], "codex")

    def test_missing_server_token_fails_closed(self):
        self.token = ""
        response = self.client.post(
            "/api/context-usage/report",
            json={"agents": [{"id": "codex"}]},
            headers={"Authorization": "Bearer anything"},
        )
        self.assertEqual(response.status_code, 503)


class ContextUsageCollectorTests(unittest.TestCase):
    def test_codex_parser_reads_latest_token_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "2026" / "07" / "session.jsonl"
            session.parent.mkdir(parents=True)
            rows = [
                {"type": "event_msg", "timestamp": "2026-07-15T05:00:00Z", "payload": {"type": "other"}},
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-15T06:00:00Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": 1000, "output_tokens": 200},
                            "total_token_usage": {"total_tokens": 5000},
                            "model_context_window": 258400,
                        },
                        "rate_limits": {
                            "primary": {"used_percent": 18, "resets_at": 1784098800},
                            "secondary": {"used_percent": 42, "resets_at": 1784520000},
                        },
                    },
                },
            ]
            session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            agent = collector.collect_codex(root)
            self.assertEqual(agent["quota_source"], "codex_session_jsonl")
            self.assertEqual(agent["quota"]["five_hour"]["used_percentage"], 18)
            self.assertEqual(agent["quota"]["seven_day"]["remaining_percentage"], 58)
            self.assertEqual(agent["active_sessions"][0]["latest_context_tokens"], 1200)
            self.assertEqual(agent["active_sessions"][0]["context_window_tokens"], 258400)

    def test_ccusage_command_appends_flags_when_env_is_bare_binary(self):
        with mock.patch.dict(os.environ, {"CCUSAGE_COMMAND": "/usr/bin/ccusage"}, clear=False):
            cmd = collector.ccusage_command("Asia/Shanghai")
        self.assertEqual(cmd[0], "/usr/bin/ccusage")
        self.assertIn("blocks", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("--timezone", cmd)
        self.assertIn("Asia/Shanghai", cmd)

    def test_ccusage_command_keeps_explicit_blocks_argv(self):
        with mock.patch.dict(
            os.environ,
            {"CCUSAGE_COMMAND": "/usr/bin/ccusage blocks --active --json --timezone UTC"},
            clear=False,
        ):
            cmd = collector.ccusage_command("Asia/Shanghai")
        self.assertEqual(
            cmd,
            ["/usr/bin/ccusage", "blocks", "--active", "--json", "--timezone", "UTC"],
        )

    def test_claude_block_and_project_parser(self):
        now = dt.datetime(2026, 7, 15, 5, 0, tzinfo=dt.timezone.utc)
        window = collector.claude_window({
            "startTime": "2026-07-15T04:00:00Z",
            "endTime": "2026-07-15T09:00:00Z",
            "projection": {"remainingMinutes": 180, "totalTokens": 44000},
            "totalTokens": 22000,
            "models": ["claude-opus-4-1"],
        }, now=now)
        self.assertNotIn("remaining_percentage", window)
        self.assertNotIn("used_percentage", window)
        self.assertEqual(window["remaining_minutes"], 180)
        self.assertEqual(window["remaining_basis"], "time_until_reset")
        self.assertEqual(window["total_tokens"], 22000)
        self.assertEqual(window["projected_total_tokens"], 44000)

        almost_finished = collector.claude_window({
            "startTime": "2026-07-15T04:00:00Z",
            "endTime": "2026-07-15T09:00:00Z",
            "totalTokens": 22000,
        }, now=dt.datetime(2026, 7, 15, 8, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(almost_finished["remaining_minutes"], 60)
        self.assertNotIn("used_percentage", almost_finished)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "project" / "session.jsonl"
            session.parent.mkdir()
            session.write_text("\n".join([
                json.dumps({
                    "timestamp": "2026-07-15T05:30:00Z",
                    "message": {"model": "claude-opus-4-1", "usage": {"input_tokens": 900, "output_tokens": 100}},
                }),
                json.dumps({
                    "timestamp": "2026-07-15T06:00:00Z",
                    "error": "Weekly limit reached; resets at 09:00",
                }),
            ]) + "\n", encoding="utf-8")
            sessions, limit = collector.scan_claude_projects(root)
            self.assertEqual(sessions[0]["latest_context_tokens"], 1000)
            self.assertEqual(limit["kind"], "weekly")
            self.assertTrue(limit["exhausted"])


class OAuthCredentialReadingTests(unittest.TestCase):
    def test_read_claude_oauth_token_nested_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "creds.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret-token"}}), encoding="utf-8")
            before_text = path.read_text(encoding="utf-8")
            before_mtime = path.stat().st_mtime

            token = collector.read_claude_oauth_token(path)

            self.assertEqual(token, "secret-token")
            # Reading credentials must never modify or refresh them.
            self.assertEqual(path.read_text(encoding="utf-8"), before_text)
            self.assertEqual(path.stat().st_mtime, before_mtime)

    def test_read_claude_oauth_token_flat_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "creds.json"
            path.write_text(json.dumps({"accessToken": "flat-token"}), encoding="utf-8")
            self.assertEqual(collector.read_claude_oauth_token(path), "flat-token")

    def test_read_claude_oauth_token_missing_file_returns_none(self):
        self.assertIsNone(collector.read_claude_oauth_token(Path("/nonexistent/creds.json")))

    def test_read_claude_oauth_token_prefers_setup_token_env_var(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "creds.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}), encoding="utf-8")
            before_text = path.read_text(encoding="utf-8")
            before_mtime = path.stat().st_mtime

            with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "setup-token-value"}):
                token = collector.read_claude_oauth_token(path)

            self.assertEqual(token, "setup-token-value")
            # The env var path must not touch the credentials file at all.
            self.assertEqual(path.read_text(encoding="utf-8"), before_text)
            self.assertEqual(path.stat().st_mtime, before_mtime)

    def test_read_claude_oauth_token_falls_back_to_file_when_env_var_unset(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "creds.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                token = collector.read_claude_oauth_token(path)
            self.assertEqual(token, "file-token")

    def test_read_codex_oauth_nested_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.json"
            path.write_text(
                json.dumps({"tokens": {"access_token": "codex-token", "account_id": "acct-1"}}),
                encoding="utf-8",
            )
            before_mtime = path.stat().st_mtime

            result = collector.read_codex_oauth(path)

            self.assertEqual(result, ("codex-token", "acct-1"))
            self.assertEqual(path.stat().st_mtime, before_mtime)

    def test_read_codex_oauth_missing_token_returns_none(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.json"
            path.write_text(json.dumps({"tokens": {}}), encoding="utf-8")
            self.assertIsNone(collector.read_codex_oauth(path))


class OfficialUsageParsingTests(unittest.TestCase):
    def test_fetch_claude_official_usage_parses_utilization(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=({
            "five_hour": {"utilization": 23, "resets_at": "2026-07-15T18:00:00Z"},
            "seven_day": {"utilization": 70, "resets_at": "2026-07-20T00:00:00Z"},
        }, 200)):
            result = collector.fetch_claude_official_usage("fake-token")
        self.assertEqual(result["five_hour"]["used_percentage"], 23)
        self.assertEqual(result["five_hour"]["remaining_percentage"], 77)
        self.assertEqual(result["seven_day"]["used_percentage"], 70)
        self.assertEqual(result["seven_day"]["remaining_percentage"], 30)

    def test_fetch_claude_official_usage_returns_none_when_fields_missing(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=({"unrelated": True}, 200)):
            self.assertIsNone(collector.fetch_claude_official_usage("fake-token"))

    def test_fetch_claude_official_usage_falls_back_on_http_failure(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=(None, None)):
            self.assertIsNone(collector.fetch_claude_official_usage("fake-token"))

    def test_fetch_claude_official_usage_returns_none_on_429(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=(None, 429)):
            self.assertIsNone(collector.fetch_claude_official_usage("fake-token"))

    def test_fetch_codex_official_usage_parses_rate_limit(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 18,
                    "limit_window_seconds": 18000,
                    "reset_at": 1784098800,
                },
                "secondary_window": {
                    "used_percent": 42,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784520000,
                },
            },
        }, 200)):
            result = collector.fetch_codex_official_usage("fake-token", "acct-1")
        self.assertEqual(result["five_hour"]["used_percentage"], 18)
        self.assertEqual(result["five_hour"]["remaining_percentage"], 82)
        self.assertEqual(result["seven_day"]["used_percentage"], 42)
        self.assertEqual(result["seven_day"]["remaining_percentage"], 58)

    def test_fetch_codex_weekly_only_primary_maps_to_seven_day(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 12,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784520000,
                },
            },
        }, 200)):
            result = collector.fetch_codex_official_usage("fake-token", "acct-1")
        self.assertEqual(result["five_hour"], {})
        self.assertEqual(result["seven_day"]["used_percentage"], 12)

    def test_rate_limit_detail_ignores_diary_like_lines(self):
        diary = (
            'resets 4pm (U\\n---\\n[DIARY 2026-06-11] ---\\n\\n今日无事可记。'
            'weekly limit is mentioned in prose only'
        )
        self.assertIsNone(collector.rate_limit_detail(diary, "2026-07-18T03:52:06Z"))

    def test_collect_claude_drops_jsonl_limit_when_official_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "official-cache.json"
            creds = root / "creds.json"
            creds.write_text(json.dumps({"accessToken": "tok"}), encoding="utf-8")
            session = root / "session.jsonl"
            session.write_text(json.dumps({
                "timestamp": "2026-07-18T03:52:06Z",
                "error": "Weekly limit reached; resets at 09:00",
            }) + "\n", encoding="utf-8")
            with mock.patch.object(collector, "OFFICIAL_CACHE_PATH", cache):
                with mock.patch.object(collector, "fetch_claude_official_usage", return_value={
                    "five_hour": {"used_percentage": 10, "remaining_percentage": 90},
                    "seven_day": {"used_percentage": 20, "remaining_percentage": 80},
                    "updated_at": "2026-07-18T04:00:00Z",
                }):
                    agent = collector.collect_claude(root, "UTC", creds, use_official=True)
        self.assertEqual(agent["quota_source"], "claude_oauth_usage")
        self.assertNotIn("effective_limit", agent["quota"])

    def test_collect_claude_reuses_cache_on_429_instead_of_jsonl_exhausted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "official-cache.json"
            cache.write_text(json.dumps({
                "claude": {
                    "fetched_at": "2026-07-18T06:00:00Z",
                    "quota": {
                        "five_hour": {"used_percentage": 40, "remaining_percentage": 60},
                        "seven_day": {"used_percentage": 55, "remaining_percentage": 45},
                        "updated_at": "2026-07-18T06:00:00Z",
                    },
                },
            }), encoding="utf-8")
            session = root / "session.jsonl"
            session.write_text(json.dumps({
                "timestamp": "2026-07-18T07:00:00Z",
                "error": "Weekly limit reached; resets at 09:00",
            }) + "\n", encoding="utf-8")
            creds = root / "creds.json"
            creds.write_text(json.dumps({"accessToken": "tok"}), encoding="utf-8")
            with mock.patch.object(collector, "OFFICIAL_CACHE_PATH", cache):
                with mock.patch.object(collector, "OFFICIAL_MIN_INTERVAL_SEC", 0):
                    with mock.patch.object(collector, "fetch_claude_official_usage", return_value=None):
                        agent = collector.collect_claude(root, "UTC", creds, use_official=True)
        self.assertEqual(agent["quota_source"], "claude_oauth_usage")
        self.assertEqual(agent["quota"]["five_hour"]["used_percentage"], 40)
        self.assertNotIn("effective_limit", agent["quota"])

    def test_fetch_codex_official_usage_returns_none_on_http_failure(self):
        with mock.patch.object(collector, "_http_get_json_result", return_value=(None, None)):
            self.assertIsNone(collector.fetch_codex_official_usage("fake-token", ""))


class OfficialUsageFallbackTests(unittest.TestCase):
    def test_collect_codex_default_signature_never_touches_network(self):
        with mock.patch.object(collector, "_http_get_json", side_effect=AssertionError("network should not be called")):
            with tempfile.TemporaryDirectory() as temp:
                agent = collector.collect_codex(Path(temp))
        self.assertEqual(agent["quota_source"], "unavailable")

    def test_collect_codex_uses_official_source_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "official-cache.json"
            auth_path = root / "auth.json"
            auth_path.write_text(
                json.dumps({"tokens": {"access_token": "tok", "account_id": "acct"}}), encoding="utf-8",
            )
            with mock.patch.object(collector, "OFFICIAL_CACHE_PATH", cache):
                with mock.patch.object(collector, "fetch_codex_official_usage", return_value={
                    "five_hour": {"used_percentage": 18, "remaining_percentage": 82},
                    "seven_day": {"used_percentage": 42, "remaining_percentage": 58},
                    "updated_at": "2026-07-15T06:00:00Z",
                }):
                    agent = collector.collect_codex(root / "sessions", auth_path, use_official=True)
        self.assertEqual(agent["quota_source"], "codex_oauth_usage")
        self.assertEqual(agent["quota"]["five_hour"]["used_percentage"], 18)

    def test_collect_codex_falls_back_to_local_when_official_fetch_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "official-cache.json"
            sessions_dir = root / "sessions"
            sessions_dir.mkdir()
            session = sessions_dir / "session.jsonl"
            session.write_text(json.dumps({
                "type": "event_msg",
                "timestamp": "2026-07-15T06:00:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 10}},
                    "rate_limits": {"primary": {"used_percent": 5}},
                },
            }) + "\n", encoding="utf-8")
            auth_path = root / "auth.json"
            auth_path.write_text(json.dumps({"tokens": {"access_token": "tok"}}), encoding="utf-8")

            with mock.patch.object(collector, "OFFICIAL_CACHE_PATH", cache):
                with mock.patch.object(collector, "fetch_codex_official_usage", return_value=None):
                    agent = collector.collect_codex(sessions_dir, auth_path, use_official=True)
        self.assertEqual(agent["quota_source"], "codex_session_jsonl")
        self.assertEqual(agent["quota"]["five_hour"]["used_percentage"], 5)

    def test_collect_claude_uses_official_source_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "official-cache.json"
            creds = root / "creds.json"
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8")
            with mock.patch.object(collector, "OFFICIAL_CACHE_PATH", cache):
                with mock.patch.object(collector, "fetch_claude_official_usage", return_value={
                    "five_hour": {"used_percentage": 23, "remaining_percentage": 77},
                    "seven_day": {"used_percentage": 70, "remaining_percentage": 30},
                    "updated_at": "2026-07-15T06:00:00Z",
                }):
                    agent = collector.collect_claude(root / "projects", "UTC", creds, use_official=True)
        self.assertEqual(agent["quota_source"], "claude_oauth_usage")
        self.assertEqual(agent["quota"]["five_hour"]["used_percentage"], 23)
        self.assertEqual(agent["quota"]["seven_day"]["used_percentage"], 70)

    def test_collect_claude_no_official_usage_flag_skips_network_and_ccusage_mock(self):
        with mock.patch.object(collector, "fetch_claude_official_usage", side_effect=AssertionError("should not be called")), \
             mock.patch.object(collector, "read_ccusage_block", return_value=None):
            with tempfile.TemporaryDirectory() as temp:
                agent = collector.collect_claude(Path(temp), "UTC", use_official=False)
        self.assertNotEqual(agent["quota_source"], "claude_oauth_usage")


class OfficialUsageToggleTests(unittest.TestCase):
    def test_cli_flag_disables_official_usage(self):
        args = collector.parse_args(["--no-official-usage"])
        self.assertFalse(collector.official_usage_enabled(args))

    def test_env_var_disables_official_usage(self):
        args = collector.parse_args([])
        with mock.patch.dict(os.environ, {"CONTEXT_USAGE_OFFICIAL": "0"}):
            self.assertFalse(collector.official_usage_enabled(args))

    def test_enabled_by_default(self):
        args = collector.parse_args([])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTEXT_USAGE_OFFICIAL", None)
            self.assertTrue(collector.official_usage_enabled(args))


class RedirectDoesNotLeakAuthorizationTests(unittest.TestCase):
    """Regression test for the cross-host redirect credential leak.

    Reproduces the reported issue exactly: an origin server on 127.0.0.1
    redirects to a different hostname (localhost). The fix must refuse to
    follow the redirect at all, so the second server must never see the
    request (and therefore never see the Authorization header).
    """

    def test_redirect_is_refused_and_authorization_is_never_replayed(self):
        captured_auth = []

        class CaptureHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                captured_auth.append(self.headers.get("Authorization"))
                body = b'{"five_hour": {"utilization": 99}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        capture_server = http.server.HTTPServer(("127.0.0.1", 0), CaptureHandler)
        capture_thread = threading.Thread(target=capture_server.serve_forever, daemon=True)
        capture_thread.start()
        capture_port = capture_server.server_address[1]

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{capture_port}/usage")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        origin_server = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
        origin_thread = threading.Thread(target=origin_server.serve_forever, daemon=True)
        origin_thread.start()
        origin_port = origin_server.server_address[1]

        try:
            result = collector._http_get_json(
                f"http://127.0.0.1:{origin_port}/usage",
                headers={"Authorization": "Bearer top-secret"},
                timeout=5,
            )
        finally:
            origin_server.shutdown()
            capture_server.shutdown()
            origin_thread.join()
            capture_thread.join()
            origin_server.server_close()
            capture_server.server_close()

        self.assertIsNone(result)
        self.assertEqual(captured_auth, [])


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
