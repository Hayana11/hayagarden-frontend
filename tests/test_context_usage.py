import datetime as dt
import http.server
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
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
        with mock.patch.object(collector, "_http_get_json", return_value={
            "five_hour": {"utilization": 23, "resets_at": "2026-07-15T18:00:00Z"},
            "seven_day": {"utilization": 70, "resets_at": "2026-07-20T00:00:00Z"},
        }):
            result = collector.fetch_claude_official_usage("fake-token")
        self.assertEqual(result["five_hour"]["used_percentage"], 23)
        self.assertEqual(result["five_hour"]["remaining_percentage"], 77)
        self.assertEqual(result["seven_day"]["used_percentage"], 70)
        self.assertEqual(result["seven_day"]["remaining_percentage"], 30)

    def test_fetch_claude_official_usage_returns_none_when_fields_missing(self):
        with mock.patch.object(collector, "_http_get_json", return_value={"unrelated": True}):
            self.assertIsNone(collector.fetch_claude_official_usage("fake-token"))

    def test_fetch_claude_official_usage_falls_back_on_http_failure(self):
        with mock.patch.object(collector, "_http_get_json", return_value=None):
            self.assertIsNone(collector.fetch_claude_official_usage("fake-token"))

    def test_fetch_codex_official_usage_parses_rate_limit(self):
        with mock.patch.object(collector, "_http_get_json", return_value={
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
        }):
            result = collector.fetch_codex_official_usage("fake-token", "acct-1")
        self.assertEqual(result["five_hour"]["used_percentage"], 18)
        self.assertEqual(result["five_hour"]["remaining_percentage"], 82)
        self.assertEqual(result["seven_day"]["used_percentage"], 42)
        self.assertEqual(result["seven_day"]["remaining_percentage"], 58)

    def test_fetch_codex_weekly_only_primary_maps_to_seven_day(self):
        with mock.patch.object(collector, "_http_get_json", return_value={
            "rate_limit": {
                "primary_window": {
                    "used_percent": 12,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784520000,
                },
            },
        }):
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
            creds = root / "creds.json"
            creds.write_text(json.dumps({"accessToken": "tok"}), encoding="utf-8")
            session = root / "session.jsonl"
            session.write_text(json.dumps({
                "timestamp": "2026-07-18T03:52:06Z",
                "error": "Weekly limit reached; resets at 09:00",
            }) + "\n", encoding="utf-8")
            with mock.patch.object(collector, "fetch_claude_official_usage", return_value={
                "five_hour": {"used_percentage": 10, "remaining_percentage": 90},
                "seven_day": {"used_percentage": 20, "remaining_percentage": 80},
                "updated_at": "2026-07-18T04:00:00Z",
            }):
                agent = collector.collect_claude(root, "UTC", creds, use_official=True)
        self.assertEqual(agent["quota_source"], "claude_oauth_usage")
        self.assertNotIn("effective_limit", agent["quota"])

    def test_fetch_codex_official_usage_returns_none_on_http_failure(self):
        with mock.patch.object(collector, "_http_get_json", return_value=None):
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
            auth_path = root / "auth.json"
            auth_path.write_text(
                json.dumps({"tokens": {"access_token": "tok", "account_id": "acct"}}), encoding="utf-8",
            )
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

            with mock.patch.object(collector, "fetch_codex_official_usage", return_value=None):
                agent = collector.collect_codex(sessions_dir, auth_path, use_official=True)
        self.assertEqual(agent["quota_source"], "codex_session_jsonl")
        self.assertEqual(agent["quota"]["five_hour"]["used_percentage"], 5)

    def test_collect_claude_uses_official_source_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            creds = root / "creds.json"
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
