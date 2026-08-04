# Behavior Authority B1-0｜Planner Shadow Contract + Insertion-Point Audit

Status: **Frozen contract (docs-only).** Not implemented. Not production Decision.

Prerequisite: `👑 STATE AUTHORITY COMPLETE` (V3 is sole internal-state authority).

This document freezes where Planner Shadow may sit, what it may read, what it may emit, and what it must never touch. It does **not** implement Planner, Action Gate, Shadow runtime, or cutover.

---

## 1. Current real production Wake decision chain

Verified by static call-chain reading on `main` (files/functions below). Scheduler entry is `tools/dream_wake.py` → `POST /wake` on gateway.

```text
1. tools/dream_wake.py::_call_wake / morning / nightwatch / ritual / self_trigger
   → HTTP POST gateway `/wake` with mode + wake_run_id

2. gateway.py::wake_decide
   → acquires wake lock
   → gateway.py::_wake_decide_locked

3. gateway.py::_wake_decide_locked  (pre-Decision gates)
   - wake.wake_run_id.missing_live_wake_run_id  (live settlement modes require run id)
   - _wake_run_id_seen                           (dedup)
   - chat.interaction_state.read_interaction_clock + wake_guard_reason
   - wake.runners.select_wake_provider
   - chat.window_identity.capture_current_window_identity  (soft-window freeze)
   - t2_hours / t_hours from authoritative clock (fail closed → 0.0; never invent 999h)

4. Context / prompt collection
   - gateway.py::_wake_build_system_for_plan
     → chat.system_builder.build_system(wake=True, …)   # Wake context; Chat BP3 state inject retired
     → wake.builder.build_prompt_suffix
     → wake.builder.inject_snippets                       # Decision-time freeze lives here

5. Authoritative V3 state snapshot (Drive materialization)
   - Inside wake.builder.inject_snippets:
     drive_engine.decide()
       → drive_engine.get_drive()
         → chat.drive_authority.read_current_drives()
           → check_cutover_ready
           → read_v3_state
           → internal_state_events.materialize_bond
           → internal_state_events.materialize_drives
             (uses derived Longing boost + Bond passion boost)
   - Snapshot values are carried on decide()['drive']

6. Legacy Decision
   - drive_engine.decide()
     returns {fired, action, hint, blocked, drive, contributors}
     (includes legacy WANT_ACTION / fatigue gate — production Decision today)

7. Decision-time provenance freeze
   - drive_engine.freeze_decision_provenance(decision)
     → {source, captured_at, primary_drive, contributors, blocked, suggested_action}
   - Same `decision` object is passed to drive_engine.get_wake_snippet(decision=…)
   - inject_snippets may also append desire.get_longing_wake_fact (facts only; no second Drive→Action)
   - Returns (system, decision_provenance) to gateway

8. Model call (production Action still undecided)
   - wake.runners.*.run(WakeRequest…) → raw_text

9. Parsed / final Action
   - gateway._parse_wake_response → wake.parser.parse_response
     → (thoughts, action ∈ {none,message,diary,explore}, content)

10. Executor (real Action surfaces)
    - wake.executor.execute
      BEGIN IMMEDIATE
      → soft-window final gate
      → wake_log (+ chat_messages / posts when delivered)
      → Settlement (below) in same txn when delivered + want_settle
      COMMIT / rollback

11. V3 Settlement
    - chat.drive_authority.apply_wake_outcome_on_conn
      → check_cutover_ready_on_conn
      → internal_state_events.apply_outcome (join_transaction)
    - Consumes settle_fired_drive from frozen decision_provenance.primary_drive
    - Requires settle_provenance_present (object authenticity; N5)
    - Shadow wake_outcome path is observe_only / disabled — not a second writer
```

Modes `dream` / `summarize`: `inject_snippets` skips decide/freeze (`provenance=None`); settlement is not wanted for those modes.

---

## 2. Recommended Planner Shadow insertion point

### Choice (unique recommended)

| Field | Value |
|---|---|
| **file** | `gateway.py` |
| **function** | `_wake_decide_locked` |
| **after** | `system, surfaced_desire_ids, decision_provenance = _wake_build_system_for_plan(...)` returns |
| **before** | `runner.run(WakeRequest(...))` (model call that produces raw Action text) |
| **why** | Authoritative Drive snapshot + legacy Decision + Decision-time provenance already exist via `inject_snippets` → `decide` / `freeze_decision_provenance`. Final production Action is **not** yet determined. Shadow can observe the same Decision-time facts without touching Action / Gate / executor / Settlement. |

### B1-1 plumbing requirement for this seat (hard)

The gateway seat remains the **execution seat** for Planner Shadow. It is **not** allowed to become a second state-read seat.

B1-1 **must** perform **minimal read-only plumbing** so that, by the time `_wake_build_system_for_plan(...)` returns, the following already-frozen Decision-time inputs are carried intact to this seat:

```text
PlannerStateView + CapabilitySkillView (+ legacy decision_provenance as companion evidence)
```

Formation of those views happens at / with the authoritative V3 Decision-time read epoch (same epoch as `decide()` / `freeze_decision_provenance` inside `inject_snippets`). After freeze:

- **POST-FREEZE REREAD ALLOWED: NO**
- Forbidden: at the gateway seat, separately re-read Drive / Affect / Bond / Longing / `state_version` “for Planner”
- Forbidden: post-freeze multi-hop reads that create state-epoch drift
- Forbidden: Shadow calling `drive_engine.decide()` again

`decision['drive']` and legacy `decision_provenance` may be used as **same-epoch compatibility evidence** when packaging `PlannerStateView`; they must not authorize a second `decide()`.

### Adjacent formation locus (not a second Shadow seat)

Inside `wake.builder.inject_snippets`, immediately after:

```text
decision = drive_engine.decide()
provenance = drive_engine.freeze_decision_provenance(decision)
```

is the natural **formation epoch** for freezing `PlannerStateView` (and packaging `CapabilitySkillView` from already-resolved Wake-run capability facts). Shadow still **runs** at the gateway seat after `_wake_build_system_for_plan` returns — formation and execution may be plumbed apart; they must share one frozen epoch.

### Rejected seats

| Seat | Why rejected |
|---|---|
| After `parse_response` / before executor | Action already chosen → invites Action→Drive reverse inference |
| Inside `wake.executor.execute` / Settlement | Outcome path; Shadow must not share mutation txn semantics |
| Before clock / window identity / cutover-capable snapshot | Snapshot incomplete / identity incomplete |
| Parallel re-`decide()` later without freeze | Second snapshot; breaks Decision-time provenance parity |
| Gateway seat that re-reads V3 state after `_wake_build_system_for_plan` | Violates same-snapshot / no post-freeze reread invariant |

---

## 3. Authoritative inputs (Planner Shadow may read)

B1 answers whether **existing V3 authority** already supports behavior decisions. No Track A / Track C expansion for new state.

Upper design retained (`docs/internal_state_v3_spec.md` Track B):

```text
structured V3 State View + Capability Skill → Wake Planner → Intent / Action candidate
```

B1 freezes the two Decision-time input views below. It does **not** build a new Skill ontology, registry, DB, service, or tool system.

### 3.1 PlannerStateView (hard invariant — same snapshot)

B1-1 **must** form an immutable `PlannerStateView` before Planner Shadow runs.

Hard invariants:

1. `PlannerStateView` comes from **one** authoritative V3 state read epoch (the Decision-time snapshot that feeds production `decide()` / freeze).
2. Affect / Bond / Eight Drives base state **must** share the same `state_version`.
3. All time-materialized values in the view **must** use the same `observed_at`.
4. Derived Longing and the authoritative interaction clock **must** be bound to that Decision-time snapshot (not a later clock / longing re-sample).
5. After freeze, the view is carried whole to the recommended gateway Shadow seat.
6. After `_wake_build_system_for_plan` returns: **no** separate re-read of Drive / Affect / Bond / Longing for Planner.
7. Post-freeze multi-hop reads that create state-epoch drift are forbidden.
8. `decision['drive']` / legacy provenance are same-epoch compatibility evidence only; Shadow **must not** re-`decide()`.

Frozen schema (semantics; concrete Python type deferred to B1-1):

```text
PlannerStateView
  state_version          # V3 state_version of this snapshot
  observed_at            # single Decision-time observation instant for all materializations
  wake_run_id            # when present for this Wake
  affect                 # Current Affect fields from the same V3 read
  bond                   # intimacy / passion / commitment from the same epoch
  drives                 # eight drives map from the same epoch:
                           attachment, curiosity, reflection, social,
                           duty, libido, stress, fatigue
  longing_derived        # derived Longing bound to this snapshot
  interaction_clock      # authoritative clock / t2_hours / t_hours bound to this snapshot
  immutable = true
```

B1-0 does **not** implement this object. It freezes schema + invariant + B1-1 plumbing requirement only.

### 3.2 CapabilitySkillView (retained upper contract — minimal)

`CapabilitySkillView` is **in B1**. It is not deferred out of Track B.

It only wraps capability **facts already true for the current Wake run**. It answers:

```text
现在能做什么？
```

It must **never** answer:

```text
什么状态应该做什么？
```

Frozen minimal schema (semantics; concrete packaging deferred to B1-1):

```text
CapabilitySkillView
  action_vocabulary      # allowed Action family for this Wake parse path
                           e.g. {none, message, diary, explore}
  tool_allowlist         # tools / tool facts already resolved for this run (if any)
  provider_availability  # selected / available provider facts already resolved
  preconditions          # necessary preconditions already known (mode gates, etc.)
  external_effect_class  # classification of candidate surfaces (message/diary/explore/none)
  source = "wake_run_resolved_facts"
```

Allowed content examples:

- current allowed Action family / action vocabulary
- already-resolved tool availability / allowlist
- provider / capability availability already selected for this run
- necessary preconditions or external-effect classification already known

Forbidden inside `CapabilitySkillView` (and forbidden as Planner rules derived from it):

```text
attachment → message
curiosity → explore
longing → message
```

Also forbidden for this view in B1:

- new Skill ontology / capability registry
- new DB tables, services, endpoints
- new tool systems beyond facts already resolved on the Wake path

### 3.3 What may appear inside PlannerStateView / companion context

- **Current Affect / Bond / Eight Drives** — only via the frozen `PlannerStateView` same snapshot.
- **Authoritative interaction clock** — only as bound into that view.
- **Derived Longing** — only as bound into that view (Stage B facade; not `desire_state.last_hayana_msg_time`).
- **Wake context already legally held** for this run (mode, ritual_type, activity_desc, window identity) as companion context, not as a second state authority.
- **wake_run_id** / existing event identity.
- **CapabilitySkillView** — §3.2.

### 3.4 Forbidden as Shadow inputs (B1)

- Affect Trace, Thought / Fixation, Eventide, Pulse / Body, Morning state
- New Memory continuity / Topic Identity / Semantic Match / Reviewed View
- Chat Exposure payloads / new sensors
- Model body text / parsed Action / executor result used to infer state
- Rebuilding Track A “to make Planner smarter”
- Any Drive→Action command table disguised as “skill”

---

## 4. Frozen Planner Decision output schema

B1-0 freezes schema only. No runtime writer.

```text
planner_decision_id     # unique id for this Shadow Decision observation
wake_run_id             # same Wake identity as production run (when present)
captured_at             # Decision-time wall clock (Beijing string or equivalent existing convention)
state_version           # V3 state_version observed at Decision-time

intent                  # what the agent wants to accomplish (not an executor verb)
action_candidate        # suggested candidate only — NOT approved Action
confidence              # [0,1] Shadow self-assessment; not production probability

primary_drive           # from Decision-time state reasoning; may be null
contributors            # list of participating drives/affect/bond/longing factors

blocked                 # bool — legitimate silence / fatigue / etc.
reason_codes            # stable machine codes explaining blocked / none / low confidence

source = "planner_shadow"
shadow_only = true
```

### Field semantics

| Field | Meaning |
|---|---|
| `intent` | Goal-level desire (“reconnect”, “rest”, “survey environment”). **Not** `message`/`explore` buttons. |
| `action_candidate` | Planner suggestion among production Wake action vocabulary candidates (`none` / `message` / `diary` / `explore`) or an explicit `none`. **Must still pass a future Action Gate before any production effect.** |
| `primary_drive` | Primary deficit/motive inferred at Decision-time from allowed state. **Never** from final Action / assistant text. |
| `contributors` | Multi-factor participation (other drives, Affect, Bond, Longing). Records influence, not a fixed mapping table. |
| `blocked` | True when Shadow concludes it should not propose active behavior (e.g. fatigue). `action_candidate=none` with `blocked=false` is also legal (quiet contentment). |
| `shadow_only` | Hard true for entire B1 Shadow phase. |

### Tendency, not command

Forbidden fixed production rules inside Planner:

```text
attachment high → message
curiosity high → browse/explore
libido high → flirt/message
```

State may change **tendency** and `confidence`; it must not encode a permanent Drive→Action dictionary. (Legacy `drive_engine.WANT_ACTION` is exactly such a dictionary and remains production Decision until Behavior cutover — Shadow must not copy it as authority.)

---

## 5. Decision-time provenance contract (permanent)

Correct causality (aligned with Stage D / `internal_state_v3_spec` §5.2):

```text
State Snapshot S
→ Planner Decision D          # Shadow-only in B1
→ freeze provenance P         # Decision-time
→ (future) Action Gate
→ Action A                    # production still from legacy path in B1
→ Settlement(A, P_legacy)     # production Settlement continues to use legacy freeze
```

During B1 Shadow:

- Production Settlement continues to consume **legacy** `decision_provenance` from `drive_engine.freeze_decision_provenance` (Stage D).
- Shadow Decision may freeze its **own** observation provenance for comparison, but **must not** replace or rewrite production Settlement provenance.
- Forbidden: `Action → infer primary_drive / intent`.
- Forbidden: revive `infer_fired_drive_for_action` under any name for production or Shadow “truth”.
- Missing production provenance object remains fail closed for non-`none` settlement (Stage D N5).

---

## 6. Shadow-only invariants

For the entire Planner Shadow phase:

1. `Shadow Decision ≠ Production Decision`.
2. Shadow **must not** affect: Wake trigger / probability, legacy Decision, real Action, Action Gate (not yet built), executor, message content, prompt, persona, V3 state, Settlement, Longing / Affect / Bond / Drive authority, DB authority tables.
3. Divergent pairs are expected and allowed for study:

```text
legacy = message , shadow = none
legacy = none    , shadow = message
```

Production executes **legacy path only**.

4. Shadow must not execute Shadow Action or apply Shadow Settlement.
5. Shadow must not become a second V3 mutation entry (same class of bug Stage D closed for wake_outcome shadow).
6. Shadow consumes only carried `PlannerStateView + CapabilitySkillView` (plus legacy provenance evidence). Post-freeze V3 reread / second `decide()` is forbidden.

---

## 7. B1-1 allowed scope (next implementation boundary)

When human-approved, B1-1 may:

1. At the Decision-time formation epoch (with `decide()` / `freeze_decision_provenance`), build immutable `PlannerStateView` from **one** authoritative V3 read, and package `CapabilitySkillView` from Wake-run-resolved capability facts only.
2. Perform **minimal read-only plumbing** to carry the frozen `PlannerStateView + CapabilitySkillView` (plus legacy provenance as companion evidence) to the recommended gateway seat after `_wake_build_system_for_plan(...)` returns. Plumbing must not change production Decision / Action / Settlement semantics.
3. At that seat, run Planner Shadow on the carried frozen views only (no post-freeze V3 re-read; no second `decide()`).
4. Emit a structured Shadow Decision matching §4.
5. Persist a **Shadow observation** sufficient for human comparison (non-authoritative medium chosen in B1-1; must not be Canonical Event authority).
6. Leave production Decision, Action, executor, and Settlement unchanged.
7. Keep `shadow_only=true`.

Deferred to B1-1 implementation choice only (not open design for “whether”):

- concrete Python form (`dataclass` / `TypedDict` / plain `dict`, etc.)
- non-authoritative Shadow observation persistence medium

---

## 8. B1-1 prohibited scope

- Action Gate implementation or wiring
- Local takeover / Behavior Authority cutover
- New Internal State (Track A) fields or engines
- Track C Chat Exposure rollout
- New memory layer / Topic Identity / Semantic Match
- Prompt redesign for production Decision
- Changing Wake probability / scheduler
- Shadow Action execution or Shadow Settlement
- Post-freeze re-read of Drive / Affect / Bond / Longing / clock / `state_version` for Planner
- Shadow re-calling `drive_engine.decide()`
- New Skill ontology / capability registry / DB / service / endpoint / tool system for CapabilitySkillView
- New endpoints, daemons, CI matrices, canaries, schema migrations (unless a later dedicated B1-1 task explicitly authorizes a minimal observation sink — **not** authorized by B1-0 alone beyond “save observation”)

---

## 9. Future B1 acceptance questions (do not implement tests in B1-0)

B1 graduates to B2 only when evidence answers:

### Q1

Can Planner form explainable, stable Decisions from the same class of V3 state across comparable Wake runs?

### Q2

Does Planner obey **state modifies tendency, not command** (no fixed Drive→Action dictionary)?

### Q3

Is provenance truly Decision-time (frozen before Action), never Action-posterior inference?

Do not require exhaustive edge coverage.

---

## 10. Known risks / deferred (non-contract) notes

Frozen by this narrow fix (no longer open design questions):

- **PlannerStateView must be formed and carried whole** — same `state_version` / same `observed_at`; no post-freeze reread (§3.1, §2 plumbing).
- **CapabilitySkillView is in B1** — minimal Wake-run-resolved capability facts only; no new Skill ontology (§3.2).

Remaining risks / B1-1 engineering notes:

1. **Legacy `WANT_ACTION` fixed mapping** already shapes production Decision + prompt hint (“倾向于 X 行为”). Shadow vs legacy disagreement is informative; copying `WANT_ACTION` into Planner would fail Q2.
2. **Second `get_drive()`** may occur later in `_wake_build_system_for_plan` (recall_photo nudge) after freeze — production Settlement still uses frozen provenance; Shadow must pin to frozen `PlannerStateView`, not the nudge re-read.
3. **Live path pre-snapshot side effects** (`drive_engine._flush` no-op, `desire.calibrate_va`) run before `_wake_build_system_for_plan`. Confirm in B1-1 they do not mutate V3 authority (Stage D: flush is retired no-op; calibrate must remain non-authoritative).
4. **B1-1 deferred only:** concrete Python packaging of the two views; non-authoritative Shadow observation persistence medium.

---

## 11. Stop condition (evaluated)

A credible seat exists:

> authoritative Drive snapshot formed + legacy Decision + provenance frozen, and final Action not yet produced.

**B1-0 is not BLOCKED.**

---

## 12. Document control

| Item | Value |
|---|---|
| Track | Behavior Authority / B1 Planner Shadow |
| Phase | B1-0 contract freeze |
| Code changes | None (docs-only) |
| Production Decision | Unchanged (legacy) |
| Next | Human static re-review; do not start B1-1 until approved |
