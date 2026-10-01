# Ombre 3.6.14 READ SHADOW R4A

R4A is a migration observer only. Legacy Ombre remains authoritative for every
caller-visible result.

## Authority and fail-open behavior

The observer:

- is default OFF and requires exact-string 1 gates;
- dispatches only when OMBRE_ADAPTER_BACKEND=legacy_module;
- freezes hashes, IDs, counts, and numeric metadata before dispatch;
- returns the authoritative object unchanged without waiting for the worker;
- cannot write legacy Ombre, sidecar Markdown, vectors, or MCP state;
- cannot affect the authoritative operation success or failure;
- uses at most two detached workers and drops observations when both slots are
  occupied;
- stores only receipt-safe hashes, IDs, counts, statuses, and numbers.

R4A does not grant authority to the 3.6.14 sidecar and is not coupled to
Memory Kernel or MEMORY-INTEROP.

The sidecar is localhost-only on 127.0.0.1:18002 and runs with
decay_engine=None, embedding_outbox=None, and you_service=None.

## Operation gates

Master:

OMBRE_READ_SHADOW_ENABLED

Per operation:

- OMBRE_READ_SHADOW_HANDOFF_ENABLED
- OMBRE_READ_SHADOW_RECORDS_ENABLED
- OMBRE_READ_SHADOW_EMOTION_ENABLED
- OMBRE_READ_SHADOW_SEARCH_ENABLED

Only the exact value 1 enables a gate. Values such as true, yes, on, and TRUE
remain OFF. The search gate remains OFF for real-user traffic.

## Privacy

Handoff and normalized-record observations persist lengths, hashes, IDs, and
safe metadata only. Raw handoff text and raw memory content are never written
to the receipt database or logs.

Semantic Search Shadow sends the raw search query to the configured embedding
provider for query-vector generation. Therefore real-user Search Shadow stays
OFF in R4A. The isolated acceptance process may use only these fixed canaries:

- 记忆系统
- 房间装修
- 上下文压缩

The raw canary query is transient worker input and is persisted only as a
SHA-256 query hash.

## Status interpretation

exact means the observed safe fields match. same_set_different_order records
an ordering difference without calling it content failure. partial_overlap and
divergent_valid are review diagnostics. lifecycle_only_drift is reserved for
ID/content/semantic equality with only last_active differences. shadow_unavailable,
shadow_timeout, shadow_error, and dropped_busy are fail-open observer outcomes.

R4A has no write-shadow path. Functions such as hold, trace, grow, release,
anchor, plan, and emotion update are intentionally outside this observer.

## Receipt location

The dedicated database is supplied by OMBRE_READ_SHADOW_DB_PATH, for example:

/var/lib/hayagarden/ombre-shadow-r4a/receipts/ombre-read-shadow.db

It must not resolve to a production Ombre path, production memories database,
or any Ombre embeddings database.
## R4B adapter wiring

The R4B adapter calls the R4A observer only after the authoritative legacy
result has been fully produced for:

- get_handoff
- list_memory_records
- get_memory_record
- get_emotion_snapshot
- search_memories

The adapter returns that same authoritative value immediately. Observer import,
dispatch, spawn, and observer exceptions are fail-open. HTTP backend reads do not
import or recurse through the observer. Write and surface paths remain
unobserved. The master and per-operation gates above remain the only dispatch
authority, and search remains OFF by default.
