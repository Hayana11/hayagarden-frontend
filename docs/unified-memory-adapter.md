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

The optional `mcp` Python package is **not** installed from `requirements.txt`.
Install it only in disposable shadow environments before enabling the HTTP
backend.

## Legacy scope (default `legacy_module`)

Default `legacy_module` keeps call interfaces, return shapes, timeouts and jieba-only
warmup compatible with production. **One approved behaviour change** is included:

- nightly cleaner **stops** auto-upgrading `importance >= 8` to permanent `pinned`.

`importance`, `layer=core`, and `pinned` are decoupled:

- cleaner auto-sync always passes `pinned=False` regardless of importance 1–10;
- `layer=core` promotion from high importance may remain;
- explicit `hold_memory(..., pinned=True)` and other controlled paths still pass
  through to Ombre unchanged.

This PR **does not** bulk-unpin, delete or rewrite existing production buckets.
Cleaning ~54 historical auto-pinned records is a **separate data-repair task**
(re backup → read-only report → approval).

Anything outside the list above is either dormant HTTP code or an explicitly
documented future change.

## Safety rules

- Automatic handoff never calls `touch`.
- Recent continuity accepts only ordinary `type=dynamic` memories.
- Permanent, pinned, protected (legacy metadata), resolved, digested and
  `dont_surface` records cannot masquerade as recent continuity in the legacy
  safe builder.
- Emotion calibration excludes permanent/pinned/test/terminal memories.
- Frontend hot paths no longer import Ombre Brain's internal `server.py`.
- `memory_unification_audit.py` opens SQLite read-only and never writes Markdown.

## HTTP backend known gaps (dormant until shadow phase)

The HTTP path is intentionally incomplete and must not be treated as production
parity merely because unit mocks pass:

- `/api/buckets` includes archive buckets but list payloads expose no archive
  flag, so HTTP recent continuity cannot yet match
  `list_all(include_archive=False)`.
- Ombre 2.8.10 list payloads expose `pinned`, `resolved`, `digested`,
  `dont_surface`, etc., but **not** legacy vault `protected` metadata.
- HTTP explicit search currently ignores `touch=True`; activation / last_active
  semantics differ from legacy recall until a touch API is chosen.
- HTTP handoff/search now honour a total wall-clock deadline, but cutover still
  requires real sidecar fixtures, not invented mock fields.

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
5. Read-only shadow evaluation with real Ombre 2.8.10 fixtures.
6. A separately approved cutover and rollback window.

## P0 security: Ombre `test_tools.py` (out of scope for deploy)

Production forensics flagged `/opt/ombre-brain/test_tools.py` as potentially
reading production config and deleting real buckets at test teardown.

**Do not run this script on production.** Until isolated:

- add a hard gate that refuses execution when pointed at production bucket paths;
- tests must use disposable temp directories only.

Track this as a **separate P0 security fix** — not bundled with #134 adapter
merge/deploy.
