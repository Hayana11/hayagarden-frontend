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
