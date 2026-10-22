# Memory Kernel B-4: read cutover and rollout

## The switch: `memory.kernel_write` = Kernel cutover

The `runtime_config` key `memory.kernel_write` in `memories.db` is no longer a write-only switch. Since B-4 it moves the memory types (MEMORY / FACT / MISS) to the Memory Kernel **in both directions at once**:

- **writes**: `memory_tool.save_memory` goes to the Kernel (B-3);
- **reads**: every reader of memory types goes through `tools/memory_kernel_read.py`.

It flips once, so nothing is ever written to the Kernel while readers still only look at posts. Anything that cannot read the switch treats it as off. The switch must stay off until B-4 is deployed and the owner confirms.

## What readers see with the switch on

- **Source of truth:** the text is always the Kernel's current head. Ombre only ranks (bucket ids → lineage via the projection ledger).
- **Memories Ombre has not projected yet:** heads not yet projected, and posts that fell back on a Kernel write failure and are still waiting for backfill, are matched lexically. If Ombre cannot be asked, every head is matched lexically.
- **Memories Ombre has retired never come back:** archived, deleted / tombstoned, `dont_surface`, `digested`.
  - The verdict is Ombre's own (`SurfacePolicyVM`, "automatic" mode, observed by the worker).
  - It is recorded per bucket in `projection_lifecycle`.
  - It is kept while Ombre is unreachable.
  - A vanished bucket is "missing" and gets rebuilt; that is never treated as a deletion.
- **Buckets created inside Ombre** appear after the next sync (at most about five minutes later). Raw Ombre text is never shown.
- **Kernel memories are read-only on the static pages (core / long-term / recent)** until Kernel revisions exist (B-5). `PUT` / `PATCH` / `DELETE /api/posts/kernel:…` answer `409 kernel_memory_read_only`.

## Automatic sync

`deploy/systemd/memory-kernel-sync.{service,timer}` run `python3.11 -m tools.memory_kernel_store sync` every five minutes. The `OMBRE_RUNTIME_*` variables come from `EnvironmentFile=/opt/frontend/.env`, the same file the frontend and gateway services read.

- **No overlap:** a run takes a non-blocking lock (`<projection db>.sync.lock`), so a slow run is never doubled. Request-time projection and sync keep serialising per lineage through the existing locks.
- **Every run is recorded** in `projection_sync_runs`: start, finish, duration, outcome, summary (backfilled, ingested, projected, scan counts) and error.
- **Health check:** `python -m tools.memory_kernel_store status` prints, under `sync`, the last run, the last success and `consecutive_failures`.
- **Failures are loud:** a failed run exits 1, so systemd marks the unit failed. A missing runtime configuration counts as a failure.

Install (owner approval required; this changes the VPS):

```sh
cp /opt/frontend/deploy/systemd/memory-kernel-sync.service /etc/systemd/system/
cp /opt/frontend/deploy/systemd/memory-kernel-sync.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now memory-kernel-sync.timer
```

## Re-importing the production Kernel (before the first baseline)

The production Kernel was imported before `structured_value.memory` existed. Rebuild it from Ombre and posts with the current importer, and never overwrite the old file in place:

1. Import into a **new** file next to the old one.
2. Verify the new file:
   - every head carries `structured_value.memory`;
   - the totals and the MEMORY / FACT / MISS split, the long-term count and memo samples match the sources;
   - `PRAGMA integrity_check` returns `ok`;
   - the file mode is `0600`.
3. Rename the old Kernel to a dated backup, then move the new file into place (an atomic rename).
4. Keep the switch off.
5. Run one sync to build the Ombre baseline, then a second sync, which must report everything `unchanged`.

Keep the old Kernel until the cutover is stable.
