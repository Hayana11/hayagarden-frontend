"""B-4: every reader of memory types follows the cutover switch.

Off: the old posts results, unchanged. On: MEMORY / FACT / MISS come from the
Kernel (legacy posts copies are never read again), other types stay in posts.
"""
from __future__ import annotations

import builtins
import io
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from tests.test_memory_kernel_read import ReadFixture
from tests.test_memory_kernel_store import set_flag
from tools import memory_kernel_read as reader
from tools import memory_kernel_store as store

ROOT = Path(__file__).resolve().parents[1]
LEGACY = "旧帖子里的记忆只在开关关闭时可见。"


def _fake_jieba():
    module = types.ModuleType("jieba")
    module.cut = lambda text: text.split()
    return module


_GATEWAY = None


def load_gateway():
    """Import gateway once with /opt/frontend isolated (see tests/test_cc_effort.py)."""
    global _GATEWAY
    if _GATEWAY is None:
        tmp = tempfile.mkdtemp(prefix="kernel-read-gw-")
        real_connect, real_open = sqlite3.connect, builtins.open

        def isolated_connect(database, *args, **kwargs):
            raw = os.fspath(database)
            if raw.startswith("/opt/frontend/"):
                database = str(Path(tmp) / Path(raw).name)
            return real_connect(database, *args, **kwargs)

        def isolated_open(file, *args, **kwargs):
            try:
                raw = os.fspath(file)
            except TypeError:
                raw = ""
            if raw == "/opt/frontend/.env":
                return io.StringIO("")
            return real_open(file, *args, **kwargs)

        with mock.patch.object(sqlite3, "connect", side_effect=isolated_connect), \
                mock.patch.object(builtins, "open", side_effect=isolated_open):
            import gateway
        _GATEWAY = gateway
    return _GATEWAY


class PathsFixture(ReadFixture):
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.posts)
        for column in ("recall_count INTEGER DEFAULT 0", "last_recalled_at TEXT", "valence REAL",
                       "arousal REAL", "processed INTEGER DEFAULT 0"):
            conn.execute(f"ALTER TABLE posts ADD COLUMN {column}")
        conn.execute("INSERT INTO posts (id, type, content, author, created_at, layer, tags) VALUES "
                     "(40, 'MEMORY', ?, 'fyodor', datetime('now','+8 hours','-1 hours'), 'long-term', 'memo')",
                     (LEGACY,))
        conn.execute("INSERT INTO posts (id, type, content, author, created_at, layer) VALUES "
                     "(41, 'DIARY', '今天的日记写了看海。', 'fyodor', datetime('now','+8 hours','-1 days'), 'long-term')")
        conn.execute("INSERT INTO posts (id, type, content, author, created_at) VALUES "
                     "(42, 'FACT', '旧帖子里的事实：她怕打雷。', 'fyodor', '2026-10-01 10:00:00')")
        conn.commit()
        conn.close()
        self.sync()  # so the Kernel and Ombre agree before each test

    def kernel_memory(self, content, **kwargs):
        kwargs.setdefault("source", "test")
        kwargs.setdefault("runtime", self.ombre)
        return store.remember(content, kernel_db=self.kernel, **kwargs)

    def conn(self):
        conn = sqlite3.connect(self.posts)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn


class SystemBuilderTests(PathsFixture):
    def setUp(self):
        super().setUp()
        sys.modules.setdefault("config_store", __import__("config_store"))
        from chat import system_builder

        self.sb = system_builder

    def test_switch_off_reads_posts(self):
        conn = self.conn()
        self.assertEqual(self.sb._kernel_facts_and_long_term(conn), (None, None))
        self.assertIsNone(self.sb._kernel_memos(conn))

    def test_switch_on_reads_the_kernel(self):
        self.kernel_memory("她和我约好每年冬至去临江公园。", memory_type="FACT", importance=9)
        self.kernel_memory("网页窗口：今天聊了搬家。", tags="memo")
        self.kernel_memory("她说我们之间最重要的是在场。", layer="long-term")
        set_flag(self.posts, "1")
        conn = self.conn()
        facts, long_term = self.sb._kernel_facts_and_long_term(conn)
        self.assertEqual(facts[0]["content"], "她和我约好每年冬至去临江公园。")
        self.assertIn("哈娅不喜欢老豆腐，喜欢内酯豆腐。", [f["content"] for f in facts])
        self.assertNotIn("旧帖子里的事实：她怕打雷。", [f["content"] for f in facts])
        self.assertEqual({m["content"] for m in long_term}, {"她说我们之间最重要的是在场。", "今天的日记写了看海。"})
        self.assertEqual([m["content"] for m in self.sb._kernel_memos(conn)], ["网页窗口：今天聊了搬家。"])

    def test_kernel_failure_with_switch_on_never_falls_back_to_posts(self):
        set_flag(self.posts, "1")
        with mock.patch.object(reader, "facts", side_effect=sqlite3.DatabaseError("corrupt")), \
                self.assertLogs("chat.system_builder", "ERROR"):
            self.assertEqual(self.sb._kernel_facts_and_long_term(self.conn()), ([], []))


class SearchToolTests(PathsFixture):
    def test_memory_tool_search(self):
        self.assertEqual([r["id"] for r in self.memory_tool.search_memories("旧帖子里的记忆")], [40])
        set_flag(self.posts, "1")
        self.assertEqual(self.memory_tool.search_memories("旧帖子里的记忆"), [])
        written = self.kernel_memory("她把薄荷搬到了阳台上。")
        with mock.patch("tools.ombre_adapter.search_bucket_ids", return_value=[written["bucket_id"]]) as ombre:
            hits = self.memory_tool.search_memories("薄荷")
        ombre.assert_called_once()
        self.assertEqual([h["ref"] for h in hits], [f"kernel:{written['lineage_id']}"])
        with mock.patch("tools.ombre_adapter.search_bucket_ids", return_value=[]):
            self.assertEqual([h["id"] for h in self.memory_tool.search_memories("日记")], [41, 3])

    def test_product_handler_search(self):
        from tools.product_handlers import search_memory_posts

        self.assertIn(40, [r["id"] for r in search_memory_posts(self.conn(), keyword="旧帖子")])
        set_flag(self.posts, "1")
        with mock.patch("tools.ombre_adapter.search_bucket_ids", return_value=["bbb222"]):
            hits = search_memory_posts(self.conn(), keyword="小家", limit=8)
            self.assertEqual([h["ref"] for h in hits], ["kernel:lin:ombre:bbb222"])
            self.assertNotIn(40, [r["id"] for r in search_memory_posts(self.conn(), keyword="旧帖子")])
        newest = search_memory_posts(self.conn(), keyword="", limit=50)
        self.assertNotIn(40, [r["id"] for r in newest])
        self.assertIn(41, [r["id"] for r in newest])
        self.assertIn("kernel:lin:ombre:bbb222", [r.get("ref") for r in newest])

    def test_explicit_search_never_touches(self):
        set_flag(self.posts, "1")
        with mock.patch.object(reader, "touch_buckets") as touch, \
                mock.patch("tools.ombre_adapter.search_bucket_ids", return_value=["bbb222"]):
            self.memory_tool.search_memories("小家")
        touch.assert_not_called()


class BatchReaderTests(PathsFixture):
    def test_fact_extractor_dedupes_against_kernel_facts(self):
        sys.modules.setdefault("llm_lite", types.ModuleType("llm_lite"))
        sys.modules.setdefault("memory_tool", self.memory_tool)
        from tools import fact_extractor

        with mock.patch.object(fact_extractor, "DB", str(self.posts)):
            self.assertIn("旧帖子里的事实：她怕打雷。", fact_extractor._existing_facts())
            set_flag(self.posts, "1")
            self.memory_tool.save_memory("她不吃香菜。", type="FACT")
            facts = fact_extractor._existing_facts()
        self.assertIn("她不吃香菜。", facts)
        self.assertNotIn("旧帖子里的事实：她怕打雷。", facts)

    def test_dream_draws_recent_memories_from_the_kernel(self):
        from tools import dream_generator

        set_flag(self.posts, "1")
        self.kernel_memory("她今天在雨里跑回家。")
        frags = dream_generator._collect_recent_memories(self.conn(), n=8)
        contents = [f["content"] for f in frags]
        self.assertIn("她今天在雨里跑回家。", contents)
        self.assertIn("今天的日记写了看海。", contents)
        self.assertNotIn(LEGACY, contents)


class GatewayRecallTests(PathsFixture):
    def setUp(self):
        super().setUp()
        self.gateway = load_gateway()
        for patcher in (
            mock.patch.dict(sys.modules, {"jieba": _fake_jieba()}),
            mock.patch.object(self.gateway, "get_db", side_effect=self.conn),
            mock.patch.object(self.gateway.config_store, "get_int", side_effect=lambda k, d=None: d),
            mock.patch.object(self.gateway.config_store, "get_bool", side_effect=lambda k, d=None: d),
            mock.patch.object(self.gateway, "_memory_runtime_provenance", return_value={}),
            mock.patch("tools.memory_runtime_bridge.record_legacy_recall"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_switch_off_keeps_the_posts_recall(self):
        with mock.patch.object(self.gateway, "_ombre_recall_search", return_value=[]):
            block, items = self.gateway._recall_memories("旧帖子里的记忆只在开关关闭时可见。 记忆")
        self.assertIn(LEGACY, block)

    def test_switch_on_ombre_ranks_kernel_text_and_touches_only_what_was_injected(self):
        set_flag(self.posts, "1")
        pending = self.kernel_memory("下雨天她把伞给了我。", runtime=None)  # Ombre has not seen it
        with mock.patch("tools.ombre_adapter.search_bucket_ids", return_value=["bbb222", "aaa111"]), \
                mock.patch.object(reader, "touch_buckets") as touch, \
                mock.patch.object(self.gateway, "_ombre_recall_search") as legacy_ombre:
            block, items = self.gateway._recall_memories("下雨天 伞给了我 旧帖子里的记忆只在开关关闭时可见。 记忆")
        legacy_ombre.assert_not_called()
        self.assertNotIn(LEGACY, block)
        self.assertIn("下雨天她把伞给了我。", block)  # unprojected head, lexical side
        self.assertIn("我们的小家已经很完美了。", block)  # Ombre-ranked, Kernel text
        self.assertNotIn("渐变脑", block)
        [bucket_ids] = touch.call_args.args
        self.assertEqual(sorted(bucket_ids), ["aaa111", "bbb222"])
        self.assertNotIn(pending["bucket_id"], bucket_ids)


_APP = None


def load_app():
    """Import app.py once with /opt/frontend isolated (see tests/test_cc_effort.py)."""
    global _APP
    if _APP is None:
        tmp = tempfile.mkdtemp(prefix="kernel-read-app-")
        real_connect, real_open = sqlite3.connect, builtins.open
        conn = real_connect(str(Path(tmp) / "memories.db"))
        conn.execute(  # app.py migrates chat_messages at import time
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL DEFAULT 'user',"
            " content TEXT NOT NULL DEFAULT '', thinking TEXT DEFAULT '', tool_calls TEXT DEFAULT '',"
            " branches TEXT DEFAULT '', branch_idx INTEGER DEFAULT 0, cache_info TEXT DEFAULT '',"
            " choices TEXT DEFAULT '', image_url TEXT DEFAULT '', file_url TEXT DEFAULT '',"
            " file_name TEXT DEFAULT '', attachments TEXT DEFAULT '[]', display_segments TEXT DEFAULT '',"
            " source_kind TEXT NOT NULL DEFAULT 'chat', created_at TEXT DEFAULT (datetime('now')))")
        conn.commit()
        conn.close()

        def isolated_connect(database, *args, **kwargs):
            raw = os.fspath(database)
            if raw.startswith("/opt/frontend/"):
                database = str(Path(tmp) / Path(raw).name)
            return real_connect(database, *args, **kwargs)

        def isolated_open(file, *args, **kwargs):
            try:
                raw = os.fspath(file)
            except TypeError:
                raw = ""
            if raw == "/opt/frontend/.env":
                return io.StringIO("")
            return real_open(file, *args, **kwargs)

        with mock.patch.object(sqlite3, "connect", side_effect=isolated_connect), \
                mock.patch.object(builtins, "open", side_effect=isolated_open):
            sys.modules.setdefault("moments_cover", types.ModuleType("moments_cover"))
            if "account_balance_routes" not in sys.modules:
                from flask import Blueprint
                routes = types.ModuleType("account_balance_routes")
                routes.create_relay_account_blueprint = lambda **_kw: Blueprint("kernel_read_stub", __name__)
                sys.modules["account_balance_routes"] = routes
            import app
        _APP = app
    return _APP


class DashboardTests(PathsFixture):
    def setUp(self):
        super().setUp()
        self.app = load_app()
        patcher = mock.patch.object(self.app, "get_db", side_effect=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.app.app.test_client()
        self.written = self.kernel_memory("她在阳台种了一盆藏红花。", layer="long-term")
        self.ref = f"kernel:{self.written['lineage_id']}"
        self.today = reader.list_current(self.kernel)[0]["created_at"][:10]

    def library(self, path="/api/memories/library/index"):
        return self.client.get(path).get_json()

    def test_switch_off_library_uses_post_refs_and_no_kernel(self):
        ids = [e["id"] for e in self.library()["entries"]]
        self.assertIn("post:40", ids)
        self.assertNotIn(self.ref, ids)
        self.assertEqual(self.client.get("/api/memories/library/entry/post:40").get_json()["id"], "post:40")
        self.assertEqual(self.client.get("/api/memories/library/entry/40").get_json()["id"], "post:40")
        self.assertEqual(self.client.get(f"/api/memories/library/entry/{self.ref}").status_code, 404)

    def test_switch_on_library_index_full_detail_and_search(self):
        set_flag(self.posts, "1")
        for path in ("/api/memories/library/index", "/api/memories/library"):
            ids = [e["id"] for e in self.library(path)["entries"]]
            self.assertIn(self.ref, ids, path)
            self.assertNotIn("post:40", ids, path)  # legacy memory post: Kernel owns memories now
            self.assertIn("post:41", ids, path)  # DIARY stays in posts
        detail = self.client.get(f"/api/memories/library/entry/{self.ref}").get_json()
        self.assertEqual(detail, {"id": self.ref, "content": "她在阳台种了一盆藏红花。"})
        self.assertEqual(self.client.get("/api/memories/library/entry/post:40").status_code, 404)
        found = self.client.get("/api/memories/library/search?q=藏红花").get_json()["results"]
        self.assertEqual([r["id"] for r in found], [self.ref])

    def test_switch_on_pending_fallback_is_listed_and_openable(self):
        set_flag(self.posts, "1")
        post_id = self.fallback("她把围巾落在了电影院。")
        ids = [e["id"] for e in self.library()["entries"]]
        self.assertIn(f"post:{post_id}", ids)
        self.assertEqual(self.client.get(f"/api/memories/library/entry/post:{post_id}").status_code, 200)

    def test_switch_on_summary_calendar_and_posts_list(self):
        set_flag(self.posts, "1")
        summary = self.client.get("/api/posts/summary").get_json()
        texts = [i["text"] for s in summary["sections"] for i in s["items"]]
        self.assertIn("她在阳台种了一盆藏红花。", texts)
        self.assertNotIn(LEGACY, texts)
        month = self.client.get(f"/api/posts/calendar?month={self.today[:7]}").get_json()
        self.assertTrue(month["days"][int(self.today[8:]) - 1]["hasMemory"])
        day = self.client.get(f"/api/posts/calendar/day?date={self.today}").get_json()["entries"]
        self.assertIn("她在阳台种了一盆藏红花。", [e["title"] for e in day])
        self.assertNotIn(LEGACY, [e["title"] for e in day])
        listed = self.client.get("/api/posts?type=MEMORY&layer=long-term&limit=500").get_json()["posts"]
        self.assertEqual([p["id"] for p in listed], [self.ref])
        everything = [p["id"] for p in self.client.get("/api/posts?limit=500").get_json()["posts"]]
        self.assertIn(41, everything)
        self.assertNotIn(40, everything)
        self.assertIn(self.ref, everything)

    def test_switch_off_dashboards_are_unchanged(self):
        summary = self.client.get("/api/posts/summary").get_json()
        self.assertIn(LEGACY[:120], [i["text"] for s in summary["sections"] for i in s["items"]])
        listed = [p["id"] for p in self.client.get("/api/posts?type=MEMORY&limit=500").get_json()["posts"]]
        self.assertIn(40, listed)
        self.assertNotIn(self.ref, listed)
