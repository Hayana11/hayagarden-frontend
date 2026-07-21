#!/usr/bin/env python3
"""只读 CLI：打印一次 Internal State 影子快照（JSON）。

严格只读：
  - 所有原始表与 chat 库均以 SQLite ``mode=ro`` URI 打开；
  - DB 路径来自 ``MEMORIES_DB`` / ``--db``，不 import 旧模块取路径；
  - **默认不运行**旧 getter（``--legacy`` 才启用，且在隔离子进程 +
    临时 DB 副本上执行，绝不让 ensure_table 碰生产库）。

用法：
    python3 tools/internal_state_shadow.py              # 默认：无 legacy，pretty JSON
    python3 tools/internal_state_shadow.py --compact    # 单行 JSON（JSONL 友好）
    python3 tools/internal_state_shadow.py --legacy     # 隔离子进程旧 getter 对照
    python3 tools/internal_state_shadow.py --db PATH

cron（须先确认严格只读已修好；务必加 --compact）：
    tools/internal_state_shadow.py --compact >> /var/log/shadow_snapshots.jsonl
在“旧模块 import 可能写库”未彻底隔离前，**不建议部署 cron**。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state as ist


def get_db_readonly(db_path: str):
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Internal State Shadow Phase 0 — 只读快照 CLI')
    parser.add_argument(
        '--db', default=None,
        help=f'memories.db 路径（默认 MEMORIES_DB 或 {ist.DEFAULT_MEMORIES_DB}）')
    parser.add_argument(
        '--legacy', action='store_true',
        help='在隔离子进程+临时副本上跑旧 getter 对照（默认关闭）')
    parser.add_argument(
        '--no-legacy', action='store_true',
        help='显式跳过旧 getter（默认行为；保留兼容）')
    parser.add_argument(
        '--compact', action='store_true',
        help='单行 JSON（JSONL）；默认 pretty indent=2')
    args = parser.parse_args(argv)

    db_path = ist.memories_db_path(args.db)
    include_legacy = bool(args.legacy) and not args.no_legacy

    snap = ist.capture_shadow_snapshot(
        lambda: get_db_readonly(db_path),
        include_legacy=include_legacy,
        db_path=db_path,
    )
    print(ist.snapshot_to_json(snap, compact=args.compact))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
