# Unified Memory Adapter

This patch creates one stable integration boundary between Haya Garden and
Ombre Brain. It does **not** switch production to Ombre Brain 2.8.10 and it does
not modify the live vault.

## Backends

The default remains the deployed legacy module:

```bash
OMBRE_ADAPTER_BACKEND=legacy_module
OMBRE_BRAIN_ROOT=/opt/ombre-brain
```

A future Ombre Brain 2.8.10 sidecar uses HTTP for reads and MCP for writes:

```bash
OMBRE_ADAPTER_BACKEND=http
OMBRE_HTTP_BASE_URL=http://127.0.0.1:18001
OMBRE_MCP_URL=http://127.0.0.1:18001/mcp
OMBRE_DASHBOARD_PASSWORD=...
# Set only when MCP token authentication is enabled:
OMBRE_MCP_TOKEN=...
```

Dashboard endpoints and MCP use separate authentication. The adapter logs into
the Dashboard with a cookie for `/api/*`; MCP calls use the MCP token or the
sidecar's configured localhost-only no-auth mode.

## Safety rules

- Automatic handoff never calls `touch`.
- Recent continuity accepts only ordinary `type=dynamic` memories.
- Permanent, pinned, protected, resolved, digested and `dont_surface` records
  cannot masquerade as recent continuity.
- Emotion calibration excludes permanent/pinned/test/terminal memories.
- Importance never implies `pinned`; only an explicit caller may pin.
- Frontend hot paths no longer import Ombre Brain's internal `server.py`.
- `memory_unification_audit.py` opens SQLite read-only and never writes Markdown.

## Read-only inventory

Run against consistent copies, not files that are being actively written:

```bash
python3 tools/memory_unification_audit.py \
  --sqlite /path/to/memories-copy.db \
  --vault /path/to/vault-copy \
  --json-out /path/to/report.json
```

The report lists exact cross-system matches, duplicate groups, records present
on only one side, pinned counts, hash titles and unclassified buckets.

## Important upstream sidecar caveat

Ombre Brain 2.8.10 starts its decay engine during application startup and runs
one cycle immediately. Increasing `check_interval_hours` only delays later
cycles; it does not suppress the first one. The current upstream auto-resolve
rule can therefore modify an attached vault at startup even when the sidecar is
intended to be used only for shadow reads.

Until a true no-decay/read-only startup mode exists, any sidecar experiment must
use a disposable snapshot. Never point the experimental service at the live
vault.

## Rollout gate

The patch is intentionally dormant on its feature branch. Production rollout
requires, in order:

1. Shadow observation acceptance.
2. A verified vault and SQLite backup.
3. A reviewed unification report and ID mapping.
4. A cleaned disposable vault for sidecar comparison.
5. Read-only shadow evaluation.
6. A separately approved cutover and rollback window.
