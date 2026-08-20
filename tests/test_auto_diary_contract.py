"""Contract test for the automatic Diary persistence boundary."""
from __future__ import annotations

import sys
import unittest
from unittest import mock

import auto_diary


class AutoDiaryContractTests(unittest.TestCase):
    def test_save_diary_keeps_memory_tool_entrypoint(self):
        memory_tool = mock.Mock()
        with mock.patch.dict(sys.modules, {"memory_tool": memory_tool}):
            auto_diary.save_diary("今天的自动日记")

        memory_tool.save_memory.assert_called_once_with(
            "今天的自动日记",
            type="DIARY",
            layer="recent",
        )


if __name__ == "__main__":
    unittest.main()
