# Behavior Authority B1-0｜Planner Shadow Contract + Insertion-Point Audit

Status: **Frozen contract (docs-only).** Not implemented. Not production Decision.

Prerequisite: `👑 STATE AUTHORITY COMPLETE` (V3 is sole internal-state authority). B1-1 must not start until that human grant is cited as evidence; GitHub merge of crown cleanup alone is not sufficient proof.

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
     → TODAY this early clock is both GuardClock (eligibility) AND reused as
       model-visible t2_hours / t_hours / Longing override — see §3.1 GuardClock vs DecisionClock
   - wake.runners.select_wake_provider
   - chat.window_identity.capture_current_window_identity  (soft-window freeze)
   - t2_hours / t_hours from that early clock (fail closed → 0.0; never invent 999h)

4. Context / prompt collection
   - gateway.py::_wake_build_system_for_plan
     → chat.system_builder.build_system(wake=True, …)   # Wake context; Chat BP3 state inject retired
     → wake.builder.build_prompt_suffix                 # TODAY writes GuardClock-derived t_hours/t2_hours into prompt
     → wake.builder.inject_snippets                       # Decision-time freeze lives here

5. Authoritative V3 state snapshot (Drive materialization) — TODAY
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
   - NOTE: current get_drive / read_derived_longing may sample an independent clock
     inside the Drive path; gateway already holds an earlier GuardClock; Longing fact
     also uses t_hours_override=t2_hours from that GuardClock. This is why §3.1 freezes
     one SQLite Decision-time snapshot + DecisionClock binding for B1-1.

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

7b. PRODUCTION POST-FREEZE REREAD (discovered; baseline pollution)
   - Inside _wake_build_system_for_plan, AFTER inject_snippets freeze, Relay live path:
     drive_engine.get_drive() again → attachment threshold → recall_photo prompt nudge
   - This is a second internal-state epoch influencing the production model prompt.
   - See §10.B BLOCKING PREREQUISITE — not a soft risk.

8. Capability resolution (after system build returns)
   - gateway._wake_full_tools_for_mode(mode)
   - wake.runners.prepare_tools_for_provider(wake_provider, full_tools, mode, dry_run=…)
     → dry_run → []
     → claude_code → filter_wake_tools_for_cc(...)
     → api_relay → full tools (provider-specific)
   - Final provider-specific tool allowlist exists only here — not at step 4 return.

9. Model call (production Action still undecided)
   - wake.runners.*.run(WakeRequest…, tools=_wake_tools) → raw_text

10. Parsed / final Action
    - gateway._parse_wake_response → wake.parser.parse_response
      → (thoughts, action ∈ {none,message,diary,explore}, content)

11. Executor (real Action surfaces)
    - wake.executor.execute
      BEGIN IMMEDIATE
      → soft-window final gate
      → wake_log (+ chat_messages / posts when delivered)
      → Settlement (below) in same txn when delivered + want_settle
      COMMIT / rollback
    - Executor preconditions include: message/diary with empty CONTENT → reject

12. V3 Settlement
    - chat.drive_authority.apply_wake_outcome_on_conn
      → check_cutover_ready_on_conn
      → internal_state_events.apply_outcome (join_transaction)
    - Consumes settle_fired_drive from frozen decision_provenance.primary_drive
    - Requires settle_provenance_present (object authenticity; N5) for every settlement
    - Shadow wake_outcome path is observe_only / disabled — not a second writer
```

Modes `dream` / `summarize`: `inject_snippets` skips decide/freeze (`provenance=None`); settlement is not wanted for those modes.

### 1.1 B1 Planner Shadow eligible mode / evidence scope (hard)

B1 comparison samples are **not** all Wake surfaces. Scope is frozen here (not left to B1-1 judgment).

```text
B1 Planner Shadow comparison-eligible scope:

live production Behavior-Decision modes only:
- normal
- morning
- nightwatch
- ritual
- self_trigger

AND:
- dry_run = false
- not inspect_only
- inject_snippets performs decide/freeze (provenance object present)
- production attempt reaches the recommended Shadow execution seat
```

Out of comparison scope:

```text
dream / summarize:
- outside B1 Behavior Planner comparison scope
- no PlannerStateView / legacy-provenance comparison sample
- (production already skips decide/freeze; do not invent Shadow Behavior samples here)

dry_run:
- may call the model, but does not executor / real Action / Settlement
- never accepted as production comparison evidence

inspect_only:
- separate build/inspection path; does not enter the recommended Shadow execution seat
- never accepted as production comparison evidence
```

Non-production diagnostics involving dry_run / inspect_only / dream / summarize are **not** authorized by B1-0. If a later task wants them, they must be explicitly marked non-production and must not enter Q1–Q3 evidence.

---

## 2. Recommended Planner Shadow insertion point

### Choice (unique recommended — execution seat)

Applies only to §1.1 comparison-eligible live Behavior-Decision attempts.

| Field | Value |
|---|---|
| **file** | `gateway.py` |
| **function** | `_wake_decide_locked` |
| **after** | `_wake_tools = prepare_tools_for_provider(wake_provider, full_tools, mode, dry_run=…)` |
| **before** | `runner.run(WakeRequest(...))` (model call that produces raw Action text) |
| **why** | By this point: (a) authoritative Decision-time state epoch can have frozen `PlannerStateView` and legacy provenance; (b) provider is selected and actual `_wake_tools` are prepared, so `CapabilitySkillView` can be honest; (c) final production Action is **not** yet determined. |

`PlannerStateView` is **carried** from the earlier authoritative state epoch to this seat.  
`CapabilitySkillView` is **frozen at this seat** from already-resolved provider/tool/mode facts.  
Then both are used to **freeze / dispatch** Planner Shadow **without blocking** `runner.run` (§6.1).

### Split freeze timing (hard)

Capability is not psychological State. It must **not** be forced into the same V3 state epoch.

```text
PlannerStateView
  → freeze at authoritative Decision-time state epoch (§3.1 causal order)
  → carry immutably to the execution seat

CapabilitySkillView
  → freeze after provider selected AND actual _wake_tools prepared/filtered
  → same wake_run, later than PlannerStateView freeze is required and correct

Then at execution seat (comparison-eligible attempts only):
  PlannerStateView + CapabilitySkillView
  → freeze/dispatch Planner Shadow attempt (non-blocking; §6.1)
  → production runner.run proceeds immediately
```

### B1-1 plumbing requirement (hard)

B1-1 **must** perform **minimal read-only plumbing** so that:

1. `PlannerStateView` is formed under §3.1 causal order; the SQLite read transaction ends immediately after `V` is frozen; only the immutable `V` value is carried forward.
2. `CapabilitySkillView` is formed only after `prepare_tools_for_provider(...)` returns.
3. Planner Shadow is dispatched on those frozen views only, under §6.1 non-blocking / fail-open isolation, and only for §1.1 eligible attempts.

Forbidden substitutes:

- **POST-FREEZE STATE REREAD ALLOWED: NO** — no gateway re-read of Drive / Affect / Bond / Longing / `state_version` “for Planner”
- No post-freeze multi-hop reads that create state-epoch drift
- No Shadow calling `drive_engine.decide()` again
- No early duplicate copy of tool filtering before `prepare_tools_for_provider` (two capability truths → drift)
- No packaging an unresolved / placeholder allowlist as `CapabilitySkillView`

### Rejected seats

| Seat | Why rejected |
|---|---|
| After `parse_response` / before executor | Action already chosen → invites Action→Drive reverse inference |
| Inside `wake.executor.execute` / Settlement | Outcome path; Shadow must not share mutation txn semantics |
| Before clock / window identity / cutover-capable snapshot | Snapshot incomplete / identity incomplete |
| Immediately after `_wake_build_system_for_plan` returns, before tool prepare | Final provider-specific tool allowlist does not exist yet |
| Parallel re-`decide()` later without freeze | Second snapshot; breaks Decision-time provenance parity |
| Gateway seat that re-reads V3 state after state-epoch freeze | Violates same-snapshot / no post-freeze reread invariant |

---

## 3. Authoritative inputs (Planner Shadow may read)

B1 answers whether **existing V3 authority** already supports behavior decisions. No Track A / Track C expansion for new state.

Upper design retained (`docs/internal_state_v3_spec.md` Track B):

```text
structured V3 State View + Capability Skill → Wake Planner → Intent / Action candidate
```

B1 freezes the two Decision-time input views below. It does **not** build a new Skill ontology, registry, DB, service, or tool system.

### 3.1 PlannerStateView (hard invariant — causal freeze)

B1-1 **must** form an immutable `PlannerStateView` before Planner Shadow runs.

Schema alone is insufficient. The **causal order** and **SQLite snapshot semantics** are hard contract:

```text
freeze observed_at T once
↓
open one authoritative SQLite read epoch
  = one connection
  + one read transaction / snapshot
↓
from that same snapshot, read:
  V3 row S          (e.g. store.read_state(conn))
  interaction clock C at T
                    (e.g. read_interaction_clock_from_conn(conn, now=T))
↓
derive Longing L from C
materialize Bond / Drives from S at T using L
↓
freeze PlannerStateView V  (DecisionClock C is bound inside V)
↓
legacy Decision MUST consume V.drives
  (no get_drive / no second V3 read / no second clock)
↓
freeze legacy provenance P
↓
freeze immutable V
↓
END the SQLite read transaction / release the live connection
immediately after V is materialized/frozen
↓
carry immutable V values only
(no live DB txn / connection across Shadow, model, or tools)
↓
all model-visible state/clock-derived production prompt facts
MUST render from the same frozen V / C bundle
```

Hard contract sentences:

```text
Authoritative Decision-time read epoch MUST be one SQLite
snapshot on one connection / read transaction.

V3 row S and interaction clock C must be read from that
same snapshot. Same observed_at alone is insufficient.

The SQLite read transaction used to freeze V MUST end
immediately after V is materialized/frozen.

No DB read transaction / live connection may be carried
across Shadow execution, production model calls, or tool calls.
Only the immutable V value is carried forward.

Legacy production Decision MUST consume the exact Drives
contained in the frozen PlannerStateView.
It may not obtain its Decision inputs through a second getter/read.

All model-visible internal-state / interaction-clock-derived
facts used by the production comparison baseline MUST be
rendered from the same frozen Decision-time bundle V/C.

A pre-gate GuardClock may decide wake eligibility,
but it MUST NOT be reused as Decision/prompt state.
```

Concrete API shape (`decide_from_snapshot()`, `BEGIN` vs existing helpers, parameters on `decide()`, pure helper split, etc.) is deferred to B1-1. The causal order and **transaction / snapshot semantics** are **not** deferred.

#### GuardClock vs DecisionClock (hard)

```text
GuardClock
  → may be read earlier (today: wake_guard_reason path)
  → only answers: may this Wake attempt proceed?
  → MUST NOT supply model-visible t_hours / t2_hours
  → MUST NOT supply Longing fact / other state-derived Wake prompt facts
  → MUST NOT supply PlannerStateView / legacy Decision inputs

DecisionClock C
  → read inside the same SQLite snapshot as V3 row S
  → sole clock allowed into:
       PlannerStateView
       legacy Decision inputs
       model-visible t2_hours / t_hours
       Longing fact (including get_longing_wake_fact override)
       other state-derived Wake prompt facts
       recall_photo nudge attachment (via V; §10.B)
```

Today's pollution pattern to eliminate in B1-1 plumbing (docs freeze only here):

```text
Guard / old prompt clock = C0
atomic Planner epoch later = S1 + C1 → V1
production prompt still uses C0 for t_hours / t2_hours / Longing fact
→ Planner(C1) vs Legacy model(C0 + C1) — comparison invalid
```

Additional hard invariants:

1. Affect / Bond / Eight Drives base state **must** share the same `state_version` from row `S`.
2. All time-materialized values in the view **must** use the same `observed_at` `T`.
3. Derived Longing and the authoritative interaction clock **must** be the `L` / `C` bound into `V` — not GuardClock, not a later re-sample, and not the independent clock currently sampled inside `read_derived_longing` / `_longing_for_boost`.
4. After freeze, `V` (including DecisionClock) is carried whole to the Shadow execution seat **and** used to render production comparison-baseline prompt facts that depend on state/clock.
5. After state-epoch freeze: **no** separate re-read of Drive / Affect / Bond / Longing for Planner or for legacy Decision inputs.
6. Post-freeze multi-hop reads that create state-epoch drift are forbidden.
7. Separate SQLite connections / commits between reading `S` and `C` are forbidden for the Decision-time epoch (even if both stamp the same `observed_at`).
8. `decision['drive']` / legacy provenance may equal `V.drives` as same-epoch evidence only when they were produced by consuming `V`; they must not authorize a second `decide()` / `get_drive()`.

Frozen schema (semantics; concrete Python type deferred to B1-1):

```text
PlannerStateView
  state_version          # V3 state_version of row S
  observed_at            # single Decision-time observation instant T
  wake_run_id            # when present for this Wake
  affect                 # Current Affect fields from S
  bond                   # intimacy / passion / commitment from S at T
  drives                 # eight drives map from S at T using L:
                           attachment, curiosity, reflection, social,
                           duty, libido, stress, fatigue
  longing_derived        # L derived from C at T
  interaction_clock      # C bound at T (authoritative; same values used for L)
  immutable = true
```

B1-0 does **not** implement this object. It freezes schema + causal invariant + B1-1 plumbing requirement only.

### 3.2 CapabilitySkillView (retained upper contract — freeze after resolve)

`CapabilitySkillView` is **in B1**. It is not deferred out of Track B.

It only wraps capability **facts already true for the current Wake run after resolve**. It answers:

```text
现在能做什么？
```

It must **never** answer:

```text
什么状态应该做什么？
```

#### Freeze timing (hard)

```text
CapabilitySkillView freezes only when:
  - wake_provider already selected
  - actual _wake_tools already prepared/filtered by prepare_tools_for_provider(...)
  - mode / dry_run / provider contracts for this run are known
```

It is **not** required (and must not be pretended) to freeze at the V3 state epoch.

#### Resolved action capability (hard)

`action_vocabulary` / resolved action capability **must not** be only the parser enum `{none,message,diary,explore}`.

It must describe real executable capability for this run:

```text
resolved action capability =
  parser legality
  ∩ current mode contract
  ∩ current provider contract
  ∩ executor preconditions
```

Known production facts that force this intersection (examples, not a Skill ontology):

- Parser accepts `none` / `message` / `diary` / `explore`.
- Executor rejects `message` or `diary` with empty CONTENT.
- Claude Code Wake contract currently tells the model diary CONTENT may be left empty — which then fails executor. Relay Wake prompt lists diary with content expectations. Provider/mode contracts therefore differ; CapabilitySkillView must reflect the **resolved** run, not a global parser list.

Frozen minimal schema (semantics; concrete packaging deferred to B1-1):

```text
CapabilitySkillView
  resolved_action_capability  # intersection above — NOT bare parser enum
  tool_allowlist              # actual prepared _wake_tools for this run
  provider_availability       # selected provider + availability facts
  mode_contract               # mode / dry_run / prompt-contract facts already resolved
  preconditions               # executor / surface preconditions already known
  external_effect_class       # classification of candidate surfaces
  source = "wake_run_resolved_facts"
  frozen_after = "prepare_tools_for_provider"
```

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
- duplicating tool-filter logic before `prepare_tools_for_provider` as a second truth
- claiming parser enum alone is `action_vocabulary`

### 3.3 What may appear inside PlannerStateView / companion context

- **Current Affect / Bond / Eight Drives** — only via the frozen `PlannerStateView` same snapshot.
- **Authoritative interaction clock** — only as bound into that view.
- **Derived Longing** — only as bound into that view (Stage B facade; not `desire_state.last_hayana_msg_time`).
- **Wake context already legally held** for this run (mode, ritual_type, activity_desc, window identity) as companion context, not as a second state authority.
- **wake_run_id** / existing event identity.
- **CapabilitySkillView** — §3.2 (frozen later; capability facts only).

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
planner_decision_id     # unique id for this Shadow Decision observation (per attempt)
wake_run_id             # same Wake identity as production run (when present)
decision_attempt_id     # identity of this Decision attempt (may equal planner_decision_id)
captured_at             # Decision-time wall clock (Beijing string or equivalent existing convention)
state_version           # V3 state_version observed at Decision-time (from PlannerStateView)

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
| `planner_decision_id` / `decision_attempt_id` | Identifies **one Decision attempt**, not merely `wake_run_id`. Same `wake_run_id` may have multiple attempts before Action commit; pairing is per attempt (§6.1). |
| `intent` | Goal-level desire (“reconnect”, “rest”, “survey environment”). **Not** `message`/`explore` buttons. |
| `action_candidate` | Planner suggestion among **resolved** action capability for this run (§3.2), or explicit `none`. **Must still pass a future Action Gate before any production effect.** |
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
State Snapshot S / PlannerStateView V
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

### Stage D N5 (hard — must not weaken)

Aligned with production `apply_wake_outcome_on_conn` / State Authority:

```text
Missing production provenance object remains fail closed
for every settlement, including Action=none.

primary_drive may be null only when the provenance object
itself is valid and the Action semantics permit null primary.
```

Concrete legal / illegal cases:

```text
valid provenance object
+ primary_drive=None
+ Action=none
→ legal

provenance object entirely missing
+ Action=none
→ must fail closed

provenance object missing
+ Action≠none
→ must fail closed

valid provenance object
+ primary_drive=None
+ Action≠none
→ must fail closed (non-none requires primary_drive)
```

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
6. Shadow consumes only carried `PlannerStateView` + freeze-after-resolve `CapabilitySkillView` (plus legacy provenance evidence). Post-freeze V3 reread / second `decide()` is forbidden.
7. Shadow comparison evidence is invalid while production baseline still uses a post-freeze internal-state reread (§10.B) or GuardClock-derived prompt facts (§3.1) instead of frozen `V/C`.

### 6.1 Shadow Isolation Contract (hard — fail-open + non-blocking)

The pre-run seat **freezes / dispatches** the Shadow attempt. Shadow remains observational. Fail-open **and** non-blocking semantics are **not** deferred:

```text
Planner Shadow failure / timeout / parse failure
/ observation persistence failure
→ MUST NOT block or alter production Wake.

Production runner MUST NOT wait for Shadow completion,
timeout, parsing, or observation persistence.

The pre-run seat freezes / dispatches the Shadow attempt;
it must not add blocking latency to the production runner.

Shadow may execute in an isolated concurrent path, or an
equivalent non-blocking mechanism, using only the frozen
Decision-time inputs.

Production proceeds immediately.

Production runner receives the same:
  system
  messages
  tools
  provider/runtime state
as if Shadow were disabled.

Shadow may not execute Wake tools or external effects.

If model-backed:
  Shadow must not mutate/reuse production runner conversational
  or resident state (including Claude Code Wake resident session).

Shadow execution must have a bounded budget;
budget exhaustion → drop Shadow sample, continue production.
```

Forbidden “fail-open but blocking” pattern:

```text
run Shadow
wait up to N seconds
↓
then runner.run()
```

That still delays production (window identity / board / rate-limit / user message may change) and violates “Shadow must not affect real Action / production Wake”.

Exact timeout/budget numbers and concurrency mechanism may be chosen in B1-1. **Fail-open, non-blocking, and no production wait are not choosable.**

### 6.2 Observation pairing / orphan semantics (hard)

`wake_run_id` alone is insufficient for comparison identity. The same `wake_run_id` may retry before Action commit:

```text
wake_run_id = X
attempt 1 → Shadow D1 persisted → production model/executor failure
attempt 2 → Shadow D2 persisted → production Action A2 commits
```

Hard contract:

```text
Shadow observation identity is per Decision attempt,
not merely per wake_run_id.

Every accepted comparison sample must unambiguously pair
one frozen Shadow Decision with the production attempt/outcome
from that same attempt.

Orphan Shadow attempts after failed production runs
must not be treated as comparison evidence.
```

`planner_decision_id` (and/or `decision_attempt_id`) satisfies attempt identity. Storage medium and whether observation is written only after production success are deferred to B1-1; pairing / orphan semantics are not.

---

## 7. B1-1 allowed scope (next implementation boundary)

When human-approved **and** §10.B prerequisite is satisfied, B1-1 may:

1. For §1.1 comparison-eligible live Behavior-Decision attempts only: open one SQLite connection / read transaction snapshot; read `S` + DecisionClock `C` from that snapshot at `observed_at T`; derive `L`; materialize; freeze `PlannerStateView V` (§3.1); **end the read transaction immediately**; carry immutable `V` only.
2. Make legacy production Decision consume **exact** `V.drives` (no second `get_drive` / V3 read / clock for Decision inputs). API naming deferred.
3. Bind all model-visible state/clock-derived production prompt facts (`t_hours` / `t2_hours`, Longing fact, snapshot-bound recall_photo nudge, other state-derived Wake prompt facts) to frozen `V/C`. GuardClock remains eligibility-only.
4. Carry frozen immutable `PlannerStateView` (+ legacy provenance) to the execution seat after tool prepare — never a live DB txn/connection.
5. After `prepare_tools_for_provider(...)`, freeze `CapabilitySkillView` from actual resolved provider/tool/mode/executor facts (§3.2).
6. Freeze/dispatch Planner Shadow under §6.1 fail-open **non-blocking** isolation on those frozen views only (no production wait; no post-freeze V3 re-read; no second `decide()`). Production `runner.run` proceeds immediately.
7. Emit a structured Shadow Decision matching §4 with per-attempt identity (§6.2).
8. Persist a **Shadow observation** sufficient for human comparison under §6.2 pairing rules and §1.1 eligible scope (non-authoritative medium chosen in B1-1; must not be Canonical Event authority).
9. Leave production Decision semantics / Action Gate / Settlement authority unchanged except the minimal same-snapshot + DecisionClock-binding plumbing required by §3.1 / §6 and the §10.B prerequisite cleanup.
10. Keep `shadow_only=true`.

Deferred to B1-1 implementation choice only (not open design for “whether”):

- concrete Python form (`dataclass` / `TypedDict` / plain `dict`, etc.)
- non-authoritative Shadow observation persistence medium
- exact function names for “decide from snapshot” / read-txn helper
- numeric Shadow timeout / budget values and concurrency mechanism (fail-open + non-blocking already frozen)

---

## 8. B1-1 prohibited scope

- Action Gate implementation or wiring
- Local takeover / Behavior Authority cutover
- New Internal State (Track A) fields or engines
- Track C Chat Exposure rollout
- New memory layer / Topic Identity / Semantic Match
- Prompt redesign for production Decision beyond: §10.B snapshot-bind of recall_photo nudge, and binding model-visible state/clock-derived facts to DecisionClock `V/C` (§3.1)
- Changing Wake probability / scheduler / GuardClock eligibility policy (beyond not reusing GuardClock as Decision/prompt state)
- Treating dream / summarize / dry_run / inspect_only as B1 production comparison evidence (§1.1)
- Shadow Action execution or Shadow Settlement
- Shadow fail-closed behavior that blocks `runner.run` / alters production inputs on Shadow error/timeout/persistence failure
- Synchronously waiting for Shadow completion/timeout/parse/persistence before `runner.run` (blocking latency)
- Holding the Decision-time SQLite read transaction / live connection across Shadow, model, or tool calls
- Shadow mutating or reusing production runner conversational / resident state
- Shadow executing Wake tools or external effects
- Treating `wake_run_id`-only pairs (including orphan Shadow attempts) as accepted comparison evidence
- Post-freeze re-read of Drive / Affect / Bond / Longing / clock / `state_version` for Planner or for legacy Decision inputs
- Shadow re-calling `drive_engine.decide()` / `get_drive()` for its inputs
- Reading V3 row `S` and DecisionClock `C` from different SQLite snapshots / connections / commits for the Decision-time epoch
- Freezing `CapabilitySkillView` before actual `_wake_tools` prepare
- Treating parser enum alone as resolved action capability
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

Shadow vs legacy Action comparison is only valid when:

- sample is in §1.1 comparison-eligible live Behavior-Decision scope
- §10.B prerequisite holds (no production post-freeze state reread shaping the baseline prompt)
- production model-visible state/clock-derived facts bind to DecisionClock `V/C` (§3.1), not GuardClock
- comparison samples obey per-attempt pairing / orphan rules (§6.2)
- production path was not delayed by waiting on Shadow (§6.1)

---

## 10. Known risks / deferred notes / blocking prerequisite

### Already frozen (not open design)

- **PlannerStateView causal order** — §3.1; legacy Decision must consume `V.drives`.
- **One SQLite Decision-time snapshot** — same connection / read transaction for `S` and `C`; same `observed_at` alone is insufficient; txn ends immediately after `V` freeze; only immutable `V` carried — §3.1.
- **GuardClock vs DecisionClock** — GuardClock eligibility-only; all model-visible state/clock-derived baseline facts from frozen `V/C` — §3.1.
- **Eligible mode / evidence scope** — live `normal` / `morning` / `nightwatch` / `ritual` / `self_trigger` only; dream/summarize/dry_run/inspect_only out of comparison — §1.1.
- **CapabilitySkillView in B1** — freeze after `prepare_tools_for_provider`; resolved action capability intersection — §3.2.
- **N5** — missing provenance object fail-closed for every settlement, including `Action=none` — §5.
- **Execution seat** — after tool prepare; freeze/dispatch Shadow without blocking `runner.run` — §2 / §6.1.
- **Shadow Isolation** — fail-open + non-blocking; no production wait; no tool/external effects; no resident-state reuse; bounded budget — §6.1.
- **Per-attempt observation pairing** — not `wake_run_id`-only; orphans are not comparison evidence — §6.2.

### 10.B B1-1 BLOCKING PREREQUISITE (production post-freeze reread)

Current production `_wake_build_system_for_plan` (Relay live) performs:

```text
inject_snippets()
→ drive_engine.decide()
→ freeze provenance P1
→ (later in same function)
→ drive_engine.get_drive() again          # second V3/Drive epoch S2
→ if attachment >= 0.45: append recall_photo prompt nudge
→ model Action
→ Settlement(P1)
```

This is the same class of hazard Stage D closed for the second desire Drive→Action prompt: a **post-freeze internal-state read** influences the production model prompt / Action baseline while Settlement still cites `P1`.

B1-0 freezes:

```text
B1-1 BLOCKING PREREQUISITE:

retire / snapshot-bind the recall_photo attachment nudge
before Planner Shadow evidence is considered valid.

No post-freeze internal-state read may influence
the production model prompt used as the Shadow comparison baseline.
```

Preferred narrow production cleanup (separate from this docs PR; do not implement in B1-0):

- Keep recall_photo nudge behavior.
- Bind its attachment threshold to the **same frozen PlannerStateView / legacy decision snapshot** attachment.
- Do **not** call `get_drive()` again after freeze.

Until that prerequisite is done, Shadow vs legacy Action comparisons are baseline-contaminated and must not be treated as B1 acceptance evidence.

### Other engineering notes

1. **Legacy `WANT_ACTION` fixed mapping** already shapes production Decision + prompt hint (“倾向于 X 行为”). Shadow vs legacy disagreement is informative; copying `WANT_ACTION` into Planner would fail Q2.
2. **Live path pre-snapshot side effects** (`drive_engine._flush` no-op, `desire.calibrate_va`) run before `_wake_build_system_for_plan`. Confirm in B1-1 they do not mutate V3 authority (Stage D: flush is retired no-op; calibrate must remain non-authoritative).
3. **B1-1 deferred only:** concrete Python packaging of the two views; non-authoritative Shadow observation persistence medium; exact snapshot-consume / read-txn API names; numeric Shadow timeout/budget and concurrency mechanism; whether observation persists only after production success.
4. **Prerequisite grant evidence:** before starting B1-1, cite the human `👑 STATE AUTHORITY COMPLETE` grant; do not treat crown-cleanup merge metadata alone as that grant.

---

## 11. Stop condition (evaluated)

A credible execution seat exists:

> after provider/tool resolve and before model Action, with room to carry a prior authoritative state freeze.

B1-1 validity still depends on implementing the frozen contracts (eligible mode scope, one SQLite Decision snapshot released immediately after `V` freeze, DecisionClock-bound production prompt facts, Shadow fail-open **non-blocking** isolation + per-attempt pairing) and the §10.B recall_photo prerequisite. Those are **not** reasons to invent a second Shadow seat or to start Planner runtime in this PR.

**B1-0 remains docs-only.** This revision locks B8/B9; it does not authorize B1-1 start or merge.

---

## 12. Document control

| Item | Value |
|---|---|
| Track | Behavior Authority / B1 Planner Shadow |
| Phase | B1-0 contract freeze (narrow fix: eligible mode scope + non-blocking Shadow / immediate snapshot release) |
| Code changes | None (docs-only) |
| Production Decision | Unchanged (legacy) |
| Next | Human static seal check; do not start B1-1; do not merge until PASS |
