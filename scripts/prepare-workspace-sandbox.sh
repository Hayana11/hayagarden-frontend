#!/usr/bin/env bash
# Prepare /opt/workspace sandbox for gateway workspace tools (PR 1).
set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/opt/workspace}"
SANDBOX_USER="${SANDBOX_USER:-wsandbox}"
WORKSPACE_GROUP="${WORKSPACE_GROUP:-workspace}"
GATEWAY_USER="${GATEWAY_USER:-}"

echo "==> Creating workspace directories under ${WORKSPACE_ROOT}"
mkdir -p \
  "${WORKSPACE_ROOT}/projects" \
  "${WORKSPACE_ROOT}/artifacts/tool_outputs" \
  "${WORKSPACE_ROOT}/apps" \
  "${WORKSPACE_ROOT}/tools" \
  "${WORKSPACE_ROOT}/.jobs/events"

if ! getent group "${WORKSPACE_GROUP}" >/dev/null; then
  echo "==> Creating workspace group ${WORKSPACE_GROUP}"
  groupadd --system "${WORKSPACE_GROUP}" || true
fi

if ! id "${SANDBOX_USER}" &>/dev/null; then
  echo "==> Creating sandbox user ${SANDBOX_USER}"
  useradd --system --home "/home/${SANDBOX_USER}" --shell /bin/bash \
    --gid "${WORKSPACE_GROUP}" "${SANDBOX_USER}" || true
else
  usermod -g "${WORKSPACE_GROUP}" "${SANDBOX_USER}" 2>/dev/null || true
  usermod -aG "${WORKSPACE_GROUP}" "${SANDBOX_USER}" 2>/dev/null || true
fi

if [ -z "${GATEWAY_USER}" ]; then
  GATEWAY_USER="$(systemctl show -p User --value frontend-gw.service 2>/dev/null || true)"
fi
if [ -n "${GATEWAY_USER}" ] && id "${GATEWAY_USER}" &>/dev/null; then
  echo "==> Adding gateway user ${GATEWAY_USER} to ${WORKSPACE_GROUP}"
  usermod -aG "${WORKSPACE_GROUP}" "${GATEWAY_USER}" 2>/dev/null || true
fi

echo "==> Setting ownership and permissions on ${WORKSPACE_ROOT}"
chown -R "${SANDBOX_USER}:${WORKSPACE_GROUP}" "${WORKSPACE_ROOT}"
# setgid so new files inherit workspace group; 2770 = owner+group only
find "${WORKSPACE_ROOT}" -type d -exec chmod 2770 {} +
find "${WORKSPACE_ROOT}" -type f -exec chmod 660 {} + 2>/dev/null || true
chmod 2770 "${WORKSPACE_ROOT}"
chmod 2770 "${WORKSPACE_ROOT}/.jobs" || true
chmod 2770 "${WORKSPACE_ROOT}/.jobs/events" || true

# Tighten default umask for sandbox user (group-only writes)
if [ -f "/home/${SANDBOX_USER}/.profile" ]; then
  if ! grep -q 'umask 007' "/home/${SANDBOX_USER}/.profile" 2>/dev/null; then
    echo 'umask 007' >> "/home/${SANDBOX_USER}/.profile"
  fi
else
  install -d -m 750 -o "${SANDBOX_USER}" -g "${WORKSPACE_GROUP}" "/home/${SANDBOX_USER}"
  echo 'umask 007' > "/home/${SANDBOX_USER}/.profile"
  chown "${SANDBOX_USER}:${WORKSPACE_GROUP}" "/home/${SANDBOX_USER}/.profile"
fi

echo "==> Workspace sandbox ready"
echo "    root:  ${WORKSPACE_ROOT} (2770, setgid)"
echo "    user:  ${SANDBOX_USER}"
echo "    group: ${WORKSPACE_GROUP}"
echo "    gateway: ${GATEWAY_USER:-<not detected>}"
echo "    EXEC_GROUP=${WORKSPACE_GROUP} (subprocess group=EXEC_GROUP)"
echo "    EXEC_ENABLED defaults to 0 (shell_exec + git-mode ws_diff disabled)"
