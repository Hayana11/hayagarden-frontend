# Context Window v0.2 — Transcript Core

Flag-off infrastructure only. Not wired into runtime, Preview, Manual Forge, or deployment.

## Scope

| Module | Path | Responsibility |
|--------|------|----------------|
| Model | `chat/claude_transcript_model.py` | Immutable event / candidate-round / graph types; thinking & sidechain policy enums |
| Reader | `chat/claude_transcript_reader.py` | Read-only JSONL parse; UUID/parent/tool indexes; **candidate** user classification; order-independent sidechain attribution |
| Transform | `chat/claude_transcript_transform.py` | Pure deterministic rebuild; mapping confirms real user; whole-round sidechain exclusion; native-cold 0-round |
| Validator | `chat/claude_transcript_validator.py` | Local structural contract only (`proof_kind=local_structure_contract`); KEEP thinking requires signature |

## Contract (R1–R3)

1. Ordinary JSONL `user` rows are `CANDIDATE_USER` only. Confirmed kitten/user text comes solely from `user_canonical_by_event_uuid`.
2. **SidechainPolicy v0.2 supports `EXCLUDE` only.** v0.2 does not support Sidechain KEEP. Illegal policy → `TRANSFORM_INVALID_POLICY` (no silent fallback).
3. Sidechain impact uses the **full parent graph** (not JSONL line order). Delayed sidechain / descendants still pollute the true parent round; that whole confirmed round is dropped under EXCLUDE.
4. Unattributed / cyclic sidechain → warnings `unattributed_sidechain:<uuid>` / `sidechain_parent_cycle:<uuid>`; never migrate; never contaminate unrelated rounds.
5. `keep_rounds=0` defaults to **native cold / empty transcript** (no fabricated boundary user).
6. Old `SYSTEM` events are never attached to rounds and never migrated.
7. `ThinkingPolicy.KEEP` requires non-empty `signature` on signed thinking (transform + validator).

## Spike decision evidence (locatable)

| Decision | Evidence |
|----------|----------|
| External forged JSONL can `--resume` | `docs/manual-forge-spike-report.md` LIVE_PASS; `artifacts/spike-claude-forge-resume/results.json` |
| Production argv baseline / staged resume | `cc_resident.py` `_spawn` + `spawn_resumable` |
| Thinking keep/drop strategies | `tools/claude_forge_core.py` keep/drop; CASE 2A/2B; explicit `ThinkingPolicy` (KEEP = intact incl. signature) |
| Tool round pair + ID remap | Spike CASE 3 + forge validator pair rules |
| 0-round | **native cold / empty** in this Core (Spike did not close 0-round boundary-user semantics) |
| Sidechain exclude | Spike CASE 5B direction; this Core excludes the **whole affected round** via parent-graph attribution |

## Explicit non-goals

- no runtime wiring / DB / Session Registry / Mapping service
- no Preview API / Manual Forge production changes
- no Capacity Swap / front-end / CI / deployment / flag changes
- no claim of Claude resume acceptance from Validator
- no Sidechain KEEP implementation in v0.2

## Safety

- Reader opens source with `O_RDONLY` only; tests assert SHA-256 / size / bytes unchanged
- Transform never copies full old user payloads; rebuilds from authoritative mapping
- No `mtime` active-session selection, no `--continue`, no model calls, no real resume
