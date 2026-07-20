import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from wake.builder import append_system_text, inject_snippets
from wake.executor import execute
from wake.usage import append_usage_round, build_wake_cache_info, usage_round_from_result


def _result(*, inp, out, read=0, create=0, create_5m=0, create_1h=0):
    return {
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create,
            "cache_creation": {
                "ephemeral_5m_input_tokens": create_5m,
                "ephemeral_1h_input_tokens": create_1h,
            },
        }
    }


class WakeUsageTests(unittest.TestCase):
    def test_normalizes_every_non_stream_round(self):
        row = usage_round_from_result(
            _result(inp=12, out=3, read=100, create=20, create_5m=18, create_1h=2),
            index=2,
        )
        self.assertEqual(row["index"], 2)
        self.assertEqual(row["cache_read"], 100)
        self.assertEqual(row["cache_creation"], 20)
        self.assertEqual(row["cache_creation_5m"], 18)
        self.assertEqual(row["cache_creation_1h"], 2)
        self.assertEqual(row["context_tokens"], 132)

    def test_aggregates_tool_and_format_rounds_into_one_cache_info(self):
        rounds = []
        append_usage_round(rounds, _result(inp=10, out=2, create=100, create_5m=100))
        append_usage_round(rounds, _result(inp=8, out=4, read=100))
        seen = {}

        def payload_builder(**kwargs):
            seen.update(kwargs)
            return {"cost_usd": 0.25, "cost_estimated": True}

        payload = build_wake_cache_info(
            rounds,
            elapsed_sec=1.2349,
            cache_supported=True,
            mode="normal",
            model="claude-opus-4-6",
            payload_builder=payload_builder,
        )
        self.assertEqual(seen["input_tokens"], 18)
        self.assertEqual(seen["output_tokens"], 6)
        self.assertEqual(seen["cache_read"], 100)
        self.assertEqual(seen["cache_creation"], 100)
        self.assertEqual(seen["cache_creation_5m"], 100)
        self.assertEqual(seen["elapsed_sec"], 1.235)
        self.assertEqual(payload["provider"], "api_relay")
        self.assertEqual(payload["source"], "wake")
        self.assertEqual(payload["mode"], "normal")
        self.assertEqual(payload["wake_mode"], "normal")
        self.assertEqual(payload["num_rounds"], 2)
        self.assertEqual(payload["last_round_context"], 108)
        self.assertEqual(payload["max_round_context"], 110)
        self.assertEqual(payload["cost_usd"], 0.25)


class WakeSystemCacheTests(unittest.TestCase):
    def test_appending_dynamic_text_preserves_static_cache_blocks(self):
        cached = {
            "type": "text",
            "text": "stable persona",
            "cache_control": {"type": "ephemeral"},
        }
        original = [cached]
        result = append_system_text(original, "dynamic wake context")
        self.assertEqual(original, [cached])
        self.assertEqual(result[0], cached)
        self.assertEqual(result[-1], {"type": "text", "text": "dynamic wake context"})
        self.assertNotIn("cache_control", result[-1])

    def test_injected_snippets_do_not_flatten_blocks(self):
        drive = types.SimpleNamespace(get_wake_snippet=lambda: "drive state")
        desire = types.SimpleNamespace(
            get_wake_snippet=lambda t_hours_override=None: "desire state"
        )
        base = [{
            "type": "text",
            "text": "stable",
            "cache_control": {"type": "ephemeral"},
        }]
        with mock.patch.dict(sys.modules, {"drive_engine": drive, "desire": desire}):
            result = inject_snippets(base, "normal", desire_driven=True)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual([row["text"] for row in result[1:]], ["drive state", "desire state"])


class WakeExecutorUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "wake.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thoughts TEXT, action TEXT, content TEXT, consumed INTEGER,
                woke_at TEXT, surfaced_desire_ids TEXT, cache_info TEXT
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT, content TEXT, thinking TEXT, cache_info TEXT
            );
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT, content TEXT, layer TEXT, author TEXT, processed INTEGER
            );
            """
        )
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        return sqlite3.connect(self.db_path)

    def test_message_persists_same_usage_to_wake_and_chat_rows(self):
        cache_info = {"v": 2, "provider": "api_relay", "source": "wake", "cost_usd": 0.5}
        execute(
            "message", "thought", "hello", "normal", self.get_db,
            cache_info=cache_info,
        )
        conn = self.get_db()
        wake_raw = conn.execute("SELECT cache_info FROM wake_log").fetchone()[0]
        chat_row = conn.execute(
            "SELECT author, content, thinking, cache_info FROM chat_messages"
        ).fetchone()
        conn.close()
        self.assertEqual(json.loads(wake_raw), cache_info)
        self.assertEqual(chat_row[:3], ("fyodor", "hello", "thought"))
        self.assertEqual(json.loads(chat_row[3]), cache_info)

    def test_none_action_still_persists_hidden_model_spend(self):
        cache_info = {"v": 2, "provider": "api_relay", "source": "wake", "cost_usd": 0.3}
        execute("none", "wait", "", "normal", self.get_db, cache_info=cache_info)
        conn = self.get_db()
        wake_raw = conn.execute("SELECT cache_info FROM wake_log").fetchone()[0]
        chat_count = conn.execute("SELECT count(*) FROM chat_messages").fetchone()[0]
        conn.close()
        self.assertEqual(json.loads(wake_raw), cache_info)
        self.assertEqual(chat_count, 0)

    def test_legacy_test_schema_without_cache_columns_still_works(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, thoughts TEXT, action TEXT, content TEXT,
                consumed INTEGER, woke_at TEXT
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT
            );
            """
        )
        conn.close()

        def legacy_db():
            return sqlite3.connect(legacy_path)

        execute("message", "t", "c", "normal", legacy_db, cache_info={"cost_usd": 1})
        conn = legacy_db()
        self.assertEqual(conn.execute("SELECT count(*) FROM wake_log").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM chat_messages").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
