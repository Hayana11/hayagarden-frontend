# MEMORY-CONTEXT-G4｜Canonical Context Read Bridge

Status: `G4_STEP1_CONSTRUCTION`

## 1. Goal

Connect the already-frozen GitHub canonical memory contract to Haya Garden through a read-only, fail-safe runtime seam **without** migrating legacy memory, changing production Persona authority, or adding another history channel.

This stage exists so later formal memory migration can become visible to chat without redesigning Forge / Capacity Swap / the resident lifecycle again.

## 2. Authority split frozen for this stage

- Recent exact dialogue: Claude transcript / Manual Forge / Capacity Swap.
- Production Persona today: `chat.persona_store` runtime authority.
- Canonical GitHub archive future identity slot: `identity/persona.md`; it is readable by the bridge but **must not override production Persona in G4**.
- Default-full-read long-term continuity: only `recent/current.md`, and only once at cold/session bootstrap.
- `MEMORY_INDEX.md`: discovery only; never inject its body as conversation context.
- Fact / Precedent / Feel / Shape bodies: on-demand only after an index hit and a later retrieval stage.
- Source originals: verification only; G4 does not open them.
- State / V3: separate authority; G4 does not translate memory into state or action.

## 3. Hard non-duplication rule

When canonical `recent/current.md` is eventually wired into cold-once, it must not coexist as a second copy of the same continuity through legacy Ombre handoff, old text carryover, or current-history packaging.

The final wiring step must therefore decide one authority per continuity payload:

```text
forged / swapped transcript = recent verbatim scene
canonical recent/current     = short cross-window continuity capsule
legacy Ombre handoff         = fallback only while canonical current is unavailable
```

No caller may concatenate all three.

## 4. Read boundary

The G4 reader:

- is disabled by default through `CANONICAL_MEMORY_CONTEXT_ENABLED`;
- has no guessed production archive path;
- requires `HAYAGARDEN_CANONICAL_MEMORY_ROOT` when enabled;
- rejects archive-root symlinks, child symlinks, `..`, absolute indexed paths, and realpath escape;
- reads UTF-8 only;
- refuses oversize current/persona/index files instead of silently truncating;
- treats G2 `EMPTY_G2_SLOT` / `EMPTY` scaffolds as no memory;
- returns diagnostics and no injection on failure;
- performs no Git operation, network request, DB write, `touch`, candidate promotion, Source decryption, or archive mutation.

## 5. Why no production root default

The verified production VPS currently has `/opt/ombre-brain` at the old safety-gate commit `593e2cc...`, while the canonical G2 archive contract was merged later in the private `Hayana11/Aletheia` repository. Treating `/opt/ombre-brain` as canonical would silently read the wrong authority.

Therefore G4 code may land flag-off before archive delivery is decided, but activation must remain blocked until a separately authorized deployment supplies a verified local canonical archive root.

## 6. Steps

### G4-1 — Reader + payload boundary

- add `chat/canonical_memory_context.py`;
- parse only canonical Persona/current/index metadata;
- expose `format_current_for_cold_once()` which returns only current capsule content;
- focused tests for disabled, empty scaffold, populated discovery, oversize fail-safe, and path/symlink rejection.

### G4-2 — Existing cold-once wiring

After G4-1 PASS, wire `format_current_for_cold_once()` into the existing CC `cold_once` builder. Do **not** create a new session hook or resident.

When canonical current is present, the old Ombre handoff copy for the same continuity must be suppressed. When canonical current is unavailable, legacy behavior remains the fallback.

### G4-3 — Flag-off integration acceptance

Using existing test infrastructure only, prove:

1. normal cold start with empty canonical archive is byte/semantic-compatible with legacy behavior;
2. populated canonical current appears exactly once in cold bootstrap;
3. hot turns do not resend it;
4. Forge/Capacity Swap retained transcript is not duplicated by canonical current;
5. failure/missing root falls back without blocking chat.

No new CI, runner, DB, daemon, monitor, or test framework.

### G4-4 — Activation decision (later)

Requires all of:

- a verified local canonical archive delivery path;
- formal archive content that is actually approved for reading (current and/or migrated records);
- explicit Owner authorization for deployment/flag change;
- a separate decision on legacy Ombre handoff retirement/fallback.

G4-4 is not authorized by construction of this Draft PR.

## 7. Explicitly out of scope

- formal migration of the 508 G3 rows;
- resolving duplicate/insufficient holds;
- auto-generating Shape;
- populating or rewriting `identity/persona.md`;
- Curator runtime / automatic canonical writes;
- Aletheia 2 rebuild;
- Relationship Context re-enable;
- State/Behavior/Unified Heartbeat changes;
- Manual Forge or Capacity Swap redesign;
- VPS filesystem changes or deployment.

## 8. Stop conditions

Return `BLOCKED` instead of expanding scope if correct injection requires:

- modifying live VPS before a verified archive root exists;
- bulk-promoting legacy memory first;
- creating another context/session infrastructure path;
- duplicating transcript/carryover/handoff/current;
- reading Source originals by default.
