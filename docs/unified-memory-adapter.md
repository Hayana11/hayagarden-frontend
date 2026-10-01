# Unified Memory Adapter

This patch creates one stable integration boundary between Haya Garden and
Ombre Brain. It does **not** switch production to Ombre Brain 3.6.14 and it does
not modify the live vault.

## Backends

The default remains the deployed legacy module:

```bash
OMBRE_ADAPTER_BACKEND=legacy_module
OMBRE_BRAIN_ROOT=/opt/ombre-brain
```

An isolated Ombre Brain 3.6.14 shadow uses HTTP for reads and MCP only for explicitly authorized operations:

```bash
OMBRE_ADAPTER_BACKEND=http
OMBRE_HTTP_BASE_URL=http://127.0.0.1:<port>
OMBRE_MCP_URL=http://127.0.0.1:<port>/mcp
# Separate dashboard secret; never reuse the embedding/Gemini key:
OMBRE_DASHBOARD_PASSWORD=<separate dashboard secret>
# Set only when MCP token authentication is enabled:
OMBRE_MCP_TOKEN=...
```

Dashboard REST and MCP are separate authentication boundaries. `/api/*`
requires an Ombre Dashboard session: the adapter POSTs the dashboard password
to `/auth/login`, retains the `ombre_session` cookie, and retries the original
request. `/mcp` follows Ombre's MCP authentication configuration and is not
made anonymous by this adapter. The sidecar must remain localhost-only; do not
expose it publicly merely to use the adapter.

Modern HTTP retrieval is read-only. HayaGarden search does not call touch or
reinforcement APIs; explicit reinforcement is a separate, deliberate action.
The `mcp` package is listed in `requirements.txt` for the isolated shadow
runtime, but must not be installed into the live production Python environment
as part of R3B.

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

## HTTP backend contract

The HTTP backend is intended for an isolated Ombre 3.6.14 shadow only:

- `/api/*` requires Dashboard session authentication; anonymous REST access is
  not supported or enabled by this adapter.
- `/mcp` is a separate MCP authentication boundary and may use the sidecar's
  configured localhost-only mode or an MCP token.
- `/api/search` is treated as an active-only server contract because pinned
  Ombre 3.6.14 performs `list_all(include_archive=False)` and constrains
  semantic search to active bucket IDs. The adapter preserves
  `list[tuple[name, content]]`, `limit`, and 300-character clipping without
  inferring lifecycle from directory paths.
- `/api/buckets` may include terminal records; handoff excludes
  `type=archived`, `deleted_at`, and `tombstone` records before fetching detail.
- Explicit HTTP search keeps `touch=True` as a compatibility no-op. Modern
  Ombre retrieval is separate from explicit reinforcement.
- HayaGarden must not expose the sidecar publicly merely to use this adapter.
- `OMBRE_DASHBOARD_PASSWORD` is a separate Dashboard secret and must never be
  replaced with or copied from the embedding/Gemini secret.

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

Ombre Brain 3.6.14 starts its decay engine during application startup and runs
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
5. Read-only shadow evaluation with real Ombre 3.6.14 fixtures.
6. A separately approved cutover and rollback window.

## P0 security: Ombre `test_tools.py` (out of scope for deploy)

Production forensics flagged `/opt/ombre-brain/test_tools.py` as potentially
reading production config and deleting real buckets at test teardown.

**Do not run this script on production.** Until isolated:

- add a hard gate that refuses execution when pointed at production bucket paths;
- tests must use disposable temp directories only.

Track this as a **separate P0 security fix** — not bundled with #134 adapter
merge/deploy.
## R3C normalized consumer boundary

R3C consumers use these read-only normalized adapter fields only:

id, name, type, domain, tags, valence, arousal, importance, created, last_active, content

`list_memory_records()` is active-only and supports `bucket_type`, `domain`,
`min_arousal`, deterministic timestamp ordering, and optional content loading.
`get_memory_record(bucket_id)` is an ID-based single-record read. Terminal
records (`type=archived`, `deleted_at`, or `tombstone`) are rejected by both
legacy and HTTP implementations.

Emotion corrections use `update_memory_emotion(bucket_id, valence, arousal)`.
Callers supply bipolar valence in `[-1, 1]`; the adapter stores the Ombre
unipolar value and uses MCP `trace` without reinforcement. Legacy updates use
the BucketManager ID update primitive. No consumer opens or writes Markdown.

The relationship, thought, Discord, and Moments consumers are intentionally
still dormant on this branch. Discord requires `DISCORD_BOT_TOKEN` from the
environment and reads the latest `permanent/呼吸间` content through the
read-only CLI; it has no embedded token fallback.
