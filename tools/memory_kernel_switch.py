"""B-3 runtime switch: is the Memory Kernel the memory write target?

Kept dependency-free (sqlite only) so every caller can check it cheaply and
only import the Kernel stack when the switch is on. The value lives in the
runtime_config table of memories.db, shared by gateway, internal-mcp and the
capability proxy.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

FLAG_KEY = "memory.kernel_write"
_TRUE = {"1", "true", "yes", "on"}


def write_enabled(config_db: str | os.PathLike[str] | None) -> bool:
    """Any error or missing database means the legacy path."""
    # A plain path (not a file: URI) so sandboxed callers can recognise it;
    # checked first so a missing file is never created.
    if not config_db or not Path(config_db).is_file():
        return False
    try:
        conn = sqlite3.connect(str(config_db), timeout=3)
        try:
            row = conn.execute("SELECT value FROM runtime_config WHERE key=?", (FLAG_KEY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]).strip().lower() in _TRUE
