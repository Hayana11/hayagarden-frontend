"""The Memory Kernel cutover switch: runtime_config ``memory.kernel_write``.

Despite the key's name it is no longer a write-only switch. Since B-4 it moves
memory types (MEMORY / FACT / MISS) to the Kernel in both directions at once:
writes go through memory_tool.save_memory into the Kernel, and every read goes
through memory_kernel_read. One flip, so nothing is ever written to the Kernel
while readers still look only at posts. It must stay off until B-4 is deployed
and the owner confirms; ``cutover_enabled`` is the name readers use.

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
DEFAULT_CONFIG_DB = "/opt/frontend/memories.db"
# With the switch on, new memories must go through memory_write (Kernel first).
# Ombre's own creation tools would bypass the Kernel; trace edits stay allowed
# and are ingested back as Kernel revisions.
KERNEL_OWNED_BRAIN_WRITES = ("mcp__brain__hold", "mcp__brain__grow")


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
    except Exception:  # unreadable switch (locked, missing table, sandboxed caller) = legacy
        return False
    return bool(row) and str(row[0]).strip().lower() in _TRUE


def cutover_enabled(config_db: str | os.PathLike[str] | None) -> bool:
    """Do memory reads come from the Kernel? Same flag, same fail-closed rule as writes."""
    return write_enabled(config_db)


def kernel_owned_brain_writes(env=None) -> tuple[str, ...]:
    """Ombre tools the resident CC must not get while the Kernel owns writes."""
    source = env if env is not None else os.environ
    config_db = source.get("HAYAGARDEN_CONFIG_DB_PATH") or DEFAULT_CONFIG_DB
    return KERNEL_OWNED_BRAIN_WRITES if write_enabled(config_db) else ()
