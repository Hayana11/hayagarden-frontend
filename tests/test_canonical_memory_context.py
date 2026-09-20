from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

from chat.canonical_memory_context import (  # noqa: E402
    format_current_for_cold_once,
    load_canonical_memory_context,
)


class CanonicalMemoryContextTests(unittest.TestCase):
    def _archive(self, root: Path, *, persona: str, current: str, index: str) -> None:
        (root / 'identity').mkdir(parents=True)
        (root / 'recent').mkdir(parents=True)
        (root / 'identity' / 'persona.md').write_text(persona, encoding='utf-8')
        (root / 'recent' / 'current.md').write_text(current, encoding='utf-8')
        (root / 'MEMORY_INDEX.md').write_text(index, encoding='utf-8')

    def test_disabled_does_not_require_archive(self):
        snapshot = load_canonical_memory_context(
            enabled=False,
            root='/path/that/must/not/be/read',
        )
        self.assertEqual(snapshot.status, 'disabled')
        self.assertEqual(snapshot.current_text, '')
        self.assertEqual(snapshot.index_entries, ())

    def test_g2_empty_scaffold_produces_no_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._archive(
                root,
                persona='# Canonical Persona\n\nStatus: `EMPTY_G2_SLOT`\n\nDo not infer.',
                current='# Current\n\nStatus: `EMPTY`\n\nNo current capsule yet.',
                index='# Memory Archive Index\n\n## Current entries\n\n_None._',
            )
            snapshot = load_canonical_memory_context(enabled=True, root=root)

        self.assertEqual(snapshot.status, 'empty')
        self.assertEqual(snapshot.persona_text, '')
        self.assertEqual(snapshot.current_text, '')
        self.assertEqual(snapshot.index_entries, ())
        self.assertEqual(format_current_for_cold_once(snapshot), '')

    def test_populated_archive_injects_only_current_and_keeps_index_discovery_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._archive(
                root,
                persona='# Canonical Persona\n\nStatus: `active`\n\nPERSONA_CANONICAL',
                current='# Current\n\nStatus: `active`\n\n今天确认了上下文注入只走 cold-once。',
                index=(
                    '# Memory Archive Index\n\n'
                    '| ID | Type | Topic/Title | Status | Time | Sensitivity | Path |\n'
                    '|---|---|---|---|---|---|---|\n'
                    '| fact_123 | Fact | 换窗边界 | active | 2026-08-10 | private | facts/fact_123.md |\n'
                ),
            )
            snapshot = load_canonical_memory_context(enabled=True, root=root)
            payload = format_current_for_cold_once(snapshot)

        self.assertEqual(snapshot.status, 'ready')
        self.assertIn('PERSONA_CANONICAL', snapshot.persona_text)
        self.assertEqual(len(snapshot.index_entries), 1)
        self.assertIn('今天确认了上下文注入', payload)
        self.assertNotIn('PERSONA_CANONICAL', payload)
        self.assertNotIn('换窗边界', payload)
        self.assertIsNotNone(snapshot.current_sha256)

    def test_current_over_budget_fails_safe_without_truncating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._archive(
                root,
                persona='# Canonical Persona\n\nStatus: `EMPTY_G2_SLOT`',
                current='# Current\n\nStatus: `active`\n\n' + ('x' * (33 * 1024)),
                index='# Memory Archive Index\n\n## Current entries\n\n_None._',
            )
            snapshot = load_canonical_memory_context(enabled=True, root=root)

        self.assertEqual(snapshot.current_text, '')
        self.assertEqual(format_current_for_cold_once(snapshot), '')
        self.assertTrue(any('file_too_large:recent/current.md' in d for d in snapshot.diagnostics))

    def test_symlinked_current_is_rejected(self):
        if not hasattr(os, 'symlink'):
            self.skipTest('symlink unavailable')
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / 'identity').mkdir(parents=True)
            (root / 'recent').mkdir(parents=True)
            (root / 'identity' / 'persona.md').write_text(
                '# Canonical Persona\n\nStatus: `EMPTY_G2_SLOT`', encoding='utf-8',
            )
            (root / 'MEMORY_INDEX.md').write_text('# Memory Archive Index', encoding='utf-8')
            target = Path(outside) / 'current.md'
            target.write_text('# Current\n\nStatus: `active`\n\nSECRET_OUTSIDE', encoding='utf-8')
            try:
                (root / 'recent' / 'current.md').symlink_to(target)
            except OSError as exc:
                self.skipTest(f'symlink unavailable: {exc}')
            snapshot = load_canonical_memory_context(enabled=True, root=root)

        self.assertEqual(snapshot.current_text, '')
        self.assertNotIn('SECRET_OUTSIDE', format_current_for_cold_once(snapshot))
        self.assertIn('file_symlink:recent/current.md', snapshot.diagnostics)


if __name__ == '__main__':
    unittest.main()
