#!/usr/bin/env bash
# Verify the managed native Claude Code runtime. Never install a runtime here.
# Usage: bash scripts/ensure-claude-runtime.sh [repo-root]
set -Eeuo pipefail

ROOT="$(cd "${1:-$(dirname "$0")/..}" && pwd)"
PYTHON="${PYTHON_BIN:-/usr/bin/python3.11}"

fail() {
  echo "CLAUDE RUNTIME REFUSED: $*" >&2
  exit 1
}

[[ -x "$PYTHON" ]] || fail "Python runtime not found: $PYTHON"
cd "$ROOT"
"$PYTHON" - <<'PY'
from chat.cc_runtime import (
    MINIMUM_CLAUDE_CODE_VERSION,
    active_claude_binary,
    require_managed_claude_runtime,
)

version = require_managed_claude_runtime()
binary = active_claude_binary()
print(
    'claude managed native runtime ok:',
    version,
    '(minimum', MINIMUM_CLAUDE_CODE_VERSION + ')',
    '(' + str(binary) + ')',
)
PY
