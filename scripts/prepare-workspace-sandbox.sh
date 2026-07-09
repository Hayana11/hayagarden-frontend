#!/usr/bin/env bash
# Prepare /opt/workspace sandbox for gateway workspace tools (PR 1).
set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/opt/workspace}"
SANDBOX_USER="${SANDBOX_USER:-wsandbox}"

echo "==> Creating workspace directories under ${WORKSPACE_ROOT}"
mkdir -p \
  "${WORKSPACE_ROOT}/projects" \
  "${WORKSPACE_ROOT}/artifacts/tool_outputs" \
  "${WORKSPACE_ROOT}/apps" \
  "${WORKSPACE_ROOT}/tools" \
  "${WORKSPACE_ROOT}/.jobs"

if ! id "${SANDBOX_USER}" &>/dev/null; then
  echo "==> Creating sandbox user ${SANDBOX_USER}"
  useradd --system --home "/home/${SANDBOX_USER}" --shell /bin/bash "${SANDBOX_USER}" || true
fi

echo "==> Setting ownership on ${WORKSPACE_ROOT}"
chown -R "${SANDBOX_USER}:${SANDBOX_USER}" "${WORKSPACE_ROOT}"
chmod 755 "${WORKSPACE_ROOT}"
chmod 750 "${WORKSPACE_ROOT}/.jobs" || true

echo "==> Workspace sandbox ready"
echo "    root: ${WORKSPACE_ROOT}"
echo "    user: ${SANDBOX_USER}"
echo "    EXEC_ENABLED defaults to 0 (shell_exec returns exec_disabled)"
