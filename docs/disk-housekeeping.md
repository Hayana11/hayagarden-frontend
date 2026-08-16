# HayaGarden Disk Housekeeping v1

This PR is repository-only. It provides a dry-run-first housekeeping command,
uninstalled systemd templates, and logrotate proposals. It does not install
units, edit `/etc`, delete production data, or restart services.

## Actions and schedule

Daily (`20:30 UTC`, approximately `04:30 Asia/Shanghai`) runs:

- the frozen HayaGarden `/tmp` allowlist audit/cleanup;
- the existing `tools/backup_retention.py` algorithm.

Weekly (`Saturday 21:00 UTC`, approximately Sunday `05:00 Asia/Shanghai`)
runs:

- the npm npx-cache capability check and cache verification;
- the exact `/root/.cache/pip` cache purge after path validation.

The proposed timers use `Persistent=true` and randomized delay. They are
templates only; this PR does not install or enable them.

## Usage

```text
python3.11 tools/disk_housekeeping.py --daily --dry-run
python3.11 tools/disk_housekeeping.py --weekly --dry-run
python3.11 tools/disk_housekeeping.py --daily --execute
python3.11 tools/disk_housekeeping.py --weekly --execute
```

If neither action flag is supplied, the command parser rejects the request.
If neither `--dry-run` nor `--execute` is supplied, the mode defaults to
dry-run. The two action flags are mutually exclusive. There is no CLI option
for a cleanup root or arbitrary path.

## `/tmp` allowlist

The only delete-capable prefixes in v1 are:

```text
forge-switch-*
forge-id-*
forge-post-*
forge-pswap-*
forge-dead-*
forge-vision-*
```

Ownership evidence is repository source, not a production observation:
`tests/test_context_window_forge_switch.py` creates isolated Forge test roots
with `tempfile.mkdtemp(prefix=...)` for each of these prefixes. Round 1 names
without source ownership evidence, including `haya-runtime-parity-*`,
`hayagarden-qa.*`, `daily-replica-*`, `diag-replica-*`, `cc-r0-*`,
`ombre-v2-venv`, and `pr*-memories.db`, remain report-only.

A candidate must be a direct `/tmp` child, match a frozen prefix, be a
directory, be at least 24 hours old, be on the same device, and not be a
symlink. Regular files with an allowlisted-looking name are never deleted.
Its complete subtree must contain only regular files/directories, no `.git`
marker, no socket/device/FIFO, and no systemd-private or snap-private path.
The path must not be listed by `git worktree list`, and Linux
`/proc/self/mountinfo` must prove that neither the candidate nor a descendant
is an extra mountpoint. Unreadable or malformed mountinfo fails closed. A
best-effort `/proc` scan checks process cwd, root, and file descriptors;
permission or race uncertainty skips the candidate. The candidate is
lstat-validated again immediately before execute deletion.

Dry-run emits `WOULD_DELETE_TMP`; execute emits `DELETED_TMP`. The command
never calls `find -delete`, `rm -rf`, or `systemd-tmpfiles --clean`.

## Locks and result states

Execute mode first takes non-blocking locks in this order:

1. `/var/lock/hayagarden-housekeeping.lock`
2. `/var/lock/hayagarden-frontend-deploy.lock`

If either lock is busy, it prints `SKIP_ACTIVE_LOCK`, does not wait, and exits
0. Dry-run does not take the deploy lock; it reports `free`, `busy`, or
`unknown`.

Every run emits `HOUSEKEEPING_START` and `HOUSEKEEPING_END` summaries. A root
filesystem at or above 90% produces `PASS_WITH_DISK_ALERT`, never an expanded
cleanup scope. Retention's known disk-alert return code is treated as a
non-fatal alert only when the retention command otherwise completed normally.

## Weekly caches

The production build/deploy identity is root and npm's expected cache is
`/root/.npm`. The command refuses an unexpected npm cache path. It checks for
an explicit `npm cache npx rm` capability before attempting it; unsupported
npm versions emit `SKIP_NPX_UNSUPPORTED` and do not hand-delete `_npx`.

`npm cache verify --cache /root/.npm` is allowed only in weekly execute mode.
The pip cache path must normalize exactly to `/root/.cache/pip`; otherwise the
command emits `REFUSE_UNEXPECTED_PIP_CACHE_PATH`. Only weekly execute mode may
run `/usr/bin/python3.11 -m pip cache purge`. The command never deletes all of
`/root/.cache`, Playwright data, or Codex data.

## Backups and anchors

The command calls the existing `tools/backup_retention.py`; it does not copy
or reinterpret its retention algorithm. It never edits or deletes an anchor
itself. `tools/backup_anchors.txt` remains the authority for permanent
protection. The PR intentionally does not introduce the two-copy policy.

Manual backups, `.bak` files, unknown backup directories, databases,
attachments, uploads, `memories.db*`, and `/opt/frontend/backups` remain out of
automatic housekeeping scope.

## Logs

The confirmed HayaGarden file log is `/opt/frontend/client_errors.log`.
The proposed `deploy/logrotate/hayagarden-frontend` stanza uses `maxsize 50M`,
`rotate 3`, compression, delayed compression, `missingok`, `notifempty`, and
`create 0640 root root`. It deliberately does not use `copytruncate`: `app.py`
opens, appends, and closes the file for each write, so there is no persistent
file descriptor to preserve. `mcp-http.log` and `gateway.log` remain
report-only until their writer/reopen semantics are proven.

Production currently has nginx and rsyslog logrotate entries. This PR does not
edit `/etc/logrotate.d` or the logrotate timer.

Production audit found Shop restart loops producing approximately
106–107 MB/day before remediation. After runtime retirement and disablement of
the two broken Shop services, the usable post-fix measurement was approximately
2.36 MB/day, with an estimated 50 MiB growth time of approximately 22 days.

Therefore the final rsyslog policy is:

- existing weekly rotation;
- existing system daily logrotate evaluation;
- `rotate 4`;
- `maxsize 50M` safety net;
- compression and delayed compression;
- the existing rsyslog HUP hook;
- no dedicated hourly timer.

The repository template is `deploy/logrotate/rsyslog`. Its complete observed
production file set is preserved. The existing historical `/var/log/syslog.1`
is pre-fix debt and must not be manually deleted or truncated as part of policy
rollout. Installation remains a separate approved operation; this PR does not
edit `/etc/logrotate.d` or the logrotate timer.

## Installing or rolling back the templates later

Installation is intentionally a separate approved operation. A future operator
must copy the reviewed templates to `/etc/systemd/system/` and then run
`systemctl daemon-reload`, enable/start only the housekeeping timers, and
inspect their journal. Do not bind them to frontend service restarts.

To stop a future installation:

```text
sudo systemctl disable --now hayagarden-housekeeping-daily.timer
sudo systemctl disable --now hayagarden-housekeeping-weekly.timer
```

Rollback means stopping/disabling those two timers, removing only the four
installed housekeeping unit files after the units are inactive, and running
`systemctl daemon-reload`. It does not touch frontend units, deploy scripts,
database files, caches, backups, or `/tmp` contents.

## Deliberate exclusions

Playwright, Codex, snapd, `/var/tmp`, systemd-private directories, snap-private
directories, Unix sockets, Git worktrees, unknown `/tmp` names, and all
production runtime/data paths are excluded because age alone cannot prove they
are disposable.

