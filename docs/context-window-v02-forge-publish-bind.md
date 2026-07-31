# Context Window v0.2 — Forge file publish + Intent bind (R1)

Publish Preview-validated candidate bytes to a never-activated Claude JSONL and
bind candidate identity onto an existing `context_switch_intents` row.

Flag-off. No target `daily_context`, Session Registry, staged resident, Claude
calls, first-turn commit, or source close.

## Scope

| Module | Change |
|--------|--------|
| `chat/context_window_preview.py` | `PreparedPreviewCandidate` / `prepare_context_window_candidate`; public Preview response unchanged |
| `chat/context_window_forge_publish.py` | Atomic no-replace publish + intent bind |
| `chat/daily_context.py` | Idempotent `preview_id` / `thinking_policy` / `target_jsonl_size` columns |
| `tests/test_context_window_forge_publish.py` | Four focused cases |
| This doc | Contract notes |

Frozen: `context_window.py` switch state machine, old `context_window_forge.py`,
routes, gateway, runtime, resident, registry/mapping cores, Transcript Core,
frontend, deploy, flags, Actions.

## Sole publisher (execution lock)

`publish_context_window_forge_candidate` is wrapped with the existing
`_serialize_context_switch` process lock from `chat.context_window`. Same-request
and cross-request publishers serialize on that lock. No new file lock, lease
table, worker, or queue.

`_claim_forge_owner` returns `(live, claimed)`. **`claimed=False` is not a
first-publish authorization.**

| `claimed` | Allowed |
|-----------|---------|
| `True` | Create candidate binding; first file publish; NATIVE_COLD first bind |
| `False` | Complete `orphan_jsonl_state='none'` persisted replay **or** exact-match `pending` crash recovery only |

Unset / partial / mismatched / non-allowed status under `claimed=False` →
`FORGE_IN_PROGRESS` or `FORGE_INTENT_CANDIDATE_CONFLICT`. Never first publish.

## Persisted ALREADY replay (before Preview)

Order:

1. Validate input
2. Read-only lookup → `_replay_persisted_published_binding`
3. Only if no complete published binding → `reserve_or_load_intent` →
   `prepare_context_window_candidate` → claim / recover / publish

Replay requirements:

- Missing request → `None` (continue)
- Reserved with all candidate bindings unset → `None` (reserve defaults
  `orphan='none'`; that is **not** a published binding)
- `_payload_hash` mismatch → existing idempotency conflict
- Partial binding → `FORGE_INTENT_CANDIDATE_CONFLICT`
- `preview_id` / `thinking_policy` must match exactly on complete bindings
- `target_context_id` or `staged_ready_at` non-NULL → conflict
- Replay only when complete binding + `status=forging` + `orphan='none'`
- Does **not** update intent `updated_at`
- Does **not** call `prepare_context_window_candidate`, Reader, Mapping,
  Transform, or Validator
- Does **not** require source still open or source version unchanged

Proof kinds:

- NATIVE_COLD: `native_cold_contract` (session non-empty, size=0, empty SHA)
- File: `published_binding_contract` (verify persisted session/hash/size on disk;
  `event_count` from verified file; `selected_round_count` from locked
  `selected_message_ids` user rows)

## File ownership identity

```text
VerifiedFileIdentity { path, st_dev, st_ino, size, sha256, event_count }
PublishedFile.identity: VerifiedFileIdentity
```

`_verify_bound_file` opens via parent dir fd (`O_RDONLY`, `O_NOFOLLOW` when
available), `fstat` regular + mode ≤ `0600`, size exact, SHA-256 + non-empty
JSONL line count through the fd, then re-`fstat` for stable `st_dev` /
`st_ino` / `st_size`. No path-based `open()`. Absolute paths are not leaked in
exceptions.

First publish and pending recovery both capture this identity.

## Exact pending release CAS

Candidate ownership paths do **not** use loose `_release_intent`.

Foreign target and source-change cleanup require exact CAS:

- `status=forging`
- `orphan_jsonl_state=pending`
- exact `preview_id` / `thinking_policy` / `target_session_id` /
  `target_jsonl_sha256` / `target_jsonl_size`
- `target_context_id IS NULL`
- `staged_ready_at IS NULL`

CAS `rowcount != 1` → no state change, no delete, `FORGE_DB_FINALIZE_FAILED`.

### Source-change owned cleanup

`_release_and_cleanup_owned_pending`:

1. `BEGIN IMMEDIATE` + re-read live intent (exact pending above)
2. Re-verify current final against captured `VerifiedFileIdentity`
   (`st_dev` / `st_ino` / size / sha256)
3. Mismatch → do **not** delete; mark `released` /
   `orphan_jsonl_state=delete_blocked` / `FORGE_SOURCE_CHANGED`
4. Match → dir-fd unlink + dir fsync → `released` /
   `orphan=deleted` / `FORGE_SOURCE_CHANGED`

Replacement inode (different content) is therefore `delete_blocked` and kept.

## Temp cleanup fail-closed

`_publish_jsonl_noreplace`:

- `tmp_created=False` only after a successful temp `unlink` (+ dir fsync)
- First unlink failure keeps `tmp_created=True`; `finally` retries
- After finally, confirm temp absence via dir fd
- Temp still present → if same inode as final, unlink final; retry temp;
  return `FORGE_WRITE_FAILED`
- Never return `PUBLISHED` while temp remains
- No `os.replace` / move / overwrite of final

## Preview artifact reuse

`prepare_context_window_candidate` runs the same Preview gates (Registry /
Mapping / Transform / Validator / prefix before-after / fresh recheck). READY /
NATIVE_COLD produce a `PreparedPreviewCandidate` whose `serialized_jsonl` bytes
are exactly what Validator accepted. Forge never re-implements Transform.

Public `preview_context_window` only returns `response` (`allowed_switch_request_id=None`).
Forge passes `allowed_switch_request_id=request_id` so the publisher may ignore
**only** its own active intent.

## Intent columns (nullable)

Added by `_ensure_context_switch_forge_schema` via `ALTER TABLE ADD COLUMN`:

- `preview_id TEXT NULL`
- `thinking_policy TEXT NULL`
- `target_jsonl_size INTEGER NULL`

R1 forces canonical `preview_id == request_id`. Success leaves `status=forging`,
`target_context_id=NULL`, `staged_ready_at=NULL`.

## Publish order

1. Validate preview/request/thinking
2. `_replay_persisted_published_binding` (may return `ALREADY_PUBLISHED`)
3. Else `reserve_or_load_intent`
4. `prepare_context_window_candidate(allowed_switch_request_id=request_id)`
5. Match artifact ↔ intent source/selection
6. `_claim_forge_owner` — honor `claimed`
7. Pre-bind candidate identity (`orphan_jsonl_state='pending'`) before file I/O
8. NATIVE_COLD → bind empty SHA/size=0, `orphan='none'`, no file
9. READY → `_publish_jsonl_noreplace` (temp `O_EXCL` + `os.link` no-replace +
   dir fsync; temp must be gone)
10. Source + prefix recheck (existing scope; cleanup uses `VerifiedFileIdentity`)
11. CAS finalize `pending → none` (status remains `forging`)

## Recovery

- File written + finalize fails → leave `forging` + `pending`; retry verifies
  existing final (size/hash/dev/ino) with `recovered_existing_file=True`
- Unknown pre-existing final after new binding → `FORGE_TARGET_EXISTS`,
  `foreign_exists`, no overwrite/delete of foreign bytes
- Source drift after owned publish → identity-matched delete (`orphan=deleted`)
  or `delete_blocked` when inode/hash no longer matches
- Same-request concurrent retries serialize on `_serialize_context_switch`;
  outcomes are one `PUBLISHED` + one `ALREADY_PUBLISHED` (never dual Forge)

## Why no Session Registry yet

Registry registration requires a real target `daily_context` identity
(`context_id` / `epoch` / `resident_generation` / `chat_id`). This stage does
not create a target context, so Registry bind is deferred.
