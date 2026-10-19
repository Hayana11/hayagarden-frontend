# Memory Runtime MVP v1

This MVP adds a narrow runtime bridge around the existing legacy memory path.

## Authority and gates

- `tools.memory_tool.save_memory` remains the legacy `posts` persistence owner.
- Ombre remains a best-effort compatibility projection through `tools.ombre_adapter`.
- The Memory Kernel is not imported, initialized, or used as a production authority.
- The Ombre HTTP backend is not selected by this change.
- `MEMORY_RUNTIME_AUTO_CANDIDATE_MODE` defaults to `shadow`.
- Automatic candidate writes require both:
  - `MEMORY_RUNTIME_AUTO_CANDIDATE_MODE=write`
  - `MEMORY_RUNTIME_AUTO_CANDIDATE_ENABLED=1`
- A missing or failed Ombre write does not fail the legacy commit or chat turn.

The receipt database is separate from `memories.db`. Production defaults to
`/var/lib/hayagarden/memory-runtime-receipts.db`; tests can set
`MEMORY_RUNTIME_RECEIPT_DB_PATH`.

## Provenance and receipts

Every bridge operation records bounded source provenance and a deterministic
idempotency key derived from source identity and content SHA-256. Receipt event
IDs are deterministic for a receipt and stage.

The relevant stages are:

- `candidate_formed`: safe automatic candidate was formed in shadow mode.
- `memory_persisted`: legacy persistence was verified; Ombre status is metadata.
- `memory_recalled_injected`: a legacy memory selected by recall was injected.
- `new_window_handoff_recalled`: an isolated handoff verification matched the
  persisted memory.

Receipt metadata stores hashes, IDs, statuses, and error classes. It does not
store message bodies.

## Candidate safety rule

The first-phase candidate rule accepts at most one bounded turn candidate when
both sides have text, the turn is within the size limit, the user text contains
a preference/remember/decision intent, and no configured sensitive-looking
pattern is present. The rule is deterministic and makes no model request.

## Acceptance evidence

The focused test covers:

`turn A -> memory_persisted -> turn B memory_recalled_injected -> new_window_handoff_recalled`

It also verifies duplicate submission, Ombre fail-open behavior, and that
automatic candidates stay out of legacy `posts` until the explicit write gate
is enabled.

## Session memo safety boundary

A normal session memo is observer-only in this phase. It records provenance,
idempotency, receipt, and best-effort Ombre projection, but it does not write
legacy `posts`. Explicit `save_memory` and `[[SAVE:]]` remain the only
runtime paths that can create a legacy memory record. There is no session-memo
legacy-write switch enabled by default.

## Production receipt preflight

Before a production deployment, run this receipt preflight as the frontend
service identity, before invoking the standard deployment script:

```bash
sudo -n /bin/bash /opt/frontend/scripts/verify-memory-runtime-receipts.sh
```

The script resolves the `frontend` and `frontend-gw` systemd users and
invokes the bridge verification under that identity. Verification creates the
parent/database if needed, enables WAL, writes and commits a probe receipt,
reopens it to confirm the commit, then removes the probe row. A successful
result
must report `status=VERIFIED` and `journal_mode=wal`. This is a deployment
precondition; it does not change the production authority or enable Kernel
writes.
