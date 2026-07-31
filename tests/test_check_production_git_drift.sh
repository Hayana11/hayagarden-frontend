#!/usr/bin/env bash
# Shell regressions for scripts/check-production-git-drift.sh
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/check-production-git-drift.sh"
TMP="$(mktemp -d)"
PASS=0
FAIL=0

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

assert_exit() {
  local name="$1" expected="$2"
  shift 2
  local log_file="$TMP/log-$$.txt"
  set +e
  PRODUCTION_GIT_DRIFT_LOG="$log_file" "$@" >/dev/null 2>&1
  local actual=$?
  set -e
  if [[ "$actual" -eq "$expected" ]]; then
    echo "PASS: $name (exit $actual)"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name (expected exit $expected, got $actual)" >&2
    [[ -f "$log_file" ]] && sed 's/^/  /' "$log_file" >&2 || true
    FAIL=$((FAIL + 1))
  fi
}

setup_deployed_repo() {
  local name="$1"
  local repo="$TMP/$name"
  local bare="$TMP/${name}-bare"
  git init -q --bare "$bare"
  git clone -q "file://$bare" "$repo"
  cd "$repo"
  git config user.email test@example.com
  git config user.name test
  echo content > README.md
  git add README.md
  git commit -q -m init
  git push -q origin HEAD:main
  git branch -M main
  local sha
  sha="$(git rev-parse HEAD)"
  mkdir -p "$TMP/state-$name"
  printf '%s\n' "$sha" >"$TMP/state-$name/DEPLOYED_SHA"
  git checkout -q --detach "$sha"
  cd "$ROOT"
  REPO_PATH="$repo"
  DEPLOYED_PATH="$TMP/state-$name/DEPLOYED_SHA"
  REMOTE_URL="file://$bare"
  SHA="$sha"
}

echo "Running production git drift shell regressions..."

# detached + three SHAs aligned -> exit 0
setup_deployed_repo healthy-detached
assert_exit "detached aligned SHAs" 0 \
  env FRONTEND_ROOT="$REPO_PATH" DEPLOYED_SHA_FILE="$DEPLOYED_PATH" REMOTE=origin BRANCH=main \
  bash "$SCRIPT"

# detached + HEAD ahead of main -> exit 1
setup_deployed_repo ahead-detached
cd "$REPO_PATH"
echo drift >> README.md
git add README.md
git commit -q -m ahead
cd "$ROOT"
assert_exit "detached HEAD ahead of origin/main" 1 \
  env FRONTEND_ROOT="$REPO_PATH" DEPLOYED_SHA_FILE="$DEPLOYED_PATH" REMOTE=origin BRANCH=main \
  bash "$SCRIPT"

# DEPLOYED_SHA mismatch -> exit 1
setup_deployed_repo deployed-mismatch
printf '0000000000000000000000000000000000000001\n' >"$DEPLOYED_PATH"
assert_exit "DEPLOYED_SHA mismatch" 1 \
  env FRONTEND_ROOT="$REPO_PATH" DEPLOYED_SHA_FILE="$DEPLOYED_PATH" REMOTE=origin BRANCH=main \
  bash "$SCRIPT"

# fetch failure -> exit 1
setup_deployed_repo fetch-fail
git -C "$REPO_PATH" remote set-url origin https://invalid.invalid/repo.git
assert_exit "fetch failure" 1 \
  env FRONTEND_ROOT="$REPO_PATH" DEPLOYED_SHA_FILE="$DEPLOYED_PATH" REMOTE=origin BRANCH=main \
  bash "$SCRIPT"

# SQLite WAL/SHM companions are runtime state; an additional unknown file still fails.
setup_deployed_repo sqlite-runtime-companions
: >"$REPO_PATH/memories.db-shm"
: >"$REPO_PATH/memories.db-wal"
assert_exit "SQLite runtime companions only" 0 \
  env FRONTEND_ROOT="$REPO_PATH" DEPLOYED_SHA_FILE="$DEPLOYED_PATH" DEPLOY_REMOTE=origin DEPLOY_BRANCH=main \
  bash "$SCRIPT"
echo stray >"$REPO_PATH/UNEXPECTED.txt"
assert_exit "SQLite companions plus unexpected untracked file" 1 \
  env FRONTEND_ROOT="$REPO_PATH" DEPLOYED_SHA_FILE="$DEPLOYED_PATH" DEPLOY_REMOTE=origin DEPLOY_BRANCH=main \
  bash "$SCRIPT"

echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
