# MEMORY-INTEROP v0.1 (R0)

## Status and scope

MEMORY-INTEROP R0 is a contract-only, runtime-neutral interoperability layer above
MEMORY-KERNEL CORE-0. It defines immutable transport envelopes and future protocol
boundaries. It does not register plugins, select providers, review candidates,
persist idempotency records, or route production memory traffic.

R0 has zero production behavior. No chat, Wake, agent, context-assembly, memory
write/search, database, migration, deployment, or Ombre backend path imports or
uses this module.

## Architectural boundary

The write direction is:

```text
External memory system
        ↓
Adapter
        ↓
SubmissionEnvelope
        ↓
Candidate review/policy
        ↓
MemoryKernel
Evidence / State / Delta
```

An adapter may submit source `Evidence` and propose candidate `State` or `Delta`
payloads. Candidates are transport wrappers, not accepted Kernel objects. A later
review policy may decide whether a proposal is acceptable, and only a future
concrete Kernel committer may call `MemoryKernel` create/revise APIs. Adapters do
not receive raw SQL authority. R0 supplies interfaces for that boundary but no
review engine or production committer.

The read/bootstrap direction is:

```text
MemoryKernel + external stores
        ↓
Selection/Retrieval policy
        ↓
ContextBundle
        ↓
Chat / Wake / Agent
```

`ContextBundle` preserves ordered contributions, source and Kernel references,
provenance, uncertainty, permission/visibility boundaries, and selection reasons.
It does not rank or authorize them. BM25, vector search, reciprocal-rank fusion,
Always-On versus Retrieved policy, bootstrap policy, and context assembly remain
outside this protocol.

## Stable semantic authority

MEMORY-KERNEL remains the stable semantic authority and retains exactly three
semantic primitives:

- `Evidence`: an observed or explicitly derived input with provenance.
- `State`: an accepted, immutable semantic version.
- `Delta`: a recorded transition between accepted State versions.

Interop is protocol and orchestration, not a fourth semantic layer. Types such as
`CandidateState`, `CandidateDelta`, `ContextBundle`, and `CorrectionEnvelope` are
transport objects. They are never persisted directly into Kernel tables and never
silently become accepted `State` or `Delta` records. The CORE-0 schema and its
immutable/supersession rules are unchanged.

## Adapter model

`AdapterDescriptor` advertises an adapter's protocol version, supported operations,
and read/write characteristics. It does not create a global registry or dynamically
load code.

Ombre is one possible plugin, not the foundation of MEMORY-INTEROP. Existing Ombre
selection and behavior are unchanged. Future adapters may use other storage,
retrieval, or lifecycle implementations without changing the Kernel semantics.

SCARLETT may later provide candidate governance, family/volume organization, and
retrieval policy. Those concepts remain outside MEMORY-KERNEL and are not
implemented by R0. Continuity may later contribute provenance-bearing
`ContextItem` values without becoming the Kernel or gaining Kernel commit
authority.

## Existing authority is referenced, not duplicated

`InteropRequestContext.capability_id` and `lease_ref` are references to the existing
UH-A0 capability/lease authority. MEMORY-INTEROP does not implement a second
permission system, validate leases, or widen permissions. A future production
composition must resolve and enforce these references through UH-A0 before any
authorized operation.

Visibility and permission data in `ContextItem` are preserved boundary information
for the caller; their presence is not an authorization decision.

## Submission and candidate lifecycle

A `SubmissionEnvelope` groups one provider-neutral mutation submission:

- source Evidence that may be committed through the accepted path;
- candidate State and Delta payloads proposed for review;
- stable submission and idempotency identities;
- adapter/request provenance and epistemic metadata.

Candidate review status is a transport hint only. `proposed`, `accepted`,
`rejected`, and `conflicted` distinguish protocol state, but R0 neither performs
review nor treats an `accepted` label as commit authorization. No candidate API
silently promotes a proposal.

`CorrectionEnvelope` carries targets, correction Evidence or evidence references,
intent, and provenance. It does not mutate, supersede, invalidate, or delete Kernel
rows. Lifecycle policy and authorization are future work.

## Idempotency semantics

Every mutating submission requires both `submission_id` and `idempotency_key`.
Given an adapter-scoped idempotency key:

- a semantically identical payload may return `duplicate`;
- a different semantic payload must return `conflict`.

R0 defines these meanings but creates no persistence table or in-memory production
registry. `semantic_fingerprint` exists only as a deterministic building block for
tests and later implementations. It rejects values that cannot round-trip through
strict JSON without loss.

For `SubmissionEnvelope`, the semantic fingerprint includes `protocol_version`,
adapter identity, request authority/trigger context, Evidence, candidate semantic
payloads and their support/derivation and epistemic information, and submission
provenance. It excludes candidate `review_status`/`review_hint`, transport identity
and timing (`submission_id`, `idempotency_key`, `submitted_at`, request IDs/turn
IDs and request timestamp) plus every field named `metadata`, because those fields
are explicitly non-semantic routing/diagnostic data. For `CorrectionEnvelope`,
`protocol_version`, adapter identity, target references, Evidence/evidence
references, requested intent, and provenance participate under the same
exclusions. A Correction envelope has no request identity or request-authority
context.

Tuple ordering is semantic and therefore participates in the fingerprint. The
same items in a different order produce a different fingerprint; later policy
must normalize ordering before envelope construction if it intends set semantics.

Changing an included semantic value changes the fingerprint. Changing excluded
transport metadata does not. Duplicate references are rejected where their meaning
would otherwise be ambiguous.

## Replaceable policy and algorithms

The protocol deliberately leaves the following replaceable:

- storage engines and adapter implementations;
- retrieval, ranking, fusion, and bootstrap algorithms;
- candidate review and acceptance policy;
- correction, supersession, invalidation, retention, and deletion policy;
- family/volume organization and lifecycle governance.

`InteropResult`, `SubmissionResult`, and `CorrectionResult` communicate
provider-neutral outcomes such as `accepted`, `duplicate`, `rejected`, `conflict`,
and `unavailable`; they do not implement those policies. A `SubmissionResult`
always carries the submission identity and semantic fingerprint to which its
status applies.

An `AcceptedReview` is the explicit accepted-path token produced by future review
policy. It binds one policy decision to the exact protocol version, submission ID,
and semantic fingerprint. `validate_accepted_review` rejects mismatched identities
or payload fingerprints. Candidate review hints and generic result statuses cannot
substitute for this binding, and R0 still provides no concrete committer.

## R0 non-goals

R0 performs no data migration, shadow write, dual-write, automatic review,
retrieval, deletion, or production database operation. It does not change Ombre,
memory write/search bridges, chat/Wake context assembly, internal MCP wiring,
frontend UI, services, or deployment. Production adoption requires a separately
reviewed phase with explicit authority and runtime evidence.

