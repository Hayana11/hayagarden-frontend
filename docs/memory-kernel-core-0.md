# MEMORY-KERNEL CORE-0

Status: rework candidate; CORE-0 implementation and dedicated CI are evaluated separately from historical repository contract drift.
Production semantics: **NO-OP / NOT WIRED**. No merge or deployment is authorized.

Design source: the supplied “MEMORY-KERNEL v0.3｜稳定记忆内核与可插拔连续性协议”
and CORE-0 request. No repository copy was found during baseline investigation.
Base: 571f9eab0e95ebe651c19e37db38449fe8e9b634 (main and production at investigation).
Branch: codex/memory-kernel-core-0.

## Architecture and acceptance boundary

The only semantic objects are frozen Python dataclasses Evidence, State, Delta
in tools/memory_kernel.py. MemoryKernel is their repository/service, not another
semantic object. SQLite storage uses exactly three namespaced tables:
memory_kernel_evidence, memory_kernel_state, memory_kernel_delta.

This reuses the repository's sqlite3, explicit DB path, BEGIN IMMEDIATE,
idempotent schema-helper and unittest conventions. It adds no runtime dependency,
database service, generic migration framework, surface, processor or adapter.
JSON1 is required; it is available in the tested SQLite 3.37.2 environment.

The constructor requires a file path, has no production default and performs no
I/O. initialize() is explicitly invoked on an isolated fixture database.
Importing the module does not import config_store, app, gateway, or providers.

## API contract

- create_evidence(Evidence) persists one source or explicitly derived record.
- create_state(State, Delta) atomically persists first formation. before_ref
  must be null, after_ref must identify the new State, target_scope must match.
- revise_state(State, Delta) atomically appends a semantic version and its edge.
  before_ref must exist, the ID must be new, lineage must match (including null),
  and a semantic field must change. A comparison alone cannot create a Delta.
- get_evidence/get_state/get_delta read precise IDs and raise KeyError if absent.
- lineage_history returns a persisted version list in insertion order, not a unique
  path; branching must be reconstructed from Delta edges, without selecting a head.
- transitions_for_state returns both incoming and outgoing transitions.
- evidence_for_state/evidence_for_delta resolve their original evidence references.

Callers supply IDs and timezone-bearing timestamps. No UUID policy, clock policy,
automatic extraction, current-head selection, approval policy, or authority
matrix is introduced. Scope, representation, epistemic_status and status are
open strings. These are not model-specific enums.

All persisted State records are accepted semantic versions, including tentative
or contested knowledge; accepted storage does not mean confirmed truth.
CORE-0 does not implement mutable candidate staging or a review workflow.

## Immutability and evidence

There is no update or delete service API, including metadata update. SQLite
triggers reject UPDATE, UPSERT and INSERT OR REPLACE attempts on existing identities.
This protects evidence basis, epistemic status, scope, confidence and status. No
status downgrade can unlock old content. A superseding transition leaves the old
status untouched. CORE-0 deliberately has no deletion surface; deletion governance
is deferred to a later lifecycle/authorization protocol. The absence of a delete
API is not a permanent storage-law prohibition on future authorized deletion.

Evidence and trigger references are JSON arrays in their immutable owner rows.
Insert triggers resolve each reference against the Evidence table; there is no
separate mutable join row that could silently alter a state's evidence basis.
Missing or non-text references fail. Evidence can be reused by arbitrary states
and transitions without copying source content.

Evidence requires explicit origin_kind=source or derived plus nonempty
provenance. Source references and externally stored content references are retained
without dereferencing. Derived evidence cannot be promoted in place. The kernel
does not verify an external source's authenticity or discover a dishonest caller's
misclassification; it preserves the caller's explicit provenance and origin.

Frozen dataclasses return detached snapshots. Nested JSON collections may be
mutated by a caller in memory; every persistence/read operation serializes or
decodes independently, so mutation cannot write through. Non-JSON or lossy values,
duplicate references, non-finite confidence and naive timestamps are rejected.

## Lineage and Delta

A non-null lineage can be formed only once through the service, serialized by the
write transaction. Revisions retain lineage. A null lineage stays null; its
explicit Delta edges still preserve history. Branches from a historical state
are allowed; this kernel deliberately does not select a current head.

Every stored State has exactly one formation Delta through the public API.
Delta.after_ref is unique, before/after use foreign keys, self-loops fail, and a
predecessor must already have a formation record and precede its successor.
The SQL trigger also checks target scope and null-safe lineage equality.

Delta states that a transition was recorded. trigger_evidence_refs identify its
used/triggered/supported inputs, never real-world causes. rationale_kind is
constrained to derived and immutable; an inline rationale is only a non-authoritative
transition annotation. rationale is not Evidence, not a causal fact, and not the
truth of the transition. A rationale_ref may point to a future external derived
recognition; CORE-0 does not implement a Hypothesis object. No rationale is
automatically copied into a fact or Evidence.

## Transaction and migration

Each write owns an idle connection, enables foreign keys, takes BEGIN IMMEDIATE,
and commits or rolls back before closing. State insertion followed by Delta
insertion occurs in a single transaction. Fault injection at Delta insertion
proves a newly inserted State is rolled back. State failure never creates a Delta.
Already committed evidence is independent and is not deleted on revision failure.

ensure_memory_kernel_schema implements the additive v1 upgrade, using the existing
repository's CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS pattern. It owns a transaction
and rejects active caller transactions. There is no application startup wiring,
legacy backfill, global PRAGMA user_version change or production migration.

Verified paths: empty DB, synthetic legacy-only DB, repeated initialization with
existing kernel history, foreign_key_check, preservation of legacy rows/schema
and user_version, and rollback when DDL authorization fails.
No generic reversible migration framework was found in the inspected persistence
paths. The schema neither creates nor removes a no-delete trigger. CORE-0 has no delete
API or lifecycle behavior; future deletion governance remains outside this kernel.
No downgrade is added:
destructive removal is outside CORE-0's contract.
An import cannot upgrade any database.

MemoryKernel service/domain API is the authoritative semantic write boundary.
Database constraints provide defense-in-depth for selected integrity invariants.
Direct raw SQL is outside the supported semantic contract and does not receive all
service-level validation: it can bypass atomic State+Delta formation, timezone
validation, duplicate-ref checks, provenance checks, JSON semantic checks, and
service-level lineage/revision rules. Administrators who remove triggers or replace
the database file are outside this contract. No deletion/lifecycle system is added.

## Validation and known regression blockers

Run:
    python -m ruff check tools/memory_kernel.py tools/memory_kernel_schema.py \
        tests/test_memory_kernel.py tests/test_memory_kernel_legacy_isolation.py
    python -m unittest tests.test_memory_kernel tests.test_memory_kernel_legacy_isolation -v

Focused semantic/migration tests: 31 passed on Python 3.10 / SQLite 3.37.2.
This covers requested CASE 1-9, provenance, reference-only content, direct SQL
immutability, generated summaries, no-op comparisons, null lineage, branching,
clock-independent ordering, concurrent formation, schema rollback and JSON validation.
Import/constructor isolation and absence of production importers: 2 passed.
Ruff 0.12.12 default checks: passed. No production SPA build is needed: no build
surface, runtime import, frontend asset or dependency manifest is changed.

CASE 10 runs the original legacy tests in separate processes, using synthetic
SQLite/Markdown fixtures. Exact legacy .env, main DB and external-MCP DB paths map
to temporary fixtures. A Python audit hook rejects other out-of-sandbox DB access,
production-file access, network and subprocess calls, and records even caught
denials. This is test-only redirection, not a production adapter change.

Existing regression outcomes after corrected isolation fixture:
- tests.test_memory_library: 14/14 passed.
- tests.test_memory_write_bridge: 6/6 passed.
- tests.test_memory_unification_audit: 1/1 passed.
- tests.test_m3_04c_daily_memory_cutover: 2/5 passed; three assertions are
  BASELINE STALE CONTRACTS. The unchanged base with the same corrected fixture
  fails the same three Home/Internal MCP grouping assertions; current
  cc_capability_adapter/manifest is authoritative. This is historical contract
  cleanup pending, not a PR-introduced regression.

## Diff review and handoff

Only two new kernel modules, two new test modules, this document, and one CI workflow
are added. No legacy provider, recall, memory import, chat injection or production
startup file changes. No fourth semantic object, HTTP/UI/MCP entrypoint, silent
fallback, semantic UPDATE, Interop or Aletheia implementation.

Completed: CORE-0 implementation, focused tests, migration proof, lint, corrected
isolation fixture, and unchanged-base comparison. Remaining repository work is
historical contract cleanup for three stale Daily Memory assertions. Unique next
step: review this Draft PR; do not mix that cleanup into CORE-0.
Do not merge or deploy. The kernel remains unused by production.


## Final rework decision

Final base SHA: 571f9eab0e95ebe651c19e37db38449fe8e9b634
Old HEAD: f3f11fedcb7666e1984840eb43e15558f46c677f
Schema cleanup commit: dbafe306f10bffb0ef9fee92abed8bd657aecd8f
Branch: codex/memory-kernel-core-0
PR: https://github.com/Hayana11/hayagarden-frontend/pull/423

FIX 1 — LEGACY ISOLATION: PASS. The fixture creates the minimum runtime_config
(key/value/updated_at) schema and writes no capability override, so memory.write
naturally reads INHERIT. Corrected isolation has Memory Write Bridge 6/6,
Memory Library 14/14 and Memory Unification Audit 1/1. The same harness on a
clean unchanged base reaches the same three historical Daily Memory failures;
it records no production, network or out-of-sandbox access.

FIX 2 — DELETE BOUNDARY: PASS. CORE-0 has no delete service/API and no lifecycle
authorization. The schema protects existing identities from UPDATE, REPLACE and
UPSERT, but neither creates nor removes a no-delete trigger. Foreign keys can
still reject deletion that violates an actual relational reference; that is
integrity defense, not a frozen deletion policy.

FIX 3 — DELTA RATIONALE: PASS. Rationale is an immutable derived transition
annotation. It is not Evidence, not a causal fact, and not transition truth.
rationale_ref remains an external reference seam; CORE-0 has no Hypothesis object.
trigger_evidence_refs are used/triggered/supported inputs, never declared
real-world causes. Focused tests prove rationale does not create an Evidence row.

LINEAGE CONTRACT: PASS. lineage_history remains the public compatibility name,
but its contract is a persisted version list in insertion order, not a unique
path. Branching is allowed; Delta edges reconstruct paths; no current head is
selected.

RAW SQL TRUST BOUNDARY: PASS. MemoryKernel service/domain validation is the
authoritative semantic write boundary. Database constraints are defense-in-depth
for selected invariants. Raw SQL is unsupported and can bypass atomic formation,
timestamp, duplicate-ref, provenance, JSON and service-level lineage/revision
validation.

STATE IMMUTABILITY: PASS. Existing semantic State UPDATE, REPLACE and UPSERT
remain rejected; revision creates a new State ID and Delta. No accepted semantic
payload is overwritten.

TRANSACTION CONTRACT: PASS. State plus Delta writes remain one BEGIN IMMEDIATE
transaction with rollback tests.

LEGACY ISOLATION: PASS for PR attribution. No production provider, recall,
chat injection, migration, adapter, HTTP/MCP/UI or dual-write changes exist.

TEST RESULTS:
- Focused CORE-0 implementation checks: 31 passed on both Python 3.10 and 3.11.
- Isolation checks: 3 passed except the expected subtest report for the three
  stale Daily Memory assertions; no access violations.
- Memory Library: 14/14 passed.
- Memory Write Bridge: 6/6 passed.
- Memory Unification Audit: 1/1 passed.
- Daily Memory Cutover: 2/5 current assertions pass; 3 are baseline stale
  contract assertions, identical on unchanged base.
- Ruff: passed on both Python versions.
- Whitespace: code content and remote tree were verified; dedicated CI completed
  both matrix jobs.
- Dedicated CI run 34080996744: both Python jobs completed, lint passed, all
  CORE-0 tests passed; job conclusion is failure only because the isolation
  harness intentionally exposes the three historical stale assertions.

BASELINE STALE CONTRACTS: pending cleanup outside #423. They are the Home/Internal
MCP grouping assertions in test_home_legacy_and_fence_lookup_remain_explicit,
test_manifest_grouping_and_loading_are_exact, and
test_normal_surface_has_only_internal_memory. Current cc_capability_adapter and
manifest remain authoritative; neither candidate nor base changed them.

DIFF REVIEW: no unrelated changes, production wiring, silent fallback, semantic
UPDATE, permanent deletion policy, fourth semantic object, Interop or Aletheia.

FINAL DECISION:
CORE-0 IMPLEMENTATION: PASS
CORE-0 DEDICATED CI: NOT PASS (job reports the three explicitly proven stale
historical assertions; both Python jobs completed).
PR-INTRODUCED LEGACY REGRESSION: NO
HISTORICAL CONTRACT CLEANUP: PENDING in separate PR #424
CORE-0 BASELINE CANDIDATE: YES, pending merge of separate PR #424 and the
subsequent #423 rebase plus dedicated CI rerun.
Do not merge or deploy.
