#!/usr/bin/env bash
# Read-only production git drift detector for cron (no commits, no pushes).
set -Eeuo pipefail

ROOT="${FRONTEND_ROOT:-/opt/frontend}"
LOG="${PRODUCTION_GIT_DRIFT_LOG:-/var/log/production-git-drift.log}"
REMOTE="${DEPLOY_REMOTE:-origin}"
BRANCH="${DEPLOY_BRANCH:-main}"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

[[ -d "$ROOT/.git" ]] || { log "ERROR: $ROOT is not a git checkout"; exit 1; }
cd "$ROOT"

issues=0

if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
  log "ALERT: detached HEAD at $(git rev-parse --short HEAD)"
  issues=1
fi

git fetch --prune "$REMOTE" >/dev/null 2>&1 || log "WARN: git fetch failed"

target_sha="$(git rev-parse --verify "$REMOTE/$BRANCH^{commit}" 2>/dev/null || true)"
current_sha="$(git rev-parse HEAD)"
if [[ -n "$target_sha" && "$current_sha" != "$target_sha" ]]; then
  if git merge-base --is-ancestor "$current_sha" "$target_sha" 2>/dev/null; then
    log "INFO: HEAD $current_sha is behind $REMOTE/$BRANCH ($target_sha)"
  elif git merge-base --is-ancestor "$target_sha" "$current_sha" 2>/dev/null; then
    log "ALERT: production-only commits ahead of $REMOTE/$BRANCH"
    git log --oneline "$target_sha..$current_sha" | while read -r line; do log "  $line"; done
    issues=1
  else
    log "ALERT: HEAD $current_sha diverged from $REMOTE/$BRANCH ($target_sha)"
    issues=1
  fi
fi

dirty="$(git status --porcelain --untracked-files=no -- . \
  ':(exclude)attachments.db' ':(exclude)attachments/**' \
  ':(exclude)client_errors.log' ':(exclude)static/uploads/**' \
  ':(exclude)memories.db.bak*')"
if [[ -n "$dirty" ]]; then
  log "ALERT: tracked worktree dirty (runtime paths excluded)"
  issues=1
fi

if [[ "$issues" -eq 0 ]]; then
  log "OK: HEAD=$current_sha remote=$target_sha clean"
fi

exit "$issues"
