# Behavior Authority B3-0｜Renderer + Settlement Contract / Insertion Audit

Status: **Frozen-contract candidate / docs-only.**  
This file does not enable Renderer runtime, does not change production code, and does not modify B2 boundaries.

B2 is **closed** (`none` only, `BEHAVIOR_AUTHORITY_B2_CONSUMER_ENABLED` may be on in production). B3-0 answers one question:

> Where does Planner → Gate → executor → V3 Settlement actually run today, and where must Renderer insert so language Actions get Persona rendering without becoming a second Planner?

Upstream authority: Internal State V3 身心—念头—行动融合方案（Eventide × Pulse × Desire）｜2026-08-03 权威迁移合同修订版.

---

## 1. B3 goal and stop line

B3 adds **language rendering** and **Decision-time provenance Settlement** for owned language Actions, without:

- re-deciding Intent or Action;
- re-running Gate;
- reading raw Drives / Affect / Thought Pool in Renderer;
- inventing a second executor or Settlement stack;
- **removing, replacing, or degrading B2 `none` authority** (additive ownership only).

B3-0 is **contract + insertion audit only**. B3-1 is the first integrated **message takeover** slice (ownership + Gate + Renderer + executor + Settlement in one Draft PR).

### 1.1 Additive ownership (frozen)

Production today:

```text
existing B2 owned actions = { none }
```

B3 is **additive only**. B3-1 newly owns one language action:

```text
B3-1 newly-owned action = message only
effective owned actions after B3-1 = { none, message }
```

Implementation must expand allowlist incrementally, e.g.:

```text
_OWNED_ACTIONS = frozenset({'none', 'message'})
```

**Forbidden:**

```text
_OWNED_ACTIONS = frozenset({'message'})     # drops none
replace {none} with {message}
```

**Routing after B3-1 (both paths coexist):**

| Owned action | Path |
|--------------|------|
| `none` | **No Renderer** → existing B2 `none_takeover` → `wake.executor.execute('none', …)` |
| `message` | Gate ALLOW → **Renderer** → `wake.executor.execute('message', …, rendered_content)` |

**Rollback / flag rules:**

- B3 consumer flag OFF → legacy for non-owned actions; **B2 `none` authority must remain** when B2 consumer is ON.
- B3 rollback must not silently remove `none` from owned actions or revert `none` to legacy-only behavior.

B3-1 may incrementally add `message` ownership in the same PR as Renderer runtime, but must **not** modify B2 `none` semantics and must **not** add `diary` / `explore`.

---

## 2. Audited production path (current `main`)

### 2.1 Planner output (authoritative, not Shadow JSONL)

| Item | Location | Notes |
|------|----------|-------|
| Authoritative Decision | `chat.planner_shadow.run_authoritative_planner_decision()` | Sync; no JSONL read/write; `authoritative=True` |
| Shadow observation | `chat.planner_shadow.dispatch_planner_shadow()` | Non-authoritative; `shadow_only=True`; must not drive B3 |
| Gateway entry | `gateway._wake_decide_locked()` | After V freeze + system build |

**Decision fields (valid authoritative):**

| Field | Role |
|-------|------|
| `intent` | Planner intent (B3 Renderer `selected_intent` source) |
| `action_candidate` | Planner action (`none` \| `message` \| `diary` \| `explore`) |
| `primary_drive` | Decision-time drive; required when `action_candidate != 'none'` |
| `contributors` | Decision-time state factors (audit / provenance) |
| `blocked` | If true, `action_candidate` must be `none` |
| `reason_codes` | Planner reason tags |
| `captured_at` | Decision timestamp |
| `state_version` | Frozen V version |
| `wake_run_id` | Wake attempt identity |
| `decision_attempt_id` | Gateway-owned attempt id |
| `planner_decision_id` | Equals `decision_attempt_id` in production |
| `source` | `planner_authority` (B2+) vs `planner_shadow` (observation only) |

**Legacy Decision-time provenance (pre-B3 language path):**

| Item | Location |
|------|----------|
| Freeze | `wake.builder.inject_snippets()` → `drive_engine.freeze_decision_provenance(decision)` |
| Gateway handoff | `_wake_build_system_for_plan()` returns `decision_provenance` dict |
| Executor | `settle_fired_drive=decision_provenance['primary_drive']`, `settle_provenance_present=isinstance(decision_provenance, dict)` |

**B2 Planner provenance freeze:**

| Item | Location |
|------|----------|
| Freeze | `chat.behavior_authority_b2.freeze_provenance_from_planner_decision(decision)` |
| When | `plan_b2_wake_action()` route `none_takeover` only today |
| Fields | `source`, `captured_at`, `primary_drive`, `contributors`, `blocked`, `suggested_action`, `planner_decision_id`, `decision_attempt_id`, `state_version` |

### 2.2 B2 Gate boundary (must not be weakened)

| Stage | Location | Behavior |
|-------|----------|----------|
| Plan + Gate | `chat.behavior_authority_b2.plan_b2_wake_action()` | Consumer OFF → `legacy` |
| Ownership today (B2, production) | `B2_OWNED_ACTIONS = { none }` | Non-owned → `legacy` |
| Ownership established | Valid Planner + `action_candidate ∈ owned` | Post-ownership exceptions → `blocked` + `precondition_failed` (no gateway fail-open) |
| Gate ALLOW `none` | `gateway._wake_decide_locked` route `none_takeover` | Skips `get_wake_runner`; calls `wake.executor.execute('none', ...)` |
| Gate BLOCK | `route == 'blocked'` | Early return; **no** runner, **no** executor, **no** legacy |
| Fresh `chat_busy` | `chat_busy_fn=_chat_is_generating` read **after** Planner returns inside `plan_b2_wake_action` | B3 must preserve this ordering |

**B3 insertion must not:**

- run Renderer on Gate BLOCK;
- allow post-ownership Gate exceptions to fall through to legacy;
- move `desire_driven` freeze before legacy runner (legacy timing restored in B2 closure).

### 2.3 Legacy language path (current production for `message` / `diary` / `explore`)

```
gateway._wake_decide_locked()
  → _wake_build_system_for_plan()          # persona + drive snippet + legacy provenance
  → [B2 plan: none_takeover | blocked | legacy]
  → get_wake_runner(provider).run()      # model + tools loop
  → _parse_wake_response(raw_text)        # wake.parser.parse_response
  → wake.executor.execute(action, thoughts, content, ...)
```

**Text source today (legacy):**

| Action | Who picks Action | Who generates language | Executor |
|--------|------------------|------------------------|----------|
| `message` | Model in runner (`ACTION:` line) | Same model (`CONTENT:` line) | `wake.executor.execute` → `chat_messages` |
| `diary` | Model in runner | Same model (`CONTENT:` line) | `wake.executor.execute` → `posts` DIARY |
| `explore` | Model in runner | Tool loop in runner; summary in `CONTENT:` | `wake.executor.execute` → `wake_log` only (no chat/diary row) |
| `none` | Model or B2 Planner | `thoughts` only; `content` empty | `wake.executor.execute` → `wake_log` |

**Parser:** `wake.parser.parse_response()` — extracts `THOUGHTS` / `ACTION` / `CONTENT`; normalizes `send` → `message`.

**Contamination:** Legacy runner combines **Planner-like** choice (`ACTION`) and **Renderer-like** output (`CONTENT`) in one model call (`wake.runners.WAKE_CONTRACT` + `FORMAT_NUDGE`). B3 must **split** these for owned language actions without deleting legacy path until B3-1 flag slice is enabled.

### 2.4 Persona / rendering entry (audit)

| Entry | File / function | Role today |
|-------|-----------------|------------|
| Persona file | `chat.system_builder.read_persona()` → `/opt/frontend/prompts/persona.md` | BP1 persona text |
| Wake system | `chat.system_builder.build_system(wake=True, ...)` | Full prompt: persona + memories + drives snippet + relationship context |
| Relationship facts | `chat.relationship_context.build_relationship_context()` | Optional continuity text |
| Wake suffix | `wake.builder.build_prompt_suffix()` | Mode template (`THOUGHTS/ACTION/CONTENT` contract embedded) |
| Drive snippet | `wake.builder.inject_snippets()` | Legacy `decide()` snippet — **not** Renderer-safe (Drive→Action leakage) |
| Model invocation | `wake.runners.get_wake_runner().run()` | CC resident or API relay agent loop |

**Closest existing “how to say it” invocation:** legacy Wake runner structured output (`CONTENT:`), **not** chat `build_system()` for user turns.

**Reuse assessment:**

- **Persona read** (`read_persona()` / shared persona slot): **reusable** as Renderer allowlist input.
- **Full `build_system()` / `inject_snippets()` / `drive_engine.decide()` snippet:** **not reusable** — violates Renderer visibility contract (raw drives, second Decision).
- **WAKE_CONTRACT in runner:** **legacy combined Planner+Renderer** — B3 Renderer must not append ACTION selection to the same call.

### 2.5 Settlement (V3)

| Step | Location |
|------|----------|
| Trigger | `wake.executor.execute()` after successful surface writes, same `BEGIN IMMEDIATE` txn |
| Authority | `chat.drive_authority.apply_wake_outcome_on_conn()` |
| Event | `internal_state_events.apply_outcome()` → `event_key = wake_outcome:{wake_run_id}` |
| Join txn | `join_transaction=True` on Action connection |

**Provenance contract:**

| Parameter | Source today (legacy) | Source today (B2 `none`) |
|-----------|----------------------|--------------------------|
| `settle_provenance_present` | `isinstance(decision_provenance, dict)` from `inject_snippets` | Explicit `True` |
| `settle_fired_drive` | `decision_provenance['primary_drive']` | `planner_provenance['primary_drive']` |
| `executor_action` | Parsed `action` from model | `'none'` |
| `desire_action` | Always `None` in production executor | Same |

**Fail closed:**

- `provenance_present=False` → `StoreError`, txn rollback, no Action commit.
- `executor_action != 'none'` and missing `fired_drive` → `StoreError`.
- Settlement success statuses: `applied` \| `duplicate` \| `stale_skipped`; else `RuntimeError` → rollback.

**`none` settlement:** `primary_drive` may be `None`; fatigue restore via `plan_outcome_transition` — provenance object still required (`settle_provenance_present=True`).

**Duplicate / idempotency:** `event_key = wake_outcome:{wake_run_id}` via `internal_state_store.apply_conditional_state_update` — same key+payload → `duplicate`; conflicting payload → `idempotency_conflict`.

**B3 requirement:** Language Actions must pass **Planner Decision-time provenance** (`freeze_provenance_from_planner_decision` or equivalent frozen object), not re-read `drive_engine.decide()` after Renderer or infer `primary_drive` from rendered text.

### 2.6 Event Authority

| Mechanism | Exists? | Location |
|-----------|---------|----------|
| `wake_outcome` canonical event | **Yes** | `internal_state_events.apply_outcome`, type `wake_outcome` |
| Per-action `action_success` / `action_failure` self/world Event | **GAP** | No separate production writer found for language Action success/failure beyond `wake_outcome` |
| Idempotency | **Yes** | `event_key` + payload hash on `internal_state_events` |
| Source identity | **Yes** | `source_id = wake_run_id` |

B3-0 does **not** create a new Event framework. B3-1 failure paths should use existing `wake_outcome` absence (no txn commit) and/or production attempt marking — not invent parallel self/world Events without Owner decision.

---

## 3. Renderer insertion point (frozen audit conclusion)

**Single recommended insertion (gateway, live Wake only):**

```text
gateway._wake_decide_locked()
  …
  _b2_plan = plan_b2_wake_action(...)     # owned: none + message after B3-1
  if blocked → return                    # terminal; no Renderer
  if none_takeover → executor (existing B2 path; no Renderer)
  if message_takeover → Renderer → wake.executor.execute('message', …)
  else legacy → get_wake_runner() …       # non-owned actions only
```

**Precise slice:**

```text
After:  B2/B3 ownership + Gate ALLOW for owned `message`
Before: wake.executor.execute('message', thoughts, rendered_content, …)
Not in: get_wake_runner() loop, inject_snippets(), drive_engine.decide(), Shadow thread

Owned `none`: no Renderer; existing `none_takeover` path unchanged.
```

Renderer receives **frozen** Planner Decision fields + allowlisted Persona/continuity — not live V re-read.

---

## 3.1 B3-1 integrated slice boundary (frozen)

**B3-1 = one complete `message` takeover slice** in a single Draft PR:

```text
retain none ownership
+ add message ownership ({ none, message })
+ message Gate reality handling (user_active vs cooldown split)
+ Renderer runtime
+ existing wake.executor.execute('message', …)
+ existing V3 Settlement
```

**Why one PR:** `message` ownership without Renderer has no production value (executor requires non-empty `content`). Splitting ownership and Renderer into separate interim PRs only adds temporary states.

**B3-1 newly-owned action:** `message` only.

**Not in B3-1:** `diary`, `explore`, second language action, B2 Case expansion, new infrastructure.

---

## 3.2 Owned `message` Gate reality semantics (frozen for B3-1)

When `action_candidate = message` and B2/B3 ownership is established, Gate evaluates **independent reality facts** after Planner returns. Gate must **not** map merged `effective_idle_hours < threshold` alone to `user_active` for owned `message`.

### Known limitation (current `wake_guard_reason`)

Today `effective_idle = min(user_idle, wake_message_idle)` and `recent_interaction` does not distinguish user activity vs recent autonomous wake message. **B3-1 owned `message` Gate must not rely on that merged shortcut.**

Use separate facts from `InteractionClock` / fresh `chat_busy_fn`:

| Fact source | Fields |
|-------------|--------|
| User activity | `last_user_at`, `user_idle_hours`, `chat_busy_fn()` |
| Wake message spacing | `last_wake_message_at`, wake-message idle hours |
| Duplicate | `wake_run_id_seen(wake_run_id)` |
| Capability | frozen `CapabilitySkillView.resolved_action_capability` |

### Block reason definitions (owned `message`)

| Reason | When | Notes |
|--------|------|-------|
| `duplicate` | `wake_run_id` already consumed for this attempt | Same as B2 |
| `user_active` | `chat_busy_fn()` is true **OR** `user_idle_hours < user_activity_idle_floor` | True user/chat activity only |
| `tool_unavailable` | `message` not in resolved capability allowlist | Same pattern as B2 |
| `cooldown` | User **not** `user_active` (per above), but autonomous wake `message` sent too recently: `wake_message_idle < message_cooldown_floor` | **Not** `user_active` |
| `precondition_failed` | Clock unreliable, ownership pairing failure, Gate evaluation exception after ownership | Fail closed |

**`user_active` means:** chat is generating, or latest real **user** interaction is within the user-activity idle floor (`WAKE_MIN_IDLE_MINUTES` / `min_idle_minutes` passed to Gate).

**`cooldown` means:** user is **not** actively chatting and user idle floor is satisfied, but `last_wake_message_at` is too recent for another unsolicited autonomous `message` (wake-message idle below `message_cooldown_floor`).

**Critical:** recent `last_wake_message_at` alone → `cooldown`, **not** `user_active`, when user idle floor is satisfied and `chat_busy` is false.

If user is genuinely active **and** message is in cooldown simultaneously → still:

```text
BLOCK / user_active
```

(priority below).

### Reason priority (unchanged)

```text
duplicate
→ user_active
→ tool_unavailable
→ cooldown
→ precondition_failed
```

Gate must **not** read Drive / Affect / Bond to relax cooldown or user-active floors.

**`none` Gate:** keeps existing B2 semantics (including `none` always allowed in capability union); B3-1 does not redefine `none` Gate beyond preserving current behavior.

---

## 4. Settlement insertion point (frozen audit conclusion)

**No new Settlement insertion.** Reuse existing single path:

```text
wake.executor.execute(
  action, thoughts, rendered_content, …,
  settle_fired_drive=planner_provenance['primary_drive'],
  settle_provenance_present=True,
  wake_run_id=…,
)
  → apply_wake_outcome_on_conn (same txn)
```

Renderer success alone must **not** call Settlement. Only executor txn success (deliver + settle statuses) commits `wake_outcome`.

---

## 5. Frozen Renderer contract

### 5.1 Renderer input (concept → existing names)

| Concept field | Frozen existing name / source | Notes |
|---------------|------------------------------|-------|
| `selected_intent` | `intent` | From authoritative Planner Decision |
| `selected_action` | `action_candidate` | After Gate ALLOW; immutable |
| `content_target` | **NAMING_DECISION_REQUIRED** | Recommended freeze: `content_target` enum (`wake_message` \| `wake_diary`) mapping to executor surface |
| `persona_context` | Persona text from `read_persona()` or frozen BP1 persona slot | Not full `build_system()` |
| `continuity_facts` | **NAMING_DECISION_REQUIRED** | Recommended freeze: `continuity_facts` as short allowlisted text blob; source may include trimmed `relationship_context` — not full Internal State View |
| `decision_identity` | `wake_run_id`, `decision_attempt_id`, `planner_decision_id`, `state_version`, `captured_at` | Bind Renderer output to one attempt |

Do not introduce parallel names (`selected_intent` as a new DB column) in B3-0; map concepts to Planner Decision dict keys at runtime in B3-1.

### 5.2 Renderer output

| Concept | Frozen recommendation |
|---------|----------------------|
| Language payload | **NAMING_DECISION_REQUIRED** — recommended runtime name: `rendered_content` (maps to executor `content` parameter and legacy `CONTENT:` semantics) |

Renderer returns **only** rendered text (and optional non-authoritative diagnostics). Must not return:

- new `intent` / `action_candidate`
- Gate verdict or reason
- drive / affect / thought mutations
- Settlement instructions
- tool choices or next-action suggestions

### 5.3 Renderer visibility allowlist

**May read:**

- Persona (`prompts/persona.md` or equivalent frozen BP1 persona string)
- `intent`, `action_candidate` (frozen)
- `content_target` (surface hint)
- `continuity_facts` (short, pre-approved text — e.g. relationship snippet, mode label, non-numeric continuity)
- `decision_identity` (correlation only; not for re-planning)

**Must not read:**

- Raw eight drives numeric vector as planning input
- Raw Affect PA/NA/V/A / `mood_word` as numeric planning input
- Full Thought Pool / Trace pools
- Full `PlannerStateView` / `internal_state_v3` row
- `CapabilitySkillView` for re-selecting action
- Gate block reasons as “try another action” hints
- `drive_engine.decide()` or `inject_snippets()` output
- Shadow JSONL observations

If a continuity fact is derived from V3, it must be **pre-digested to short text** before Renderer — no numeric state re-injection.

---

## 6. Frozen Settlement contract (B3)

### 6.1 Input (concept → existing)

| Concept | Existing production parameter / field |
|---------|-------------------------------------|
| `executed_action` | `executor_action` / `wake.executor.execute(action=…)` |
| `execution_result` | Executor txn outcome (`delivered`, `settled`, `settle_status`) |
| Decision-time provenance | `freeze_provenance_from_planner_decision()` object → `settle_fired_drive`, `settle_provenance_present=True` |
| Identity | `wake_run_id` → `event_key = wake_outcome:{wake_run_id}` |

### 6.2 Provenance rules

- **Must** come from Planner Decision-time freeze (B2 function or successor — same field semantics).
- **Forbidden:** post-executor `drive_engine.decide()`; inferring `primary_drive` from `rendered_content`; fixed Action→drive mapping; reading latest Affect to guess Intent.

### 6.3 Success vs failure

| Stage | Settlement |
|-------|------------|
| Renderer produced text | **No** Settlement |
| Executor txn committed (`applied` / `duplicate` / `stale_skipped`) | **Yes** (existing path) |
| Renderer error / timeout | **No** executor, **No** Settlement, **No** legacy replan |
| Executor error / rollback | **No** Settlement; no success self-event |

### 6.4 Idempotency

Reuse `wake_outcome:{wake_run_id}` — **do not** add a second settlement key for the same attempt. If insufficient for Renderer retry semantics: register **GAP** in B3-1, do not build new state machine in B3-0.

---

## 7. Hard Vetoes (design FAIL)

- Renderer changes Planner Action or Intent
- Renderer reads full Internal State or Drive vector to pick wording tied to drive levels
- Persona prompt contains fixed Drive→台词 mapping tables
- Gate BLOCK → Renderer
- Renderer failure → legacy runner re-decides Action
- Executor failure → legacy retries same owned Action
- Rendered text success → Settlement without executor commit
- Settlement infers `primary_drive` from text or post-action state
- Duplicate `wake_outcome` or parallel self/world Events for one success
- Second Persona stack, second executor, or second Settlement framework for B3
- New CI / runner / canary / monitor solely for B3
- Track C Chat Exposure bundled into B3
- Re-opening B2 Case expansion or `explore` executor invention in B3-0/B3-1
- Removing `none` from owned actions or routing owned `none` through Renderer
- B3 flag/rollback that drops B2 `none_takeover` while B2 consumer remains enabled

---

## 8. RECOMMENDED_B3_1_SLICE

**B3-1 newly-owned action:** `message` only.

**Effective owned actions after B3-1:** `{ none, message }` (additive; `none` unchanged).

**Why `message` (unique first language slice):**

1. Real executor exists (`wake.executor.execute` → `chat_messages`, `source_kind='wake'`).
2. Clear Settlement (`wake_outcome`, requires `primary_drive` — exercises full provenance path).
3. No new tool infrastructure (unlike `explore`, whose research happens in runner tool loop).
4. Full chain Planner → Gate → Renderer → executor → Settlement without new surfaces.
5. User-visible but flag-gated and revertible (mirror B2 consumer flag pattern).
6. `diary` deferred — second surface (`posts`); not in B3-1.
7. `explore` **rejected** — no independent post-Planner explore executor; would require new tool loop (B2-0 audit confirmed).

**B3-1 one integrated PR:** message ownership + message Gate semantics + Renderer + executor + Settlement (see §3.1). Extend `diary` / `explore` in later slices only.

---

## 9. Frozen B3-1 minimum acceptance (three cases only)

### Case 1｜Language happy path

Valid Planner `action_candidate=message` → Gate ALLOW → Renderer emits text only → `wake.executor.execute('message', …, rendered_content)` succeeds → Settlement consumes **original** Planner Decision-time provenance (`planner_authority` source).

### Case 2｜Renderer failure safe stop

Planner ownership established + Gate ALLOW → Renderer timeout / invalid / error → **stop** (mirror B2 `blocked` terminal semantics): no legacy Action re-pick, no Settlement, no success Event. May mark production attempt `failed` with renderer reason (existing shadow outcome path).

### Case 3｜Executor failure no forged success

Renderer succeeds → executor fails (validation, txn, settlement) → no success Settlement, no `wake_outcome` commit, rendered text not treated as delivered reality, no legacy redo of same owned `message`.

No Case 4–6 in B3-1.

---

## 10. Explicit non-goals (B3-0 / B3-1 boundary)

- B2 `none` Case-family expansion, canary, or monitoring (B2 is closed; **`none` authority must remain**)
- `diary` / `explore` takeover in B3-1
- New Event schema or self/world action Event framework
- Gateway behavior change in B3-0 (this document only)
- Production flag change in B3-0
- Track A / Track C
- Legacy Wake runner removal in B3-1

---

## 11. NAMING summary

### Frozen existing names (use in B3-1)

`intent`, `action_candidate`, `primary_drive`, `contributors`, `blocked`, `reason_codes`, `captured_at`, `state_version`, `wake_run_id`, `decision_attempt_id`, `planner_decision_id`, `source` (`planner_authority`), `settle_fired_drive`, `settle_provenance_present`, `executor_action`, `freeze_provenance_from_planner_decision`, `wake_outcome:{wake_run_id}`, `CONTENT` (legacy parser field only — Renderer output maps to executor `content`).

### NAMING_DECISION_REQUIRED (single recommendation each)

| Concept | Recommended B3-1 name |
|---------|---------------------|
| Renderer input bundle | `RendererInput` (dataclass or typed dict in new module) |
| Language output | `rendered_content` |
| Surface hint | `content_target` (`wake_message` \| `wake_diary`) |
| Short continuity blob | `continuity_facts` (plain text, allowlist-curated) |

Do not implement alternate aliases in parallel.

---

## 12. BLOCKED check

Insertion points for Renderer and Settlement are **uniquely determined** from current `main`:

- Renderer: gateway post-Gate ALLOW branch before `wake.executor.execute`
- Settlement: existing `wake.executor.execute` → `apply_wake_outcome_on_conn`

No BLOCKED. Event Authority has a **GAP** for discrete action success/failure Events — documented, not blocking B3-1 `message` slice.

**B3-0 RESULT: PASS** (audit complete; B3-1 message takeover contract ready for implementation).

**Post narrow-fix checklist:**

- Effective ownership: `{ none, message }` — not `{ message }` alone.
- `none` → no Renderer → existing B2 `none_takeover`.
- `message` → Gate ALLOW → Renderer → executor → Settlement.
- Owned `message` Gate uses separate `user_active` vs `cooldown` facts (not merged `effective_idle → user_active` only).
