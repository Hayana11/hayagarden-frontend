#!/usr/bin/env bash
# Source-only audited production overlay for two fixed tracked files.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  printf '%s\n' 'PROTECTED_OVERLAY_SOURCE_ONLY' >&2
  exit 1
fi

readonly PROTECTED_OVERLAY_PATH_A='artifacts/treegpt-cache-probe-baseline.json'
readonly PROTECTED_OVERLAY_PATH_B='artifacts/treegpt-cache-probe-live.json'

_protected_overlay_error() {
  printf '%s\n' "$1" >&2
  return 1
}
_protected_overlay_sha() {
  [[ "$1" =~ ^[0-9a-fA-F]{40}$ ]] || _protected_overlay_error 'PROTECTED_OVERLAY_ARGUMENTS'
}

_protected_overlay_repo() {
  git -C "$1" rev-parse --git-dir >/dev/null 2>&1 ||
    _protected_overlay_error 'PROTECTED_OVERLAY_REPOSITORY'
}

_protected_overlay_blob() {
  local root="$1"
  local revision="$2"
  local path="$3"
  local spec="${revision}:${path}"
  local kind
  kind="$(git -C "$root" cat-file -t "$spec" 2>/dev/null || true)"
  [[ "$kind" == 'blob' ]] || return 1
  git -C "$root" rev-parse "$spec" 2>/dev/null
}

_protected_overlay_current_path() {
  local root="$1"
  local current_sha="$2"
  local path="$3"
  local status
  status="$(git -C "$root" status --porcelain=v1 --untracked-files=all -- "$path" 2>/dev/null || true)"
  [[ "$status" == " D $path" ]] || return 1
  [[ ! -e "$root/$path" && ! -L "$root/$path" ]] || return 1
  [[ "$(git -C "$root" diff --cached --name-status -- "$path")" == '' ]] || return 1
  _protected_overlay_blob "$root" "$current_sha" "$path" >/dev/null
}

protected_overlay_validate_current() {
  [[ "$#" -eq 2 ]] || { _protected_overlay_error 'PROTECTED_OVERLAY_ARGUMENTS'; return 1; }
  local root="$1"
  local current_sha="$2"
  _protected_overlay_repo "$root" || return
  _protected_overlay_sha "$current_sha" || return
  _protected_overlay_current_path "$root" "$current_sha" "$PROTECTED_OVERLAY_PATH_A" &&
    _protected_overlay_current_path "$root" "$current_sha" "$PROTECTED_OVERLAY_PATH_B" ||
    _protected_overlay_error 'PROTECTED_OVERLAY_STATE_MISMATCH'
}

protected_overlay_validate_target() {
  [[ "$#" -eq 3 ]] || { _protected_overlay_error 'PROTECTED_OVERLAY_ARGUMENTS'; return 1; }
  local root="$1"
  local current_sha="$2"
  local target_sha="$3"
  _protected_overlay_repo "$root" || return
  _protected_overlay_sha "$current_sha" || return
  _protected_overlay_sha "$target_sha" || return
  local current_a target_a current_b target_b
  current_a="$(_protected_overlay_blob "$root" "$current_sha" "$PROTECTED_OVERLAY_PATH_A" || true)"
  target_a="$(_protected_overlay_blob "$root" "$target_sha" "$PROTECTED_OVERLAY_PATH_A" || true)"
  current_b="$(_protected_overlay_blob "$root" "$current_sha" "$PROTECTED_OVERLAY_PATH_B" || true)"
  target_b="$(_protected_overlay_blob "$root" "$target_sha" "$PROTECTED_OVERLAY_PATH_B" || true)"
  [[ -n "$current_a" && "$current_a" == "$target_a" &&
     -n "$current_b" && "$current_b" == "$target_b" ]] ||
    _protected_overlay_error 'PROTECTED_OVERLAY_TARGET_CHANGED'
}

protected_overlay_write_manifest() {
  [[ "$#" -eq 4 ]] || { _protected_overlay_error 'PROTECTED_OVERLAY_ARGUMENTS'; return 1; }
  local root="$1"
  local current_sha="$2"
  local target_sha="$3"
  local manifest_path="$4"
  _protected_overlay_repo "$root" || return
  _protected_overlay_sha "$current_sha" || return
  _protected_overlay_sha "$target_sha" || return
  local current_a target_a current_b target_b
  current_a="$(_protected_overlay_blob "$root" "$current_sha" "$PROTECTED_OVERLAY_PATH_A" || true)"
  target_a="$(_protected_overlay_blob "$root" "$target_sha" "$PROTECTED_OVERLAY_PATH_A" || true)"
  current_b="$(_protected_overlay_blob "$root" "$current_sha" "$PROTECTED_OVERLAY_PATH_B" || true)"
  target_b="$(_protected_overlay_blob "$root" "$target_sha" "$PROTECTED_OVERLAY_PATH_B" || true)"
  [[ -n "$current_a" && "$current_a" == "$target_a" &&
     -n "$current_b" && "$current_b" == "$target_b" ]] ||
    _protected_overlay_error 'PROTECTED_OVERLAY_TARGET_CHANGED'
  [[ ! -e "$manifest_path" ]] || _protected_overlay_error 'PROTECTED_OVERLAY_MANIFEST_EXISTS'
  local manifest_dir="${manifest_path%/*}"
  [[ "$manifest_dir" != "$manifest_path" ]] || manifest_dir='.'
  umask 077
  mkdir -p "$manifest_dir" || _protected_overlay_error 'PROTECTED_OVERLAY_MANIFEST_WRITE'
  chmod 700 "$manifest_dir" || _protected_overlay_error 'PROTECTED_OVERLAY_MANIFEST_WRITE'
  {
    printf 'current_sha=%s\n' "$current_sha"
    printf 'target_sha=%s\n' "$target_sha"
    printf 'protected_path=%s\nstate=DELETED\ncurrent_blob_sha=%s\ntarget_blob_sha=%s\n' "$PROTECTED_OVERLAY_PATH_A" "$current_a" "$target_a"
    printf 'protected_path=%s\nstate=DELETED\ncurrent_blob_sha=%s\ntarget_blob_sha=%s\n' "$PROTECTED_OVERLAY_PATH_B" "$current_b" "$target_b"
    if [[ -f "$root/client_errors.log.1" && ! -L "$root/client_errors.log.1" ]]; then
      printf 'client_errors.log.1\nstate=PRESENT\nsize=%s\nmode=%s\nsha256=%s\n' \
        "$(stat -c '%s' "$root/client_errors.log.1")" \
        "$(stat -c '%a' "$root/client_errors.log.1")" \
        "$(sha256sum "$root/client_errors.log.1" | awk '{print $1}')"
    elif [[ ! -e "$root/client_errors.log.1" && ! -L "$root/client_errors.log.1" ]]; then
      printf 'client_errors.log.1\nstate=ABSENT\nclassification=NORMAL_RUNTIME_DRIFT\n'
    else
      _protected_overlay_error 'PROTECTED_OVERLAY_RUNTIME_STATE'
    fi
  } >"$manifest_path" || _protected_overlay_error 'PROTECTED_OVERLAY_MANIFEST_WRITE'
  chmod 600 "$manifest_path" || _protected_overlay_error 'PROTECTED_OVERLAY_MANIFEST_WRITE'
}

protected_overlay_apply() {
  [[ "$#" -eq 1 ]] || { _protected_overlay_error 'PROTECTED_OVERLAY_ARGUMENTS'; return 1; }
  local root="$1"
  _protected_overlay_repo "$root" || return
  local path
  for path in "$PROTECTED_OVERLAY_PATH_A" "$PROTECTED_OVERLAY_PATH_B"; do
    if [[ -e "$root/$path" || -L "$root/$path" ]]; then
      [[ -f "$root/$path" && ! -L "$root/$path" ]] ||
        _protected_overlay_error 'PROTECTED_OVERLAY_POSTCHECK_FAILED' || return
      rm -f -- "$root/$path" ||
        _protected_overlay_error 'PROTECTED_OVERLAY_APPLY_FAILED' || return
    fi
    [[ ! -e "$root/$path" && ! -L "$root/$path" ]] ||
      _protected_overlay_error 'PROTECTED_OVERLAY_POSTCHECK_FAILED' || return
  done
}

protected_overlay_verify() {
  [[ "$#" -eq 1 ]] || { _protected_overlay_error 'PROTECTED_OVERLAY_ARGUMENTS'; return 1; }
  local root="$1"
  _protected_overlay_repo "$root" || return
  local staged unstaged expected
  staged="$(git -C "$root" diff --cached --name-status --no-renames)"
  unstaged="$(git -C "$root" diff --name-status --no-renames)"
  expected=$'D\tartifacts/treegpt-cache-probe-baseline.json\nD\tartifacts/treegpt-cache-probe-live.json'
  [[ -z "$staged" && "$unstaged" == "$expected" ]] ||
    _protected_overlay_error 'PROTECTED_OVERLAY_POSTCHECK_FAILED'
  [[ ! -e "$root/$PROTECTED_OVERLAY_PATH_A" && ! -L "$root/$PROTECTED_OVERLAY_PATH_A" &&
     ! -e "$root/$PROTECTED_OVERLAY_PATH_B" && ! -L "$root/$PROTECTED_OVERLAY_PATH_B" ]] ||
    _protected_overlay_error 'PROTECTED_OVERLAY_POSTCHECK_FAILED'
}