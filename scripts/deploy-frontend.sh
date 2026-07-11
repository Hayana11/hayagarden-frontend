#!/usr/bin/env bash
# Safe production deploy for HayaGarden frontend.
# Usage: sudo scripts/deploy-frontend.sh [expected-origin-main-sha]
set -Eeuo pipefail

ROOT="${FRONTEND_ROOT:-/opt/frontend}"
REMOTE="${DEPLOY_REMOTE:-origin}"
BRANCH="${DEPLOY_BRANCH:-main}"
EXPECTED_SHA="${1:-}"
PYTHON="${PYTHON_BIN:-/usr/bin/python3.11}"
SERVICES=(frontend frontend-gw)
LOCK_FILE="/var/lock/hayagarden-frontend-deploy.lock"
STATE_DIR="/var/lib/hayagarden"

fail() {
  echo "DEPLOY REFUSED: $*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || fail "run with sudo so service restart and rollback are reliable"
[[ -d "$ROOT/.git" ]] || fail "$ROOT is not a git checkout"
command -v git >/dev/null || fail "git is missing"
command -v flock >/dev/null || fail "flock is missing"
command -v curl >/dev/null || fail "curl is missing"
[[ -x "$PYTHON" ]] || fail "python runtime not found: $PYTHON"

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another deployment is already running"

cd "$ROOT"
git fetch --prune "$REMOTE"

dirty="$(git status --porcelain --untracked-files=all)"
if [[ -n "$dirty" ]]; then
  echo "$dirty" >&2
  fail "production worktree has local changes. Commit/recover them on a branch; never overwrite them."
fi

target_sha="$(git rev-parse --verify "$REMOTE/$BRANCH^{commit}")"
current_sha="$(git rev-parse --verify HEAD^{commit})"
if [[ -n "$EXPECTED_SHA" && "$EXPECTED_SHA" != "$target_sha" ]]; then
  fail "origin/main moved: expected $EXPECTED_SHA but fetched $target_sha"
fi
if ! git merge-base --is-ancestor "$current_sha" "$target_sha"; then
  echo "Production-only commits:" >&2
  git log --oneline "$target_sha..$current_sha" >&2 || true
  fail "production HEAD is not contained in origin/main. Recover those commits on a branch before deploying."
fi

staging="$(mktemp -d /tmp/hayagarden-release.XXXXXX)"
cleanup() {
  git worktree remove --force "$staging" >/dev/null 2>&1 || true
  rm -rf "$staging"
}
trap cleanup EXIT

git worktree add --detach "$staging" "$target_sha"
(
  cd "$staging"
  "$PYTHON" -m py_compile app.py gateway.py chat/context_continuity.py
  "$PYTHON" -m unittest discover -s tests -p 'test_context_continuity.py'
  bash -n scripts/deploy-frontend.sh
)

rollback() {
  echo "Health check failed; rolling back to $current_sha" >&2
  git checkout --detach "$current_sha"
  systemctl restart "${SERVICES[@]}"
}
trap rollback ERR

git checkout --detach "$target_sha"
systemctl restart "${SERVICES[@]}"
health_ok=0
for attempt in 1 2 3 4 5; do
  services_ok=1
  for service in "${SERVICES[@]}"; do
    if ! systemctl is-active --quiet "$service"; then
      services_ok=0
    fi
  done
  if [[ "$services_ok" -eq 1 ]] && curl --fail --silent --show-error --max-time 20 \
      http://127.0.0.1:5051/api/debug/wake_check >/dev/null; then
    health_ok=1
    break
  fi
  if [[ "$attempt" -lt 5 ]]; then
    echo "Health check attempt $attempt/5 failed; retrying in 3s..." >&2
    sleep 3
  fi
done
if [[ "$health_ok" -ne 1 ]]; then
  echo "Health check failed after 5 attempts." >&2
  false
fi

mkdir -p "$STATE_DIR"
printf '%s\n' "$target_sha" > "$STATE_DIR/DEPLOYED_SHA"
trap - ERR
echo "Deployed $target_sha from $REMOTE/$BRANCH"
