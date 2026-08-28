# Task Timer runtime DB cutover and rollback

This document defines the one-time production migration procedure. This PR does not run it, stop services, or deploy production.

## Target contract

The canonical path is:

    /var/lib/hayagarden/commands.db

The stable configuration source is the existing /opt/frontend/.env, loaded by both frontend.service and frontend-gw.service:

    TASK_TIMER_COMMANDS_DB_PATH=/var/lib/hayagarden/commands.db

Both services currently run as root (systemd User and Group are empty), and /var/lib/hayagarden is an existing root-owned 0755 directory. Before cutover, verify the active User and Group and adjust ownership or mode only if the service identity changed.

All API, feedback, command_store, task-timer adapter, and capability-proxy configuration resolve this same environment value. The code default is the same external path and never falls back to a repository-local commands.db.

## Cutover procedure

1. Record the old deployed SHA, old DB SHA-256, size, mode, schema, row count, maximum id, and exact current configuration. Confirm the target release SHA and that the source tree has no unrelated changes.
2. Identify every writer. At minimum this includes the Flask API in frontend.service, the gateway in frontend-gw.service, and capability proxy children spawned by the gateway. Stop both services and verify their processes and proxy children have exited. Do not copy a live SQLite file.
3. Create an independent rollback backup outside the repository, for example under /opt/backups/frontend/task-timer-cutover-UTC-stamp/. Use SQLite backup API or sqlite3 .backup from the old database to a temporary file. Record SHA-256, size, mode, and backup location.
4. Create the target directory with the verified service ownership and use SQLite backup API or .backup to write /var/lib/hayagarden/commands.db. Run PRAGMA integrity_check on the target.
5. Compare old and new schema, row count, maximum id, and newest real smoke row. The comparison must include pending and feedback rows; do not delete or rewrite timer rows.
6. Add TASK_TIMER_COMMANDS_DB_PATH=/var/lib/hayagarden/commands.db to /opt/frontend/.env, preserve its prior contents in the rollback backup, and verify both systemd EnvironmentFile declarations load it. Do not use a login-shell export.
7. Only after the external copy and comparisons pass, make the old tracked repository copy non-authoritative. With writers still stopped, restore the old tracked file to its Git baseline if needed to satisfy strict deploy preflight, then deploy the exact hygiene release SHA through the existing guarded deploy script. The hygiene release removes the tracked artifact; do not use the old repository file as the runtime database.
8. Start the services, verify both processes, and inspect the resolved path from the API process and capability-proxy configuration. Run production timer smoke only after all consumers report the external path.
9. Record post-cutover SHA-256, size, mode, integrity result, schema, row count, maximum id, newest id, service health, and exact deployed SHA. Confirm the source worktree is clean and no repository-local commands.db exists.

## Rollback procedure

1. Stop frontend, frontend-gw, and all capability proxy children. Verify no writer remains.
2. Preserve the failed target DB as a timestamped forensic copy. Restore the last verified rollback backup to the external canonical path with SQLite backup API or .backup, then verify integrity, schema, row count, maximum id, and SHA-256.
3. Restore the previous /opt/frontend/.env from the protected backup. If the previous release reads the legacy repository path, deploy that exact previous SHA only after restoring the legacy DB from the same verified backup while writers are stopped. Never use the Git blob as the data source.
4. Restart the exact previous services and verify API, feedback, proxy configuration, pending rows, and health. Record the previous SHA and all DB metadata.
5. Keep both the pre-cutover and failed-target backups until a later retention decision. Do not delete timer rows as part of rollback.