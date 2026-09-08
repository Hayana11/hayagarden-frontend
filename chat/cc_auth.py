"""Minimal Claude Code OAuth token reader for background surfaces."""
from __future__ import annotations

from pathlib import Path

_ENV_PATH = Path('/opt/frontend/.env')


def read_cc_oauth_token(env_path: str | Path | None = None) -> str:
    """Read the existing deployment token without logging or retaining it."""
    path = Path(env_path) if env_path is not None else _ENV_PATH
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ''
