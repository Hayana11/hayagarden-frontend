"""B-4 anti-bypass: no new posts reader of memory types outside the reviewed set.

Behaviour is proven by tests/test_memory_kernel_read*.py; this scan only keeps
a new module from quietly reading MEMORY / FACT / MISS out of posts, which
would miss every memory written after the cutover switch is turned on.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY_LITERAL = re.compile(r"""['"](MEMORY|FACT|MISS)['"]""")
POSTS_READ = re.compile(r"\bFROM\s+posts\b")

# Every production module that both reads posts and names a memory type, and why
# that is safe with memory.kernel_write on. Adding a module here is a review step.
REVIEWED = {
    "tools/memory_kernel_read.py": "the B-4 reader: pending fallback posts only",
    "tools/memory_tool.py": "the write router; search_memories is switch-gated",
    "tools/memory_kernel_import.py": "offline B-1 importer",
    "app.py": "dashboards and /api/posts are switch-gated; MEMORY is the POST default",
    "gateway.py": "recall is switch-gated",
    "chat/system_builder.py": "FACT / long-term / memo are switch-gated",
    "tools/memory_library.py": "switch-gated",
    "tools/dream_generator.py": "switch-gated",
    "tools/fact_extractor.py": "dedupe is switch-gated",
    "tools/cleaner.py": "enriches posts rows only; skips Kernel-owned posts",
    "tools/memory_cycle.py": "lifecycle of legacy posts rows only",
    "tools/memory_runtime_bridge.py": "posts-row receipts; MEMORY is the write default",
    "tools/title_enricher.py": "titles for posts rows only",
    "tools/regenerate_titles_ai.py": "titles for posts rows only (manual tool)",
    "tools/backfill_titles_quick.py": "titles for posts rows only (manual tool)",
}


def production_modules():
    paths = list(ROOT.glob("*.py"))
    for directory in ("tools", "chat", "wake"):
        paths.extend((ROOT / directory).rglob("*.py"))
    return sorted(paths)


class MemoryReadRoutingGuard(unittest.TestCase):
    def test_no_unreviewed_posts_reader_of_memory_types(self):
        found = set()
        for path in production_modules():
            text = path.read_text(encoding="utf-8")
            if POSTS_READ.search(text) and MEMORY_LITERAL.search(text):
                found.add(path.relative_to(ROOT).as_posix())
        self.assertEqual(sorted(found - set(REVIEWED)), [],
                         "route memory-type reads through tools/memory_kernel_read.py")

    def test_reviewed_switch_gated_modules_still_check_the_switch(self):
        for name, reason in REVIEWED.items():
            if "switch-gated" not in reason:
                continue
            with self.subTest(module=name):
                self.assertRegex((ROOT / name).read_text(encoding="utf-8"), r"cutover_posts_db|_memory_cutover")


if __name__ == "__main__":
    unittest.main()
