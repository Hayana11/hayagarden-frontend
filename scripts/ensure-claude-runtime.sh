#!/usr/bin/env bash
# Install / verify the project-local pinned Claude Code runtime.
# Usage: bash scripts/ensure-claude-runtime.sh [repo-root]
set -Eeuo pipefail

ROOT="$(cd "${1:-$(dirname "$0")/..}" && pwd)"
EXPECTED="${EXPECTED_CLAUDE_CODE_VERSION:-2.1.220}"
SPEC="@anthropic-ai/claude-code@${EXPECTED}"
PREFIX="$ROOT/.claude-runtime"
BIN="$PREFIX/node_modules/@anthropic-ai/claude-code/bin/claude.exe"

fail() {
  echo "CLAUDE RUNTIME REFUSED: $*" >&2
  exit 1
}

command -v npm >/dev/null || fail "npm is required to install $SPEC"
command -v node >/dev/null || fail "node is required to run $SPEC"

mkdir -p "$PREFIX"
# Exact pin; do not update unrelated root package.json dependencies.
npm install --prefix "$PREFIX" --no-save --no-audit --no-fund --prefer-offline "$SPEC" \
  || npm install --prefix "$PREFIX" --no-save --no-audit --no-fund "$SPEC"

[[ -x "$BIN" || -f "$BIN" ]] || fail "pinned binary missing after install: $BIN"
chmod +x "$BIN" 2>/dev/null || true

version_text="$("$BIN" --version 2>&1 | head -n 1 || true)"
case "$version_text" in
  "${EXPECTED}"*) ;;
  *) fail "expected ${EXPECTED}, got: ${version_text}" ;;
esac

echo "claude runtime ok: ${version_text} (${BIN})"
