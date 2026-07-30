# Context Window v0.2 — Transcript Core

Flag-off infrastructure only. Not wired into runtime, Preview, Manual Forge, or deployment.

## Scope

| Module | Path | Responsibility |
|--------|------|----------------|
| Model | `chat/claude_transcript_model.py` | Immutable event / round / graph types; thinking & sidechain policy enums |
| Reader | `chat/claude_transcript_reader.py` | Read-only JSONL parse; UUID/parent/tool indexes; role classification |
| Transform | `chat/claude_transcript_transform.py` | Pure deterministic rebuild from explicit params + authoritative user mapping |
| Validator | `chat/claude_transcript_validator.py` | Local structural contract only (`proof_kind=local_structure_contract`) |

## Spike decision evidence (locatable)

| Decision | Evidence |
|----------|----------|
| External forged JSONL can `--resume` | `docs/manual-forge-spike-report.md` LIVE_PASS; `artifacts/spike-claude-forge-resume/results.json` |
| Production argv baseline / staged resume | `cc_resident.py` `_spawn` + `spawn_resumable` |
| Thinking executable strategies | `tools/claude_forge_core.py` `keep_thinking` / `drop_thinking`; CASE 2A/2B fixtures; Transform takes explicit `ThinkingPolicy` |
| Tool round pair + ID remap | `tools/claude_forge_core.py` `_remap_tool_blocks`; `tools/claude_forge_validator.py` `FORGE_TOOL_PAIR`; CASE 3 fixture |
| 0-round boundary primer | `chat/context_window_forge.py` `BOUNDARY_PRIMER_USER` / `BOUNDARY_PRIMER_ASSISTANT` (Spike Tool Primer live CASE 4 not closed; this PR only exposes primer *candidate interface*, no ancestor search) |
| Sidechain exclude | Spike CASE 5B + `exclude_sidechain=True`; production `forbid_sidechain=True` |

## Explicit non-goals

- no runtime wiring / DB / Session Registry / Mapping service
- no Preview API / Manual Forge production changes
- no Capacity Swap / front-end / CI / deployment / flag changes
- no claim of Claude resume acceptance from Validator

## Safety

- Reader opens source with `O_RDONLY` only; tests assert SHA-256 / size / bytes unchanged
- Transform never copies full old user payloads; rebuilds from authoritative mapping
- No `mtime` active-session selection, no `--continue`, no model calls, no real resume
