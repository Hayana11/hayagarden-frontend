"""Exact regression coverage for the shared CJK/ASCII token estimator."""
from __future__ import annotations

import math
import random
import unittest

from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
    (0x2E80, 0x2EFF),
    (0x3000, 0x303F),
    (0xFF00, 0xFFEF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
)


def old_reference(text):
    """Test-only copy of the pre-optimization estimator."""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in str(text):
        codepoint = ord(ch)
        if any(start <= codepoint <= end for start, end in _RANGES):
            cjk += 1
        else:
            other += 1
    return int(cjk + math.ceil(other / 4) if other else cjk)


class TokenEstimatorEquivalenceTests(unittest.TestCase):
    def assertEquivalent(self, value):
        self.assertEqual(
            old_reference(value),
            estimate_tokens_heuristic_cjk1_ascii4_v1(value),
            repr(value),
        )

    def test_every_range_boundary_and_adjacent_codepoint(self):
        for start, end in _RANGES:
            for codepoint in (start - 1, start, start + 1, end - 1, end, end + 1):
                with self.subTest(range=(hex(start), hex(end)), codepoint=hex(codepoint)):
                    self.assertEquivalent(chr(codepoint))

    def test_ascii_emoji_cjk_japanese_korean_and_fullwidth(self):
        for value in (
            'ordinary ASCII text',
            '😀🧪🚀',
            '中文简体繁體',
            '日本語かなカナ',
            '한국어 한글',
            '，。！「」　１２３',
            'A中B😀。Ć',
            '中A中BC',
        ):
            with self.subTest(value=value):
                self.assertEquivalent(value)

    def test_empty_and_non_string_semantics(self):
        for value in (None, '', 0, False, [], {}, 12345, b'ASCII bytes'):
            with self.subTest(value=repr(value)):
                self.assertEquivalent(value)

    def test_deterministic_random_mixed_unicode(self):
        rng = random.Random(20261106)
        codepoints = []
        while len(codepoints) < 4096:
            codepoint = rng.randrange(0x110000)
            if 0xD800 <= codepoint <= 0xDFFF:
                continue
            codepoints.append(codepoint)
        self.assertEquivalent(''.join(map(chr, codepoints)))


if __name__ == '__main__':
    unittest.main()
