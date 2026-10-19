#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${FRONTEND_ROOT:-/opt/frontend}"
PYTHON="${PYTHON_BIN:-/usr/bin/python3.11}"
RECEIPT_DB="${MEMORY_RUNTIME_RECEIPT_DB_PATH:-/var/lib/hayagarden/memory-runtime-receipts.db}"
SERVICES=(frontend frontend-gw)

fail() {
  echo "MEMORY RECEIPT PREFLIGHT REFUSED: $*" >&2
  exit 1
}

[[ -x "$PYTHON" ]] || fail "python runtime is unavailable: $PYTHON"
[[ -f "$ROOT/tools/memory_runtime_bridge.py" ]] || fail "bridge is unavailable under $ROOT"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
command -v runuser >/dev/null 2>&1 || fail "runuser is unavailable"

cd "$ROOT"
declare -A checked_users=()

for service in "${SERVICES[@]}"; do
  load_state="$(systemctl show "$service" -p LoadState --value 2>/dev/null || true)"
  [[ "$load_state" == "loaded" ]] || fail "$service is not loaded"
  active_state="$(systemctl show "$service" -p ActiveState --value 2>/dev/null || true)"
  [[ "$active_state" == "active" ]] || fail "$service is not active: $active_state"

  runtime_user="$(systemctl show "$service" -p User --value 2>/dev/null || true)"
  runtime_user="${runtime_user:-root}"
  main_pid="$(systemctl show "$service" -p MainPID --value 2>/dev/null || true)"
  [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || fail "$service has no live MainPID"
  expected_uid="$(id -u "$runtime_user" 2>/dev/null || true)"
  [[ "$expected_uid" =~ ^[0-9]+$ ]] || fail "cannot resolve service user: $runtime_user"
  actual_uid="$(stat -c '%u' "/proc/$main_pid" 2>/dev/null || true)"
  [[ "$actual_uid" == "$expected_uid" ]] || fail "$service MainPID $main_pid runs as uid $actual_uid, expected $expected_uid"
  [[ -n "${checked_users[$runtime_user]:-}" ]] && continue

  probe='import json
from tools.memory_runtime_bridge import verify_receipt_store
result = verify_receipt_store()
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result.get("status") == "VERIFIED" else 1)'

  if [[ "$runtime_user" == "root" ]]; then
    output="$(
      MEMORY_RUNTIME_RECEIPT_DB_PATH="$RECEIPT_DB"       PYTHONPATH="$ROOT"       "$PYTHON" -c "$probe"
    )" || fail "$service user $runtime_user failed: $output"
  else
    output="$(
      runuser -u "$runtime_user" --         env MEMORY_RUNTIME_RECEIPT_DB_PATH="$RECEIPT_DB"         PYTHONPATH="$ROOT"         "$PYTHON" -c "$probe"
    )" || fail "$service user $runtime_user failed: $output"
  fi

  printf 'receipt preflight service=%s user=%s %s\n'     "$service" "$runtime_user" "$output"
  checked_users[$runtime_user]=1
done
