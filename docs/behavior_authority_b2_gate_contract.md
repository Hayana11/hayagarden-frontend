# Behavior Authority B2-0｜Action Gate + none-only 局部接管合同

Status: **Frozen-contract candidate / docs-only.**  
This file does not enable Behavior Authority and does not change production code.

B1-2 has graduated: Planner can form structured `intent + action_candidate + Decision-time provenance`, and natural accepted pairs support **state modifies tendency, not command**. B2 therefore starts from the smallest real authority transfer instead of redesigning Wake.

---

## 1. B2 goal and stop line

B2 answers one question:

> Can a valid Planner Decision own a deliberately tiny Action family, pass through a deterministic reality Gate, and prevent legacy Decision from acting behind its back?

B2 is **not** the Renderer phase and is **not** a new Action/Settlement framework.

Initial production ownership is frozen to:

```text
B2_OWNED_ACTIONS = { none }
```

This is intentional. Static audit of the current executor chain shows no existing non-`none` action that can be taken over without pulling B3 semantics forward:

- `message` requires non-empty `CONTENT`; generating that content belongs to Persona/Renderer.
- `diary` requires non-empty `CONTENT`; same problem.
- `explore` is not a standalone executor action today. Real exploration happens earlier inside the legacy Wake model/tool loop; `wake.executor.execute(action='explore', ...)` mainly records the chosen result and applies Settlement. Taking `explore` over now would require inventing a new post-Planner execution/tool loop.

Therefore:

> **B2 stops at `none` takeover unless the repository later contains a genuinely self-contained non-language Action. Do not create one merely to satisfy B2.**

No `message`, `diary`, `explore`, social post, new tool action, new Renderer, new executor family, new monitoring system, or Track A/C work belongs in B2-0/B2 none takeover.

---

## 2. Frozen responsibility boundary

The authority split is:

```text
Planner
  → what do I want to do?
  → emits intent + action_candidate + Decision-time provenance

Action Gate
  → is that exact candidate allowed in reality now?
  → emits ALLOW or BLOCK + one deterministic reason

Persona / Renderer (B3)
  → when language is required, how is the already-selected Action expressed?

V3 Settlement
  → after a real/accepted Action outcome, how does authoritative state change?
```

Hard invariant:

> **State modifies tendency, not command.**

The Gate must never become a second Planner.

Forbidden examples:

```text
attachment high → relax cooldown
longing high → turn explore into message
curiosity high → force explore
libido high → permit an otherwise blocked action
Bond high → override user activity
```

---

## 3. Action Gate output contract

The Gate has only two outcomes:

```text
ALLOW
BLOCK
```

It also returns exactly one primary reason code.

Frozen first-version reasons:

```text
ok

tool_unavailable
user_active
cooldown
duplicate
precondition_failed
```

Rules:

- `ALLOW` → reason is `ok`.
- `BLOCK` → reason is one of the five block reasons above.
- Gate does not rewrite `intent`.
- Gate does not rewrite `action_candidate`.
- Gate does not rewrite `primary_drive` / contributors / provenance.
- Gate does not generate user-visible text.
- Gate does not call tools.
- Gate does not execute an Action.
- Gate does not perform Settlement.

If several block facts are simultaneously true, choose deterministically using this priority:

```text
duplicate
→ user_active
→ tool_unavailable
→ cooldown
→ precondition_failed
```

This priority is diagnostic only; it must not change Planner meaning.

---

## 4. What the Gate may read

Gate inputs are **reality constraints**, not motivational scores.

Allowed classes:

- exact Planner `action_candidate` identity;
- current ownership allowlist;
- resolved capability/tool availability;
- fresh user/chat activity fact;
- duplicate/idempotency fact;
- cooldown fact;
- explicit binary precondition/safety fact required by the chosen Action.

Forbidden as Gate scoring inputs:

- raw or ranked Drive values;
- Affect score;
- Bond score;
- Longing score;
- Planner confidence as an excuse to loosen reality constraints;
- free-form personality interpretation.

The upper architecture allows hard safety/fatigue constraints at the Gate. In B2 this does **not** authorize the Gate to re-read continuous psychological state and score it again. If a later owned Action truly requires a hard safety predicate, that predicate must arrive as an explicit deterministic precondition and fail as `precondition_failed`; it must not become another Drive→Action calculation.

---

## 5. Routing and ownership contract

There are three different situations; they must not be conflated.

### 5.1 B2 consumer flag OFF

```text
Planner Shadow / observation may continue as configured
→ production behavior remains legacy Decision
→ legacy Action / existing Settlement unchanged
```

Disabling the B2 consumer is the cheap rollback.

**V3 State Authority does not roll back.**

### 5.2 Valid Planner Decision chooses a non-owned action

Initial allowlist owns only `none`.

Therefore a valid Planner Decision such as:

```text
action_candidate = message / diary / explore
```

is not a Gate BLOCK. B2 has not claimed that family yet.

Result:

```text
not owned by B2
→ continue legacy Decision path
```

No Gate reason `not_owned` is introduced because ownership routing is outside the Gate contract.

### 5.3 Valid Planner Decision chooses an owned action

For B2 initial rollout:

```text
action_candidate = none
→ B2 owns this attempt
→ evaluate deterministic Gate
```

Then:

```text
Gate ALLOW
→ execute the owned none path

Gate BLOCK
→ stop as no Action
→ DO NOT fall back to legacy Decision
→ DO NOT let legacy perform message / diary / explore behind the Gate
```

Hard invariant:

> **Once B2 claims an owned candidate, Gate BLOCK is terminal for that attempt. There is no legacy back door.**

---

## 6. Planner unavailable / invalid is not Gate BLOCK

A missing, timed-out, unparsable, or schema-invalid Planner result means there is no valid B2 Decision to own.

That condition is handled before the Gate:

```text
no valid Planner Decision
→ no B2 ownership claim
→ legacy Decision may continue
```

This preserves availability while the takeover surface is only `none`.

It must not be logged as `Gate BLOCK`, because the Gate never received a valid candidate.

Conversely, once a valid Planner Decision has selected owned `none`, later Gate BLOCK must **not** fall back to legacy.

---

## 7. Production Planner result requirement

B1 observation rows are explicitly `shadow_only=true`. B2 must not treat an old JSONL observation or a record that still claims `shadow_only=true` as production authority.

B2 implementation may reuse the same Planner model, schema validator, and frozen `PlannerStateView + CapabilitySkillView`, but the attempt that actually drives takeover must be a current-attempt validated **production Planner Decision**.

Frozen semantics:

- same current `wake_run_id` / attempt;
- same frozen Decision-time state/capability facts;
- Decision exists before B2 can suppress legacy execution;
- no stale observation lookup;
- no Action-posterior promotion;
- no second V3 state read to "refresh" motivation after Decision freeze.

Concurrency/await implementation is not frozen here. The minimum implementation must not build a new worker/queue/service merely to optimize B2 latency.

---

## 8. `none` takeover semantics

`none` means no external user-visible Action, but the current authoritative system still has an existing internal outcome semantics.

Current V3 Settlement behavior for executor `none` restores fatigue by the existing fixed amount (`-0.04`, clamped) and is already part of State Authority.

Therefore B2 must **not** silently replace the established `none` outcome with a bare early `return`.

### Gate ALLOW

For owned `none`:

```text
Planner Decision-time provenance
+ action = none
+ existing wake_run_id / interaction-clock facts
→ existing wake.executor none path
→ existing V3 Settlement semantics
```

Requirements:

- no Renderer;
- no user-visible message;
- no diary/post;
- no tool call;
- no new Settlement algorithm;
- use Planner Decision-time provenance, never infer provenance from final `none`;
- preserve existing idempotency/transaction rules.

This is reuse of the already-authoritative Settlement, not B3 Settlement redesign.

### Gate BLOCK

If reality changed after Decision (for example user became active), then:

```text
Gate BLOCK
→ no executor Action
→ no none Settlement
→ no legacy fallback
```

A blocked candidate did not happen and must not receive an Action outcome settlement.

---

## 9. Fresh user-activity rule

The early Wake guard remains useful for eligibility, but B2 Gate must be allowed to use a **fresh deterministic activity fact** immediately before committing an owned candidate.

Reason:

```text
Planner Decision freezes
↓
user returns / chat becomes active
↓
old Decision is now stale for autonomous behavior
```

For initial `none` ownership this is still meaningful because applying the existing `none` Settlement would mutate fatigue after the user has already returned.

So:

```text
fresh user/chat activity detected
→ BLOCK / user_active
→ no Settlement
```

This is a reality check, not a motivational re-score.

---

## 10. Non-`none` audit conclusion

The repository audit for B2 is closed with this decision:

| Candidate | Current reality | B2 decision |
|---|---|---|
| `none` | no external content/tool execution; existing audit/Settlement path already exists | **OWN in B2** |
| `message` | executor requires non-empty content | **DEFER B3 Renderer** |
| `diary` | executor requires non-empty content | **DEFER B3 Renderer** |
| `explore` | exploration occurs inside legacy model/tool loop before parsed Action; executor is not a standalone explorer | **DEFER; do not invent executor in B2** |

Stop condition reached.

Do not continue searching for another B2 action and do not build a new action merely for testing.

---

## 11. Minimum implementation shape

B2 implementation should be as small as possible:

1. One cheap B2 consumer feature flag, default OFF.
2. Reuse frozen `PlannerStateView` and resolved `CapabilitySkillView`.
3. Produce one current-attempt validated production Planner Decision.
4. Ownership routing:
   - non-owned candidate → legacy path;
   - owned `none` → Gate.
5. Deterministic Gate as a pure/small decision function.
6. Gate ALLOW `none` → existing executor/Settlement with Planner provenance.
7. Gate BLOCK → terminal no-action; no legacy fallback.
8. Flag OFF → legacy path unchanged; V3 State remains authoritative.

No new DB table, endpoint, daemon, runner, queue, CI family, canary, monitoring framework, capability registry, Renderer, or action executor.

---

## 12. B2 acceptance — minimum sufficient evidence

Only three facts are required.

### Case 1｜none takeover really suppresses legacy

Given:

- B2 flag ON;
- valid current Planner Decision = `none`;
- Gate = `ALLOW / ok`.

Prove:

- legacy production runner/Decision is not allowed to produce a competing Action;
- owned result is `none`;
- no chat/post/tool external effect occurs.

### Case 2｜allowed owned none preserves current outcome semantics

Prove:

- existing `none` executor/Settlement path is used;
- Planner Decision-time provenance is passed through;
- existing `none` fatigue restoration semantics remain unchanged;
- no new Renderer or Settlement algorithm exists.

### Case 3｜Gate BLOCK has no back door

Create one deterministic stale-reality condition, preferably `user_active` after Planner Decision.

Prove:

- Gate returns `BLOCK / user_active`;
- executor is not called;
- Settlement is not applied;
- legacy Decision does not execute a replacement Action.

That is enough for B2.

Do not add CASE families, nightly canary, new observability infrastructure, or broad edge coverage. If these three facts pass and known risks are recorded, stop and move to B3.

---

## 13. Failure / rollback rules

Allowed decisions after the narrow implementation test:

```text
PASS
→ B2 none takeover is sufficient
→ stop testing
→ enter B3 for language-requiring/non-none actions

FAIL
→ confirmed functional defect in the narrow B2 path
→ inspect for adjacent same-cause defects once
→ fix together, rerun only the minimum affected evidence

BLOCKED
→ environment did not execute target behavior
→ stop; do not build new test infrastructure
```

Rollback:

```text
B2 consumer flag OFF
→ production behavior returns to legacy Decision
→ V3 State / Event / Settlement authority remains where it is
```

There is no default rollback from V3 State Authority to frozen legacy state tables.

---

## 14. Direct vetoes

B2 fails review if any implementation does any of the following:

- Gate changes one action into another.
- Gate uses high attachment/longing/curiosity/etc. to relax a block.
- Gate or Skill contains fixed Drive→Action rules.
- Owned Gate BLOCK falls through to legacy Action.
- B2 consumes a stale Shadow observation as production authority.
- B2 uses a record still semantically marked `shadow_only=true` as the authoritative Decision.
- Planner provenance is reconstructed from final Action.
- `none` takeover skips existing outcome semantics without an explicit approved migration.
- B2 implements message/diary language generation.
- B2 invents a new explore executor/tool loop.
- B2 adds a new DB/daemon/queue/CI/canary/monitoring framework.
- B2 changes Track A or Track C.
- disabling B2 rolls back V3 State Authority.

---

## 15. Next after this contract

If this contract receives human static PASS:

```text
B2-1
→ implement default-OFF none-only consumer + deterministic Gate
→ run exactly the three minimum acceptance facts above
→ PASS means stop B2

then B3
→ Renderer for language-requiring actions
→ non-none Action execution
→ existing authoritative Settlement consumes Planner Decision-time provenance
```

No other B2 subproject is implied.
