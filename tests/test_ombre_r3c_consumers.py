"""R3C normalized adapter and direct-consumer tests."""

from __future__ import annotations

import contextlib
import io
import os
import runpy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import ombre_adapter


def item(
    bucket_id,
    *,
    bucket_type="dynamic",
    domain=None,
    arousal=0.3,
    last_active="2026-10-01T10:00:00",
    created="2026-10-01T09:00:00",
    content="content",
    **extra,
):
    metadata = {
        "id": bucket_id,
        "name": "name-" + bucket_id,
        "type": bucket_type,
        "domain": list(domain or []),
        "tags": ["tag"],
        "valence": 0.5,
        "arousal": arousal,
        "importance": 5,
        "created": created,
        "last_active": last_active,
    }
    metadata.update(extra)
    return {"id": bucket_id, "metadata": metadata, "content": content}


class FakeManager:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []
        self.updates = []

    async def list_all(self, include_archive=False):
        self.calls.append(("list_all", include_archive))
        return list(self.items)

    async def get(self, bucket_id):
        self.calls.append(("get", bucket_id))
        return next((x for x in self.items if x["id"] == bucket_id), None)

    async def update(self, bucket_id, **kwargs):
        self.updates.append((bucket_id, kwargs))
        return True


class NormalizedAdapterTests(unittest.TestCase):
    def test_legacy_catalog_filters_terminal_and_normalizes_exact_schema(self):
        manager = FakeManager([
            item("low", domain=["恋爱"], arousal=0.2),
            item("high", domain=["恋爱"], arousal=0.8, last_active="2026-10-01T11:00:00"),
            item("other", domain=["技术"], arousal=0.9),
            item("archived", bucket_type="archived", domain=["恋爱"], arousal=1.0),
            item("deleted", domain=["恋爱"], deleted_at="2026-10-01T12:00:00"),
        ])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "legacy_module"}),              mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            rows = ombre_adapter.list_memory_records(
                bucket_type="dynamic",
                domain="恋爱",
                min_arousal=0.6,
                include_content=True,
            )
        self.assertEqual([row["id"] for row in rows], ["high"])
        self.assertEqual(set(rows[0]), {
            "id", "name", "type", "domain", "tags", "valence", "arousal",
            "importance", "created", "last_active", "content",
        })
        self.assertEqual(manager.calls, [("list_all", False)])

    def test_single_lookup_is_id_based_and_rejects_path(self):
        target = item("abc123def456", domain=["恋爱"])
        manager = FakeManager([target])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            self.assertEqual(
                ombre_adapter.get_memory_record("abc123def456")["id"],
                "abc123def456",
            )
            self.assertIsNone(ombre_adapter.get_memory_record("/tmp/abc123def456.md"))
        self.assertEqual(manager.calls, [("get", "abc123def456")])

    def test_legacy_emotion_update_uses_bucket_manager_by_id(self):
        manager = FakeManager([])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            result = ombre_adapter.update_memory_emotion("abc123def456", -0.6, 0.55)
        self.assertTrue(result)
        self.assertEqual(manager.updates, [
            ("abc123def456", {"valence": 0.2, "arousal": 0.55}),
        ])

    def test_http_catalog_uses_buckets_and_details_not_search(self):
        summaries = [
            {
                "id": "a",
                "name": "a",
                "type": "dynamic",
                "domain": ["恋爱"],
                "tags": [],
                "valence": 0.5,
                "arousal": 0.8,
                "importance": 5,
                "created": "2026-10-01T09:00:00",
                "last_active": "2026-10-01T11:00:00",
            },
            {
                "id": "arch",
                "name": "arch",
                "type": "archived",
                "domain": ["恋爱"],
                "arousal": 1.0,
            },
        ]
        detail = {
            "id": "a",
            "metadata": summaries[0],
            "content": "detail",
        }
        calls = []

        def http_json(path, **kwargs):
            calls.append(path)
            return summaries if path == "/api/buckets" else detail

        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}),              mock.patch.object(ombre_adapter, "_http_json", side_effect=http_json):
            rows = ombre_adapter.list_memory_records(
                bucket_type="dynamic", domain="恋爱", include_content=True, limit=1
            )
        self.assertEqual([row["id"] for row in rows], ["a"])
        self.assertEqual(rows[0]["content"], "detail")
        self.assertEqual(calls, ["/api/buckets", "/api/bucket/a"])

    def test_http_trace_has_stored_unipolar_values_and_no_reinforce(self):
        calls = []

        async def fake_mcp(tool_name, arguments, *, timeout):
            calls.append((tool_name, arguments))
            return "ok"

        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}),              mock.patch.object(ombre_adapter, "_mcp_call", side_effect=fake_mcp):
            self.assertEqual(
                ombre_adapter.update_memory_emotion("abc123def456", -0.6, 0.55),
                "ok",
            )
        self.assertEqual(calls, [(
            "trace",
            {"bucket_id": "abc123def456", "valence": 0.2, "arousal": 0.55},
        )])


class ConsumerTests(unittest.TestCase):
    def test_thought_consumer_uses_adapter_filter(self):
        import tools.thought_gen as thought_gen

        with mock.patch.object(
            thought_gen, "sys", thought_gen.sys
        ), mock.patch(
            "tools.ombre_adapter.list_memory_records",
            return_value=[{"content": "thought"}],
        ) as listed:
            self.assertEqual(thought_gen.get_emotional_buckets(), ["thought"])
        listed.assert_called_once_with(
            bucket_type="dynamic",
            min_arousal=0.6,
            include_content=True,
            limit=4,
            sort="last_active_desc",
        )

    def test_cli_returns_only_latest_adapter_content(self):
        cli_path = str(Path(__file__).parents[1] / "tools" / "ombre_read_cli.py")
        with mock.patch(
            "tools.ombre_adapter.list_memory_records",
            return_value=[{"content": "latest"}],
        ) as listed, mock.patch.object(
            sys, "argv",
            [cli_path, "latest-domain", "--type", "permanent", "--domain", "呼吸间"],
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(cli_path, run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "latest")
        listed.assert_called_once_with(
            bucket_type="permanent",
            domain="呼吸间",
            include_content=True,
            limit=1,
            sort="last_active_desc",
        )

    def test_discord_has_no_direct_breath_path_or_literal_token(self):
        source = Path(__file__).parents[1].joinpath("tools", "discord_listener.js").read_text(encoding="utf-8")
        self.assertNotIn("BREATH_DIR", source)
        self.assertNotIn("XIAOKE_TOKEN", source)
        self.assertIn("process.env.DISCORD_BOT_TOKEN", source)
        self.assertIn("ombre_read_cli.py", source)
        self.assertIn("await loadBreathMemory()", source)


if __name__ == "__main__":
    unittest.main()
