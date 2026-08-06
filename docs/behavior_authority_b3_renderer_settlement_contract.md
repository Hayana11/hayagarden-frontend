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

Production today (B2 code):

```text
B2_OWNED_ACTIONS = { none }
BEHAVIOR_AUTHORITY_B2_CONSUMER_ENABLED  # default 0, fail-safe OFF
```

B3 adds language ownership **only when B3 consumer is also enabled** (see §1.2). B3-1 newly owns:

```text
B3_OWNED_ACTIONS = { message }
B3-1 newly-owned action = message only
```

**Do not** implement a single static allowlist such as `_OWNED_ACTIONS = frozenset({'none', 'message'})` without flag pairing — owned actions and flags must match.

**Forbidden:**

```text
_OWNED_ACTIONS = frozenset({'message'})     # drops none
replace {none} with {message}
B3 message owned while B3 consumer flag is OFF
```

**Routing when flags and ownership align (B2=1, B3=1):**

| Action | Owned by | Path |
|--------|----------|------|
| `none` | B2 | **No Renderer** → `none_takeover` → `wake.executor.execute('none', …)` |
| `message` | B3 | Gate ALLOW → **Renderer** → `wake.executor.execute('message', …, rendered_content)` |
| other | — | legacy `get_wake_runner()` |

B3-1 may add `message` ownership + Renderer in one PR, but must **not** modify B2 `none` semantics and must **not** add `diary` / `explore`.

### 1.2 B2 × B3 consumer flags (frozen truth table)

B3 is an **incremental layer on B2 Planner/Gate authority**. B3 cannot operate alone.

**New flag (B3-1):**

```text
BEHAVIOR_AUTHORITY_B3_CONSUMER_ENABLED
default = 0
fail-safe → OFF (missing / parse error → OFF)
```

**Effective enablement:**

```text
effective_b3_enabled = B2_consumer_enabled() AND B3_consumer_enabled()
```

B2 flag OFF → entire `plan_b2_wake_action()` returns `legacy` today; B3 has no authority path.

| `BEHAVIOR_AUTHORITY_B2_CONSUMER_ENABLED` | `BEHAVIOR_AUTHORITY_B3_CONSUMER_ENABLED` | Production routing |
|------------------------------------------|------------------------------------------|-------------------|
| 0 | 0 | **legacy** for all actions |
| 1 | 0 | **B2 `none` owned** → `none_takeover` or `blocked`; **`message` → legacy** |
| 1 | 1 | **B2 `none` owned** + **B3 `message` owned** (Renderer path) |
| 0 | 1 | **fail-safe:** treat as B3 disabled → **`message` → legacy**; no B2 authority |

**Rollback:**

- B3 flag OFF (B2 still ON) → `message` returns to legacy; **`none` B2 authority unchanged**.
- B2 flag OFF → all Behavior Authority consumer paths off; full legacy Wake.

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
  _b2_plan = plan_b2_wake_action(...)     # B2 none; B3 message when effective_b3_enabled
  if blocked → return                    # terminal; no Renderer
  if none_takeover → executor (existing B2 path; no Renderer)
  if message_takeover → Renderer → wake.executor.execute('message', …)
  else legacy → get_wake_runner() …       # non-owned actions only
```

**Precise slice:**

```text
After:  B2/B3 ownership + Gate ALLOW for owned `message`
Before: wake.executor.execute('message', str(intent), rendered_content, …)
Not in: get_wake_runner() loop, inject_snippets(), drive_engine.decide(), Shadow thread

Owned `none`: no Renderer; existing `none_takeover` path unchanged.
```

Renderer receives **frozen** Planner Decision fields + allowlisted Persona/continuity — not live V re-read.

---

## 3.1 B3-1 integrated slice boundary (frozen)

**B3-1 = one complete `message` takeover slice** in a single Draft PR:

```text
retain B2 none ownership (B2 flag semantics unchanged)
+ add B3 message ownership (only when effective_b3_enabled)
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

When `action_candidate = message`, B3 ownership is established (`effective_b3_enabled`), and Gate runs after Planner returns. Gate must **not** map merged `effective_idle_hours < threshold` alone to `user_active` for owned `message`.

### Known limitation (current `wake_guard_reason`)

Today `effective_idle_hours = min(user_idle, wake_message_idle)` and `recent_interaction` does not distinguish user activity vs recent autonomous wake message. **B3-1 owned `message` Gate must not rely on that merged shortcut.**

Use separate facts from `InteractionClock` / fresh `chat_busy_fn`. **All idle comparisons use minutes** (`min_idle_minutes` from `WAKE_MIN_IDLE_MINUTES`; `InteractionClock.user_idle_minutes` property exists).

### Mode scope (do not change special-mode behavior)

Apply the user/cooldown split **only** for modes that already use the idle floor in `wake_guard_reason` today: `normal`, `morning` (and empty mode default). Modes that bypass the idle floor (`self_trigger`, `nightwatch`, `ritual`, `dream`, `summarize`, etc.) must **not** gain new B3 idle-floor behavior in B3-1.

### Frozen evaluation (owned `message`, idle-floor modes only)

Let `min_idle_minutes` = existing config (`WAKE_MIN_IDLE_MINUTES`, gateway-passed floor).

```text
user_active :=
    chat_busy_fn() is true
    OR clock.user_idle_minutes < min_idle_minutes

wake_message_idle_minutes :=
    if clock.last_wake_message_at is None → no wake-message cooldown
    else (now - clock.last_wake_message_at).total_seconds() / 60.0

cooldown :=
    user_active is false
    AND wake_message_idle_minutes is not None
    AND wake_message_idle_minutes < min_idle_minutes
```

**No new cooldown config.** `message_cooldown_floor` **reuses** `min_idle_minutes`. B3-1 only **splits** the legacy combined test:

```text
min(user idle, wake-message idle) < min_idle_minutes  →  recent_interaction
```

into:

```text
user < min_idle_minutes        → user_active
wake message < min_idle_minutes → cooldown   (only when user not active)
```

Time behavior unchanged; reason classification corrected.

### Block reason mapping

| Reason | When |
|--------|------|
| `duplicate` | `wake_run_id_seen(wake_run_id)` |
| `user_active` | `user_active` predicate above |
| `tool_unavailable` | `message` not in `resolved_action_capability` |
| `cooldown` | `cooldown` predicate above |
| `precondition_failed` | Clock unreliable (`not clock.reliable`), ownership/Gate exception after ownership, etc. |

**Critical:** recent `last_wake_message_at` alone → `cooldown`, **not** `user_active`, when `user_active` is false.

If `user_active` and `cooldown` would both apply → still:

```text
BLOCK / user_active
```

### Reason priority (unchanged)

```text
duplicate
→ user_active
→ tool_unavailable
→ cooldown
→ precondition_failed
```

Gate must **not** read Drive / Affect / Bond to relax floors.

**`none` Gate:** keeps existing B2 semantics; B3-1 does not redefine `none` Gate.

**Duplicate:** remains **before** Renderer and executor (`wake_run_id_seen`); no second idempotency state machine in B3-0/B3-1.

---

## 4. Settlement insertion point (frozen audit conclusion)

**No new Settlement insertion.** Reuse existing single path:

```text
wake.executor.execute(
  'message',
  str(planner_decision['intent'] or ''),   # thoughts — see §5.4
  rendered_content,
  …,
  settle_fired_drive=planner_provenance['primary_drive'],
  settle_provenance_present=True,
  wake_run_id=…,
)
  → apply_wake_outcome_on_conn (same txn)
```

Renderer success alone must **not** call Settlement. Only executor txn success (deliver + settle statuses) commits `wake_outcome`.

---

## 5. Frozen Renderer contract

### 5.1 `RendererInput` (frozen bundle name and fields)

Runtime bundle name: **`RendererInput`** (dataclass or typed dict in B3 module).

| Field | Frozen source | Notes |
|-------|---------------|-------|
| `selected_intent` | `planner_decision['intent']` | Immutable after Gate ALLOW |
| `selected_action` | `planner_decision['action_candidate']` | Must be `message` for B3-1 |
| `content_target` | **`wake_message`** for B3-1 | Frozen enum value for B3-1 slice (`wake_diary` reserved for later) |
| `persona_context` | `read_persona()` or frozen BP1 persona string | Not full `build_system()` |
| `continuity_facts` | `build_relationship_context(get_db_fn).text` only | See §5.4 |
| `decision_identity` | `wake_run_id`, `decision_attempt_id`, `planner_decision_id`, `state_version`, `captured_at` | Correlation only |

Map to Planner Decision dict keys at runtime; do not add parallel DB columns in B3-1.

### 5.2 Renderer output (frozen)

Output field name: **`rendered_content`** (str).

Renderer returns **only** `rendered_content` (plus optional non-authoritative diagnostics). Must not return:

- new `intent` / `action_candidate`
- Gate verdict or reason
- drive / affect / thought mutations
- Settlement instructions
- tool choices or next-action suggestions
- hidden `thoughts` text (thoughts come from Planner `intent` at executor — §5.4)

### 5.3 Renderer visibility allowlist

**May read:**

- Persona (`read_persona()` / frozen BP1 persona string)
- `intent`, `action_candidate` (frozen)
- `content_target` (`wake_message` in B3-1)
- `continuity_facts` from `build_relationship_context(...).text` only
- `decision_identity` (correlation only)

**Must not read:**

- Raw eight drives numeric vector
- Raw Affect PA/NA/V/A / `mood_word` as numeric input
- Full Thought Pool / Trace pools
- Full `PlannerStateView` / `internal_state_v3` row
- `CapabilitySkillView` for re-selecting action
- Gate block reasons as replan hints
- `drive_engine.decide()` or `inject_snippets()` output
- Shadow JSONL observations
- V3 / Affect / Thought Pool / raw memory buckets beyond the single `continuity_facts` text contract

### 5.4 Executor wire contract (frozen)

B3-1 `message_takeover` executor call:

```text
thoughts = str(planner_decision['intent'] or '')
content  = rendered_content
```

- Planner has no separate `thoughts` field; Renderer does **not** generate thoughts.
- Same semantics as B2 `none`: intent is audit text in `wake_log.thoughts`.
- Renderer generates **only** `rendered_content` for `chat_messages.content`.

**`continuity_facts` source (frozen):**

```text
continuity_facts = build_relationship_context(get_db_fn).text
```

That helper’s existing contract: relationship facts / recent continuity only; filters imperative coaching phrases; final `.text` does not inject mood numerics; length capped (400 chars). **No** alternate “may include” sources in B3-1.

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

**Effective owned actions when B2=1 and B3=1:** `{ none, message }` (additive; `none` unchanged).

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

Valid Planner `action_candidate=message` → Gate ALLOW → Renderer emits `rendered_content` only → `wake.executor.execute('message', str(intent), rendered_content, …)` succeeds → Settlement consumes **original** Planner Decision-time provenance (`planner_authority` source).

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

## 11. Frozen names (B3-1)

### Planner / Settlement (existing — reuse)

`intent`, `action_candidate`, `primary_drive`, `contributors`, `blocked`, `reason_codes`, `captured_at`, `state_version`, `wake_run_id`, `decision_attempt_id`, `planner_decision_id`, `source` (`planner_authority`), `settle_fired_drive`, `settle_provenance_present`, `executor_action`, `freeze_provenance_from_planner_decision`, `wake_outcome:{wake_run_id}`.

Legacy parser field `CONTENT` maps to executor `content`; B3 uses `rendered_content` until executor call.

### B3 Renderer / flags (frozen in B3-1)

| Name | Role |
|------|------|
| `RendererInput` | Renderer input bundle |
| `rendered_content` | Renderer output text |
| `content_target` | Surface hint; B3-1 value `wake_message` |
| `continuity_facts` | `build_relationship_context(...).text` |
| `BEHAVIOR_AUTHORITY_B3_CONSUMER_ENABLED` | B3 consumer flag; default `0` |
| `B2_OWNED_ACTIONS` | `{ none }` — unchanged |
| `B3_OWNED_ACTIONS` | `{ message }` |
| `effective_b3_enabled` | `B2_consumer_enabled() AND B3_consumer_enabled()` |

### Executor wire (frozen)

```text
thoughts = str(planner_decision['intent'] or '')
content  = rendered_content
```

Do not introduce alternate aliases for the above in B3-1.

---

## 12. BLOCKED check

Insertion points for Renderer and Settlement are **uniquely determined** from current `main`:

- Renderer: gateway post-Gate ALLOW branch before `wake.executor.execute`
- Settlement: existing `wake.executor.execute` → `apply_wake_outcome_on_conn`

No BLOCKED. Event Authority has a **GAP** for discrete action success/failure Events — documented, not blocking B3-1 `message` slice.

**B3-0 RESULT: PASS** (frozen contract; B3-1 message takeover ready for implementation).

**Frozen contract checklist:**

- B2/B3 flag truth table + `BEHAVIOR_AUTHORITY_B3_CONSUMER_ENABLED` (default OFF).
- `B2_OWNED_ACTIONS = { none }`; `B3_OWNED_ACTIONS = { message }`; `effective_b3_enabled = B2 AND B3`.
- Owned `message` Gate: minutes + `min_idle_minutes` for both user_active and cooldown; idle-floor modes only.
- Frozen names: `RendererInput`, `rendered_content`, `content_target`, `continuity_facts`; no `NAMING_DECISION_REQUIRED`.
- Executor: `thoughts = intent`; `continuity_facts = build_relationship_context(...).text`.
- `none` → no Renderer; `message` → Renderer → executor → Settlement when B2=1 and B3=1.
