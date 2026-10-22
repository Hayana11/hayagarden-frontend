"""B-3 anti-bypass: memory types are written only through memory_tool.save_memory."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY_TYPES = ("MEMORY", "FACT", "MISS")
# Direct posts inserts that exist for non-memory content, with the type they write.
ALLOWED_DIRECT_INSERTS = {
    "tools/product_handlers.py": "DIARY",
    "wake/executor.py": "DIARY",
}
_INSERT = re.compile(r"insert\s+(?:or\s+\w+\s+)?into\s+posts\b", re.I)


def production_sources():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "scratch/", "migrations/", "app/", "node_modules/")) or "/." in rel:
            continue
        yield rel, path.read_text(encoding="utf-8", errors="replace")


class MemoryWriteRoutingGuard(unittest.TestCase):
    def test_no_production_code_inserts_memory_types_into_posts_directly(self):
        for rel, text in production_sources():
            if rel == "tools/memory_tool.py" or not _INSERT.search(text):
                continue
            with self.subTest(path=rel):
                self.assertIn(rel, ALLOWED_DIRECT_INSERTS,
                              "write posts through memory_tool.save_memory instead of a raw INSERT")
                for match in _INSERT.finditer(text):
                    statement = text[match.start():match.start() + 400]
                    for kind in MEMORY_TYPES:
                        self.assertNotRegex(statement, rf"['\"]{kind}['\"]",
                                            f"{rel} inserts a {kind} row directly")
                    self.assertIn(ALLOWED_DIRECT_INSERTS[rel], statement)

    def test_private_legacy_writer_is_only_used_by_the_router(self):
        for rel, text in production_sources():
            if rel == "tools/memory_tool.py":
                continue
            with self.subTest(path=rel):
                self.assertNotIn("_legacy_save", text)
                self.assertNotIn("_kernel_save", text)

    def test_router_covers_exactly_the_b1_import_types(self):
        from tools import memory_kernel_import, memory_kernel_store, memory_tool

        self.assertEqual(tuple(memory_tool.MEMORY_TYPES), MEMORY_TYPES)
        self.assertEqual(tuple(memory_kernel_import.LEGACY_POST_TYPES), MEMORY_TYPES)
        self.assertEqual(tuple(memory_kernel_store.MEMORY_TYPES), MEMORY_TYPES)


if __name__ == "__main__":
    unittest.main()
