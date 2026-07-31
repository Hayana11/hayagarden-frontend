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

## Why no Session Registry yet

Registry registration requires a real target `daily_context` identity
(`context_id` / `epoch` / `resident_generation` / `chat_id`). This stage does
not create a target context, so Registry bind is deferred.

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
2. `reserve_or_load_intent`
3. `prepare_context_window_candidate(allowed_switch_request_id=request_id)`
4. Match artifact ↔ intent source/selection
5. `_claim_forge_owner`
6. Pre-bind candidate identity (`orphan_jsonl_state='pending'`) before file I/O
7. NATIVE_COLD → bind empty SHA/size=0, `orphan='none'`, no file
8. READY → `_publish_jsonl_noreplace` (temp `O_EXCL` + `os.link` no-replace + dir fsync)
9. Source + prefix recheck
10. CAS finalize `pending → none` (status remains `forging`)

## Recovery

- File written + finalize fails → leave `forging` + `pending`; retry verifies
  existing final (size/hash) with `recovered_existing_file=True`
- Unknown pre-existing final after new binding → `FORGE_TARGET_EXISTS`,
  `foreign_exists`, no overwrite/delete of foreign bytes
- Source drift after owned publish → delete owned final, `released` /
  `orphan=deleted`
