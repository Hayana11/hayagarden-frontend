#!/usr/bin/env bash
# Activate the versioned Internal MCP unit only after an exact deploy.
# Usage: sudo bash scripts/activate-internal-mcp.sh <expected-deployed-sha>
set -Eeuo pipefail

ROOT="${FRONTEND_ROOT:-/opt/frontend}"
EXPECTED_SHA="${1:-}"
STATE_DIR="${INTERNAL_MCP_STATE_DIR:-/var/lib/hayagarden}"
DEPLOYED_SHA_FILE="${STATE_DIR}/DEPLOYED_SHA"
ACTIVATED_SHA_FILE="${INTERNAL_MCP_ACTIVATED_SHA_FILE:-${STATE_DIR}/internal-mcp-activated-sha}"
UNIT_NAME="internal-mcp.service"
SYSTEMD_DIR="${INTERNAL_MCP_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_PATH="${SYSTEMD_DIR}/${UNIT_NAME}"
UNIT_SOURCE="${INTERNAL_MCP_UNIT_SOURCE:-${ROOT}/deploy/systemd/${UNIT_NAME}}"
READINESS="${INTERNAL_MCP_READINESS_BIN:-${ROOT}/scripts/check-internal-mcp-readiness.mjs}"
SYSTEMCTL="${SYSTEMCTL_BIN:-systemctl}"
GIT="${GIT_BIN:-git}"
NODE="${NODE_BIN:-node}"

fail() {
  echo "INTERNAL MCP ACTIVATION REFUSED: $*" >&2
  exit 1
}

[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "root is required"
[[ -n "$EXPECTED_SHA" ]] || fail "expected deployed SHA is required"
[[ -d "$ROOT/.git" ]] || fail "frontend root is not a git checkout"
[[ -f "$DEPLOYED_SHA_FILE" ]] || fail "DEPLOYED_SHA is missing"
[[ -f "$ROOT/internal-mcp-server.js" ]] || fail "Internal MCP server is missing"
[[ -f "$UNIT_SOURCE" ]] || fail "versioned unit is missing"
[[ -f "$READINESS" ]] || fail "readiness check is missing"

HEAD_SHA="$("$GIT" -C "$ROOT" rev-parse --verify HEAD^{commit})"
[[ "$HEAD_SHA" == "$EXPECTED_SHA" ]] || fail "git HEAD mismatch: got $HEAD_SHA expected $EXPECTED_SHA"
DEPLOYED_SHA="$(tr -d '\r\n' < "$DEPLOYED_SHA_FILE")"
[[ "$DEPLOYED_SHA" == "$EXPECTED_SHA" ]] || fail "DEPLOYED_SHA mismatch: got $DEPLOYED_SHA expected $EXPECTED_SHA"

DIRTY="$("$GIT" -C "$ROOT" status --porcelain --untracked-files=all)"
[[ -z "$DIRTY" ]] || {
  printf '%s\n' "$DIRTY" >&2
  fail "production worktree is dirty"
}

for required in \
  'WorkingDirectory=/opt/frontend' \
  'ExecStart=/usr/bin/node /opt/frontend/internal-mcp-server.js' \
  'Environment=INTERNAL_MCP_PORT=3101' \
  'Environment=TODO_INTERNAL_DB_PATH=/opt/frontend/memories.db' \
  'Environment=UH_A0_REPO_ROOT=/opt/frontend' \
  'Environment=NODE_ENV=production' \
  'Restart=always' \
  'StandardOutput=append:/opt/frontend/internal-mcp.log' \
  'StandardError=append:/opt/frontend/internal-mcp.log'
do
  grep -Fqx "$required" "$UNIT_SOURCE" || fail "unit contract missing: $required"
done

mkdir -p "$SYSTEMD_DIR"
BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/internal-mcp-activation.XXXXXX")"
OLD_UNIT="$BACKUP_DIR/$UNIT_NAME"
OLD_EXISTS=0
OLD_ENABLED="disabled"
OLD_ACTIVE="inactive"
OLD_ACTIVATED_SHA=""
OLD_ACTIVATED_SHA_EXISTS=0
UNIT_REPLACED=0

cleanup() {
  rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT

if [[ -f "$ACTIVATED_SHA_FILE" ]]; then
  OLD_ACTIVATED_SHA_EXISTS=1
  cp -p "$ACTIVATED_SHA_FILE" "$BACKUP_DIR/activated-sha"
  OLD_ACTIVATED_SHA="$(tr -d '\r\n' < "$ACTIVATED_SHA_FILE")"
fi

if [[ -f "$UNIT_PATH" ]]; then
  OLD_EXISTS=1
  cp -p "$UNIT_PATH" "$OLD_UNIT"
  OLD_ENABLED="$("$SYSTEMCTL" is-enabled "$UNIT_NAME" 2>/dev/null || true)"
  OLD_ACTIVE="$("$SYSTEMCTL" is-active "$UNIT_NAME" 2>/dev/null || true)"
fi

run_readiness() {
  "$NODE" "$READINESS"
}

if [[ "$OLD_EXISTS" -eq 1 ]] \
  && cmp -s "$UNIT_SOURCE" "$UNIT_PATH" \
  && [[ "$OLD_ENABLED" == enabled* ]] \
  && [[ "$OLD_ACTIVE" == active ]] \
  && [[ "$OLD_ACTIVATED_SHA" == "$EXPECTED_SHA" ]] \
  && run_readiness
then
  echo "Internal MCP activation already ready for $EXPECTED_SHA"
  exit 0
fi

rollback() {
  trap - ERR
  if [[ "$UNIT_REPLACED" -ne 1 ]]; then
    return
  fi

  "$SYSTEMCTL" stop "$UNIT_NAME" >/dev/null 2>&1 || true
  if [[ "$OLD_EXISTS" -eq 1 ]]; then
    cp -p "$OLD_UNIT" "$UNIT_PATH"
    "$SYSTEMCTL" daemon-reload >/dev/null 2>&1 || true
    case "$OLD_ENABLED" in
      enabled*) "$SYSTEMCTL" enable "$UNIT_NAME" >/dev/null 2>&1 || true ;;
      masked*) "$SYSTEMCTL" mask "$UNIT_NAME" >/dev/null 2>&1 || true ;;
      *) "$SYSTEMCTL" disable "$UNIT_NAME" >/dev/null 2>&1 || true ;;
    esac
    if [[ "$OLD_ACTIVE" == active ]]; then
      "$SYSTEMCTL" start "$UNIT_NAME" >/dev/null 2>&1 || true
    else
      "$SYSTEMCTL" stop "$UNIT_NAME" >/dev/null 2>&1 || true
    fi
  else
    "$SYSTEMCTL" disable "$UNIT_NAME" >/dev/null 2>&1 || true
    rm -f "$UNIT_PATH"
    "$SYSTEMCTL" daemon-reload >/dev/null 2>&1 || true
  fi

  if [[ "$OLD_EXISTS" -eq 1 && "$OLD_ACTIVATED_SHA_EXISTS" -eq 1 ]]; then
    cp -p "$BACKUP_DIR/activated-sha" "$ACTIVATED_SHA_FILE"
  else
    rm -f "$ACTIVATED_SHA_FILE"
  fi
}
trap rollback ERR

install -m 0644 "$UNIT_SOURCE" "$UNIT_PATH"
UNIT_REPLACED=1
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable "$UNIT_NAME"
"$SYSTEMCTL" start "$UNIT_NAME"
run_readiness

MARKER_TMP="$BACKUP_DIR/activated-sha.new"
printf '%s\n' "$EXPECTED_SHA" > "$MARKER_TMP"
mv -f "$MARKER_TMP" "$ACTIVATED_SHA_FILE"

echo "Internal MCP activated for $EXPECTED_SHA"
