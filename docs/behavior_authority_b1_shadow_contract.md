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

### Adjacent tighter option (same epoch; not a second seat)

Inside `wake.builder.inject_snippets`, immediately after:

```text
decision = drive_engine.decide()
provenance = drive_engine.freeze_decision_provenance(decision)
```

and before returning to gateway. Same Decision-time epoch; would require B1-1 plumbing to surface Shadow observation without changing return contract of production Decision. **Preferred B1-1 seat remains gateway after `_wake_build_system_for_plan`**, so Shadow stays outside prompt assembly side effects (desire ledger room snippet, recall_photo nudge) unless those are explicitly listed as context inputs.

### Rejected seats

| Seat | Why rejected |
|---|---|
| After `parse_response` / before executor | Action already chosen → invites Action→Drive reverse inference |
| Inside `wake.executor.execute` / Settlement | Outcome path; Shadow must not share mutation txn semantics |
| Before clock / window identity / cutover-capable snapshot | Snapshot incomplete / identity incomplete |
| Parallel re-`decide()` later without freeze | Second snapshot; breaks Decision-time provenance parity |

---

## 3. Authoritative inputs (Planner Shadow may read)

B1 answers whether **existing V3 authority** already supports behavior decisions. No Track A / Track C expansion.

### Allowed

- **Current Affect** — from `internal_state_v3` via existing Affect/Bond authority reads (`chat.affect_bond_authority` / `read_v3_state` materialization paths).
- **Bond** — intimacy / passion / commitment via existing `materialize_bond` / bond authority reads.
- **Eight Drives** — attachment, curiosity, reflection, social, duty, libido, stress, fatigue via `read_current_drives` / the Drive map already on `decide()['drive']` at freeze time.
- **Authoritative interaction clock** — `chat.interaction_state.read_interaction_clock` / `t2_hours` already held in `_wake_decide_locked`.
- **Derived Longing** — `internal_state.read_derived_longing` / Stage B facade (not `desire_state.last_hayana_msg_time`).
- **Wake context already legally held** for this run (mode, ritual_type, activity_desc, window identity tuple, provider selection result as capability *facts already resolved*, not new skills).
- **state_version** and snapshot / freeze timestamp (`captured_at` from provenance freeze; V3 `state_version` from `read_v3_state`).
- **wake_run_id** and related existing event identity.

### Forbidden as Shadow inputs (B1)

- Affect Trace, Thought / Fixation, Eventide, Pulse / Body, Morning state
- New Memory continuity / Topic Identity / Semantic Match / Reviewed View
- Chat Exposure payloads / new sensors
- Model body text / parsed Action / executor result used to infer state
- Rebuilding Track A “to make Planner smarter”

### Spec conflict note

`docs/internal_state_v3_spec.md` Track B mentions “V3 State View + Capability Skill”. This B1-0 contract **does not** introduce a new Capability Skill layer. B1-1 may only reuse capabilities **already resolved** for the Wake run (e.g. selected provider / prepared tool allowlist as facts). Building a new Skill ontology is out of B1 scope.

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

---

## 7. B1-1 allowed scope (next implementation boundary)

When human-approved, B1-1 may:

1. At the recommended insertion point, read the Decision-time authoritative inputs listed above (prefer reusing the freeze-time Drive map + one coordinated Affect/Bond/Longing/clock/`state_version` read at that same seat).
2. Run Planner Shadow (pure function / local module; no production cutover).
3. Emit a structured Shadow Decision matching §4.
4. Persist a **Shadow observation** sufficient for human comparison (format TBD in B1-1; must not be Canonical Event authority).
5. Leave production Decision, Action, executor, and Settlement unchanged.
6. Keep `shadow_only=true`.

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

## 10. Known risks / unresolved

1. **No single packaged “State View” object today.** Drive snapshot is implicit in `decide()['drive']`; Affect / Bond / Longing / `state_version` require coordinated reads at the insertion seat. B1-1 must avoid multi-hop TOCTOU (snapshot drift between Drive freeze and later Affect read).
2. **Legacy `WANT_ACTION` fixed mapping** already shapes production Decision + prompt hint (“倾向于 X 行为”). Shadow vs legacy disagreement is informative; copying `WANT_ACTION` into Planner would fail Q2.
3. **Second `get_drive()`** may occur later in `_wake_build_system_for_plan` (recall_photo nudge) after freeze — production Settlement still uses frozen provenance; Shadow must pin to freeze-time facts, not the nudge re-read.
4. **Live path pre-snapshot side effects** (`drive_engine._flush` no-op, `desire.calibrate_va`) run before `_wake_build_system_for_plan`. Confirm in B1-1 they do not mutate V3 authority (Stage D: flush is retired no-op; calibrate must remain non-authoritative).
5. **`Capability Skill`** wording in `internal_state_v3_spec.md` Track B vs this contract’s “no new Skill layer” — see §3 conflict note.
6. **Observation persistence medium** for Shadow Decisions is intentionally unspecified in B1-0 (must not be Canonical Event ledger authority).
7. **UNRESOLVED:** Whether B1-1 should pass the frozen `decision` object into Shadow as a read-only Drive map vs re-call `read_current_drives` at the gateway seat. Preference: reuse freeze-time `decision['drive']` + one atomic companion read for Affect/Bond/Longing/`state_version` at the same seat; exact API packaging deferred to B1-1 design.

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
| Next | Human review; do not start B1-1 until approved |
