#!/usr/bin/env python3
"""只读 CLI：打印一次 Internal State 影子快照（JSON）。

不写任何业务数据库。chat_messages/wake_log 通过 SQLite 只读 URI 打开；
旧模块 getter 仅调用只读函数。

用法：
    python3 tools/internal_state_shadow.py            # 完整快照
    python3 tools/internal_state_shadow.py --no-legacy  # 跳过旧 getter 对照
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state as ist

DB_PATH = '/opt/frontend/memories.db'


def get_db_readonly():
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    include_legacy = '--no-legacy' not in sys.argv
    snap = ist.capture_shadow_snapshot(get_db_readonly,
                                       include_legacy=include_legacy)
    print(ist.snapshot_to_json(snap))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
