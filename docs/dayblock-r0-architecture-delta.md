# DayBlock R0 Architecture Delta

Date: 2026-09-26
Status: Preview-only shadow planning; no generation, cron, memory projection, or chat injection.

## Audit boundary

The requested named Continuity Compression Architecture Plan was not present in the Preview docs, the production docs, or the inspected ombre-brain docs. The general architecture file and current implementation describe adjacent contracts, but they do not contain the requested deferred Diary decision set or an Architecture Delta section. This note records the owner's new R0 decisions against the code that exists; it does not claim to replace or silently amend an unavailable frozen plan.

Related implementation contracts found:

- Continuity SourceSnapshot, completed_turn, autonomous_event, exact source coverage, candidate materialization, and immutable ContinuityChunk are in the existing Continuity modules.
- The producer discovers one active context/epoch. That is not a complete natural-day discovery path.
- The legacy chat-day helper has a 04:00 boundary. DayBlock instead uses the owner's midnight-to-midnight Asia/Shanghai calendar day and leaves chat-day behavior untouched.
- Primary chat authority is resolved by chat.provider_router.capture_generation_authority: CHAT_PROVIDER is canonical, GW_PROVIDER is compatibility fallback for that setting, and the selected provider's formal model resolver supplies model_identity. WAKE_PROVIDER, BACKGROUND_PROVIDER, UI model selection, and FALLBACK_PROVIDER do not select this authority.
- Legacy Auto Diary writes DIARY rows; Daily Summary writes DAILY_SUMMARY; Weekly Summary writes WEEKLY_SUMMARY. The posts table has no DayBlock provenance column. The diary.write and memory.write capabilities remain present and unchanged.

## Owner decisions recorded for DayBlock

1. The job targets the previous local calendar date. Its source range is the half-open interval [00:00 on source_day, 00:00 on the next local day).
2. Source discovery starts with daily_message_contexts ownership and reuses read_canonical_scope_rows for each overlapping context/epoch. It then filters canonical rows to that exact local-day interval. Any formal row not represented by a completed turn or canonical Wake blocks generation and is reported as uncovered.
3. Source units and revisions use existing Continuity completed_turn and autonomous_event contracts. Tool outcomes remain covered by turn evidence. Existing materialize_source_members renders exact raw members and checks current revisions. A chunk or prior summary is never accepted as the sole evidence.
4. The job freezes the formal Primary Generation Authority once. A replay of the same source revision reuses the saved provider, model, authority revision, and snapshot identity even if chat settings change. Failure/retry behavior must use that same authority and cannot cross providers.
5. DayBlock candidate identity includes identity/chat, source_day, source snapshot hash, source revision, policy, and frozen authority identity. A changed source revision gets a new candidate.
6. A generated DayBlock is written as an ordinary DIARY. Its source/evidence contract remains in the frozen job and source snapshot; the diary library has no artifact-to-post relation or canonical identity.
7. Every DIARY with a parseable created_at is automatically protected from background maintenance for 72 hours, regardless of whether it was written by DayBlock, Auto Diary, or an owner. Manual edits and deletes retain their existing behavior and do not reset the protection window.
8. ContinuityChunk remains short-term continuity compression; DayBlock remains a natural-day generation job with its own source-day evidence contract.
9. The prompt contract is versioned and hashed here as rules only. No prompt body is sent to a model in R0. Persona revision and an input budget must be frozen before a future generator can be ready.
10. This phase adds no schedule. The 03:00 rule is a pure function; missed runs name the missing source_day explicitly.

## Why the artifact is separate from ContinuityChunk

ContinuityChunk is keyed to a sealed source block and its generation job, and is intended for short-term continuity. DayBlock is keyed to one natural calendar day and keeps its own frozen source/job contract. Its generated text becomes an ordinary DIARY row; no separate artifact head or projection mapping is required. R0 reuses the source snapshot, exact-membership, raw-materialization, and primary-authority contracts in a small Preview-only planner schema.

## Preview-only schema

The Preview shadow store contains immutable source snapshots and members plus frozen shadow jobs. It has no diary projection, artifact mapping, current-head table, or post-mutation trigger. The 72-hour rule uses the existing DIARY type and created_at value and is applied only by automatic maintenance writers. This schema lives under Preview var/ and does not migrate memories.db.

## Explicit R0 blockers

- No DayBlock raw-input budget is defined, so the planner reports the measured token estimate but does not claim raw input fits.
- The prompt contract is rules-only and the generation-time persona revision is not frozen.
- Continuity's current raw materializer does not expand attachment payloads. If a source day contains attachments, planning records the count and blocks generation rather than pretending the attachment was materialized.
- A lone formal user message without a completed assistant turn is not silently dropped into a generated recap; it is counted as uncovered and blocks generation until a source representation is defined.
- R0 has no model executor call, scheduler, production write, Diary projection, or chat-context consumer.


## R1 generation-readiness handoff

Status: blocked; Preview-only planning, zero model calls, no Diary projection, cron change, or production write performed by the planner.

For source day 2026-09-25, the R1 replan records 86 exact source members: 78 completed turns, one canonical Wake event, one unmatched user turn, and six attachment references. The full raw evidence estimate is 61,402 tokens; generation input estimate is 74,696 tokens under `heuristic_cjk1_ascii4_v1`. All 86 members are covered by raw evidence; no matching ready Continuity chunks exist. The six image binaries are recorded by reference and hash but are not embedded as image input; their associated completed turns contain textual response context.

Persona bytes and R1 prompt contract are frozen in Preview. The existing job's Primary authority remains frozen as `claude_code` / `explicit:claude-opus-5-5`, captured at 12:00 on 2026-09-26. This cannot be represented as the required 03:00 capture, and the missed historical authority cannot be reconstructed. Anthropic documents a 1M context window and 128K maximum output for the exact Opus 5.5 model identity. The deterministic input budget therefore reserves the maximum output and uses 872,000 tokens. The current raw input estimate of 74,696 is below that budget under the project's existing `heuristic_cjk1_ascii4_v1` measurement; it is an estimate, not an exact provider tokenizer count. This model budget was confirmed after the current job's input plan had already been frozen with an unknown budget. The Preview immutable-provenance trigger correctly prevents retroactive mutation, so the stored job remains blocked and its frozen plan is left intact. New plans recognize the exact model budget. The earlier R0 job is stale because the exact DayBlock source membership expanded in R1; neither job is ready for generation.

The cleaner and planned DayBlock capture still both name 03:00. No cron was changed. Scheduling must remain a separate follow-up; future authority capture must be taken at the specified 03:00, and cleaner should be moved to a non-overlapping time in that separate slice. The next readiness attempt requires an authoritative budget for the exact frozen model and a properly timed future authority capture; it must not rewrite the existing frozen authority.


The Opus 5.5 budget mapping is grounded in Anthropic's [model specifications](https://platform.claude.com/docs/en/models/opus-5-5/overview). No context budget is inferred for other model identities or providers.


## R2 owner decisions (2026-10-21) — supersede R0 decisions 3, 8 and 10

The owner revised the DayBlock role. Where this section conflicts with the R0/R1 text above, this section wins.

1. **DayBlock enters chat context and doubles as the diary.** It is no longer a Diary-only artifact. Decision 8 ("ContinuityChunk and DayBlock stay separate; no chat-context consumer") is withdrawn.
2. **Evidence.** Raw source remains the primary evidence. READY chunks may be supplied as an outline. Decision 3 still forbids a chunk or prior summary from being the *sole* evidence.
3. **Context role (R2 slice 1, this change).** On cold/respawn/capacity builds, a READY DayBlock replaces every ContinuityChunk of the same natural day in the current window. Today and yesterday always stay on chunks. Budget order is unchanged: recent raw, then newest representations, with oldest-first trimming. Substitution happens only when the DayBlock snapshot covers the day's window members exactly (same `source_ref`, revision and content hash; DayBlock-only auxiliary members may be extra). Otherwise the day keeps its chunks. Refusal or too-short bodies (e.g. the 2026-10-13 receipt) never enter context. Hot turns see only DayBlocks already installed on the resident (receipt visibility), so a new DayBlock waits for the next cold start instead of forcing a respawn. Implementation: `continuity/dayblock_context.py`, hooked through `build_daily_continuity_shadow_plan(binding_transform=...)`.
4. **Gate.** Runtime flag `CONTINUITY_DAYBLOCK_CONTEXT_ENABLED` defaults to `0`. While off, non-hot builds only log `dayblock_context_observation` and keep their chunks.
5. **Store.** DayBlock moves out of Preview. The runner writes to `/var/lib/hayagarden/dayblock.db` (override with `HAYAGARDEN_DAYBLOCK_STORE`) and imports only its own checkout. Existing Preview receipts are migrated by an online SQLite backup of `/opt/frontend-preview/var/dayblock_shadow.db`.
6. **Planned slices.**
   - Slice 2: one 04:00 day boundary for chunk sealing, DayBlock source day and chat day, with DayBlock generated at 04:30. This replaces decision 10's 03:00.
   - Slice 3: the DayBlock is projected as the DIARY. An owner-edited DIARY version takes priority in context, so the owner can repair a failed or refused day. Diary injection into the system prompt and Auto Diary are retired.
   - Cross-window DayBlock carry-over is deferred; forge keeps current-window semantics.
