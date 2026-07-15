import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
