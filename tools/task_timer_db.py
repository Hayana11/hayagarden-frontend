"""Canonical runtime path resolution for Task Timer's SQLite database.

The runtime database is operational state, not a source artifact. All API,
feedback, and capability-proxy consumers use this resolver so the path cannot
silently split between a repository checkout and an external runtime store.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

TASK_TIMER_COMMANDS_DB_ENV = "TASK_TIMER_COMMANDS_DB_PATH"
DEFAULT_TASK_TIMER_COMMANDS_DB_PATH = "/var/lib/hayagarden/commands.db"


def resolve_task_timer_commands_db_path(
    *,
    env: Mapping[str, str] | None = None,
    explicit_path: str | os.PathLike[str] | None = None,
) -> str:
    """Return the one canonical Task Timer DB path.

    A configured environment value is authoritative. explicit_path is
    retained for adapters that receive the already-resolved proxy value.
    With neither present, use the external runtime default, never a repo path.
    """
    environ = env if env is not None else os.environ
    configured = str(environ.get(TASK_TIMER_COMMANDS_DB_ENV) or "").strip()
    if configured:
        return str(Path(configured).expanduser())
    if explicit_path is not None and str(explicit_path).strip():
        return str(Path(str(explicit_path).strip()).expanduser())
    return DEFAULT_TASK_TIMER_COMMANDS_DB_PATH
