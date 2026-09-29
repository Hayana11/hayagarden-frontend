"""Provider-neutral MEMORY-INTEROP v0.1 transport contracts.

This module deliberately contains no registry, persistence, selection policy, or
production wiring.  Evidence, State, and Delta remain the complete semantic
kernel.  Candidate wrappers below are proposals only; they cannot commit or
promote their payloads.

Idempotency compares :func:`semantic_fingerprint` values.  For submissions the
semantic fields are adapter_id, the authority/trigger portion of
request_context, evidence, candidate states, candidate deltas, and provenance.
For corrections they are adapter_id, target refs, correction evidence/evidence
refs, intent, and provenance.  Envelope identities, idempotency keys,
timestamps, fields literally named ``metadata``, and candidate review
status/hints are intentionally non-semantic transport data.  Kernel and
proposal identities remain semantic.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from tools.memory_kernel import Delta, Evidence, State


MEMORY_INTEROP_PROTOCOL_VERSION = "0.1"

CandidateReviewStatus = Literal[
    "proposed", "reviewed", "accepted", "rejected", "conflicted"
]
InteropStatus = Literal["accepted", "duplicate", "rejected", "conflict", "unavailable"]


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _optional_text(value: Any, name: str) -> None:
    if value is not None:
        _text(value, name)


def _protocol(value: Any) -> None:
    if value != MEMORY_INTEROP_PROTOCOL_VERSION:
        raise ValueError(
            f"protocol_version must be {MEMORY_INTEROP_PROTOCOL_VERSION!r}"
        )


def _timestamp(value: Any, name: str) -> None:
    _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{name} requires a timezone")


def _confidence(value: Any, name: str = "confidence") -> None:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(f"{name} must be finite and between zero and one")


def _json_round_trip(value: Any, name: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON values") from exc
    if decoded != value:
        raise ValueError(f"{name} must round-trip as JSON without coercion")


def _mapping(value: Any, name: str, *, required: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    if required and not value:
        raise ValueError(f"{name} must be a nonempty JSON object")
    # Plain dictionaries are untrusted transport input and must round-trip
    # without coercion.  Other Mapping implementations (including this
    # module's mappingproxy snapshots) are first reduced to the same canonical
    # JSON shape; arbitrary objects, non-string keys, and non-finite numbers
    # remain rejected by _canonical_json_value.
    if isinstance(value, dict):
        source = value
    elif isinstance(value, MappingProxyType):
        # mappingproxy plus tuple arrays is this module's immutable JSON form.
        source = _canonical_json_value(value)
    else:
        source = dict(value.items())
    _json_round_trip(source, name)
    canonical = _canonical_json_value(source)
    _json_round_trip(canonical, name)
    return _freeze_json(canonical)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _content(value: Any, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be text")


def _clone_evidence(record: Evidence) -> Evidence:
    """Validate CORE-0's public contract and take an immutable JSON snapshot."""

    if type(record) is not Evidence:
        raise TypeError("expected exact Evidence")
    for name in ("evidence_id", "source_type", "source_ref", "origin_kind"):
        _text(getattr(record, name), name)
    if record.origin_kind not in ("source", "derived"):
        raise ValueError("origin_kind must explicitly be source or derived")
    _timestamp(record.observed_at, "observed_at")
    if record.occurred_at is not None:
        _timestamp(record.occurred_at, "occurred_at")
    if not isinstance(record.provenance, Mapping) or not record.provenance:
        raise ValueError("provenance must be a nonempty JSON object")
    if isinstance(record.provenance, dict):
        _json_round_trip(record.provenance, "provenance")
    elif not isinstance(record.provenance, MappingProxyType):
        raise ValueError("provenance must use the Kernel JSON object contract")
    provenance = _canonical_json_value(record.provenance)
    _json_round_trip(provenance, "provenance")
    _content(record.content, "content")
    _optional_text(record.content_ref, "content_ref")
    if record.content is None and record.content_ref is None:
        raise ValueError("Evidence requires content or content_ref")
    _refs(record.participants, "participants")
    _refs(record.entities, "entities")
    _optional_text(record.integrity, "integrity")
    _optional_text(record.visibility, "visibility")
    return Evidence(
        evidence_id=record.evidence_id,
        source_type=record.source_type,
        source_ref=record.source_ref,
        observed_at=record.observed_at,
        provenance=_freeze_json(provenance),
        origin_kind=record.origin_kind,
        occurred_at=record.occurred_at,
        content=record.content,
        content_ref=record.content_ref,
        participants=tuple(record.participants),
        entities=tuple(record.entities),
        integrity=record.integrity,
        visibility=record.visibility,
    )


def _clone_state(record: State) -> State:
    """Validate CORE-0's public contract and take an immutable JSON snapshot."""

    if type(record) is not State:
        raise TypeError("expected exact State")
    for name in (
        "state_id", "scope", "subject_ref", "representation",
        "epistemic_status", "status",
    ):
        _text(getattr(record, name), name)
    _optional_text(record.lineage_id, "lineage_id")
    _refs(record.evidence_refs, "evidence_refs")
    _confidence(record.confidence)
    _timestamp(record.created_at, "created_at")
    _content(record.content, "content")
    structured_value = None
    if record.structured_value is not None:
        if not isinstance(record.structured_value, MappingProxyType):
            _json_round_trip(record.structured_value, "structured_value")
        structured_value = _canonical_json_value(record.structured_value)
        _json_round_trip(structured_value, "structured_value")
    if record.content is None and record.structured_value is None:
        raise ValueError("State requires content or structured_value")
    return State(
        state_id=record.state_id,
        scope=record.scope,
        subject_ref=record.subject_ref,
        representation=record.representation,
        evidence_refs=tuple(record.evidence_refs),
        confidence=record.confidence,
        epistemic_status=record.epistemic_status,
        status=record.status,
        created_at=record.created_at,
        lineage_id=record.lineage_id,
        content=record.content,
        structured_value=_freeze_json(structured_value),
    )


def _clone_delta(record: Delta) -> Delta:
    """Validate CORE-0's public contract and take an immutable JSON snapshot."""

    if type(record) is not Delta:
        raise TypeError("expected exact Delta")
    for name in ("delta_id", "target_scope", "after_ref", "review_status", "rationale_kind"):
        _text(getattr(record, name), name)
    _optional_text(record.before_ref, "before_ref")
    if record.before_ref == record.after_ref:
        raise ValueError("Delta cannot be a self-loop")
    _refs(record.trigger_evidence_refs, "trigger_evidence_refs")
    if not isinstance(record.derived_by, Mapping) or not record.derived_by:
        raise ValueError("derived_by must be a nonempty JSON object")
    if isinstance(record.derived_by, dict):
        _json_round_trip(record.derived_by, "derived_by")
    elif not isinstance(record.derived_by, MappingProxyType):
        raise ValueError("derived_by must use the Kernel JSON object contract")
    derived_by = _canonical_json_value(record.derived_by)
    _json_round_trip(derived_by, "derived_by")
    _confidence(record.confidence)
    _timestamp(record.occurred_at, "occurred_at")
    _content(record.rationale, "rationale")
    _optional_text(record.rationale_ref, "rationale_ref")
    if record.rationale_kind != "derived":
        raise ValueError("Delta rationale must remain derived")
    return Delta(
        delta_id=record.delta_id,
        target_scope=record.target_scope,
        before_ref=record.before_ref,
        after_ref=record.after_ref,
        trigger_evidence_refs=tuple(record.trigger_evidence_refs),
        derived_by=_freeze_json(derived_by),
        confidence=record.confidence,
        occurred_at=record.occurred_at,
        review_status=record.review_status,
        rationale=record.rationale,
        rationale_ref=record.rationale_ref,
        rationale_kind=record.rationale_kind,
    )


def _refs(value: Any, name: str, *, required: bool = False) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    if required and not value:
        raise ValueError(f"{name} must not be empty")
    for ref in value:
        _text(ref, name)
    if len(set(value)) != len(value):
        raise ValueError(f"{name} contains duplicate values")


def _records(value: Any, name: str, record_type: type) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    for record in value:
        if type(record) is not record_type:
            raise TypeError(f"{name} must contain exact {record_type.__name__} values")


def _canonical_json_value(value: Any) -> Any:
    """Return a lossless JSON representation or reject the value.

    Dataclasses are expanded field-by-field rather than through ``asdict`` so
    immutable mapping views are supported and arbitrary objects never receive a
    lossy string representation.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical_json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = _canonical_json_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_canonical_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    raise ValueError(f"value of type {type(value).__name__} is not lossless JSON")


def _fingerprint_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonical_json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, kw_only=True)
class InteropRequestContext:
    protocol_version: str
    request_id: str
    trigger_kind: str
    adapter_id: str
    requested_at: str
    turn_id: str | None = None
    capability_id: str | None = None
    lease_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        for name in ("request_id", "trigger_kind", "adapter_id"):
            _text(getattr(self, name), name)
        for name in ("turn_id", "capability_id", "lease_ref"):
            _optional_text(getattr(self, name), name)
        _timestamp(self.requested_at, "requested_at")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


@dataclass(frozen=True, kw_only=True)
class CandidateState:
    """A proposed State payload; never an accepted Kernel State."""

    proposal_id: str
    state: State
    supporting_refs: tuple[str, ...]
    proposed_by: Mapping[str, Any]
    confidence: float
    epistemic_metadata: Mapping[str, Any]
    review_status: CandidateReviewStatus = "proposed"
    review_hint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.proposal_id, "proposal_id")
        if type(self.state) is not State:
            raise TypeError("state must be an exact State semantic payload")
        object.__setattr__(self, "state", _clone_state(self.state))
        _refs(self.supporting_refs, "supporting_refs")
        _confidence(self.confidence)
        if self.review_status not in ("proposed", "reviewed", "accepted", "rejected", "conflicted"):
            raise ValueError("invalid review_status")
        _optional_text(self.review_hint, "review_hint")
        object.__setattr__(self, "proposed_by", _mapping(self.proposed_by, "proposed_by", required=True))
        object.__setattr__(self, "epistemic_metadata", _mapping(self.epistemic_metadata, "epistemic_metadata"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))

    @property
    def proposed_state(self) -> State:
        """Explicit proposal spelling; this does not promote the payload."""
        return self.state


@dataclass(frozen=True, kw_only=True)
class CandidateDelta:
    """A proposed Delta payload; never an accepted Kernel Delta."""

    proposal_id: str
    delta: Delta
    supporting_refs: tuple[str, ...]
    proposed_by: Mapping[str, Any]
    confidence: float
    epistemic_metadata: Mapping[str, Any]
    review_status: CandidateReviewStatus = "proposed"
    review_hint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.proposal_id, "proposal_id")
        if type(self.delta) is not Delta:
            raise TypeError("delta must be an exact Delta semantic payload")
        object.__setattr__(self, "delta", _clone_delta(self.delta))
        _refs(self.supporting_refs, "supporting_refs")
        _confidence(self.confidence)
        if self.review_status not in ("proposed", "reviewed", "accepted", "rejected", "conflicted"):
            raise ValueError("invalid review_status")
        _optional_text(self.review_hint, "review_hint")
        object.__setattr__(self, "proposed_by", _mapping(self.proposed_by, "proposed_by", required=True))
        object.__setattr__(self, "epistemic_metadata", _mapping(self.epistemic_metadata, "epistemic_metadata"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))

    @property
    def proposed_delta(self) -> Delta:
        """Explicit proposal spelling; this does not promote the payload."""
        return self.delta


@dataclass(frozen=True, kw_only=True)
class SubmissionEnvelope:
    protocol_version: str
    submission_id: str
    idempotency_key: str
    adapter_id: str
    request_context: InteropRequestContext
    evidence: tuple[Evidence, ...]
    candidate_states: tuple[CandidateState, ...]
    candidate_deltas: tuple[CandidateDelta, ...]
    submitted_at: str
    provenance: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        for name in ("submission_id", "idempotency_key", "adapter_id"):
            _text(getattr(self, name), name)
        if type(self.request_context) is not InteropRequestContext:
            raise TypeError("request_context must be InteropRequestContext")
        if self.request_context.protocol_version != self.protocol_version:
            raise ValueError("request_context protocol_version does not match submission")
        if self.request_context.adapter_id != self.adapter_id:
            raise ValueError("request_context adapter_id does not match submission")
        _records(self.evidence, "evidence", Evidence)
        object.__setattr__(self, "evidence", tuple(_clone_evidence(item) for item in self.evidence))
        _records(self.candidate_states, "candidate_states", CandidateState)
        _records(self.candidate_deltas, "candidate_deltas", CandidateDelta)
        if not (self.evidence or self.candidate_states or self.candidate_deltas):
            raise ValueError("submission requires at least one proposed semantic payload")
        identities = [item.evidence_id for item in self.evidence]
        identities += [item.proposal_id for item in self.candidate_states]
        identities += [item.proposal_id for item in self.candidate_deltas]
        if len(set(identities)) != len(identities):
            raise ValueError("submission contains duplicate evidence/proposal identities")
        state_ids = [item.state.state_id for item in self.candidate_states]
        if len(set(state_ids)) != len(state_ids):
            raise ValueError("candidate_states contains duplicate state_id values")
        delta_ids = [item.delta.delta_id for item in self.candidate_deltas]
        if len(set(delta_ids)) != len(delta_ids):
            raise ValueError("candidate_deltas contains duplicate delta_id values")
        _timestamp(self.submitted_at, "submitted_at")
        object.__setattr__(self, "provenance", _mapping(self.provenance, "provenance", required=True))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))

    def semantic_fingerprint(self) -> str:
        return semantic_fingerprint(self)


@dataclass(frozen=True, kw_only=True)
class AdapterDescriptor:
    adapter_id: str
    adapter_version: str
    protocol_version: str
    supported_operations: tuple[str, ...]
    readable: bool
    writable: bool
    supported_capabilities: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("adapter_id", "adapter_version"):
            _text(getattr(self, name), name)
        _protocol(self.protocol_version)
        _refs(self.supported_operations, "supported_operations", required=True)
        _refs(self.supported_capabilities, "supported_capabilities")
        allowed = {"ingest", "retrieve", "correct", "lifecycle", "context_contribute"}
        if not set(self.supported_operations) <= allowed:
            raise ValueError("supported_operations contains an unknown operation")
        if type(self.readable) is not bool or type(self.writable) is not bool:
            raise ValueError("readable and writable must be booleans")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


@dataclass(frozen=True, kw_only=True)
class ContextItem:
    item_id: str
    source_adapter_id: str
    provenance: Mapping[str, Any]
    content: str | None = None
    content_ref: str | None = None
    source_refs: tuple[str, ...] = ()
    kernel_refs: tuple[str, ...] = ()
    confidence: float | None = None
    epistemic_status: str | None = None
    visibility: str | None = None
    permission_boundary: Mapping[str, Any] = field(default_factory=dict)
    retrieval_reason: str | None = None
    selection_metadata: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.item_id, "item_id")
        _text(self.source_adapter_id, "source_adapter_id")
        if self.content is None and self.content_ref is None:
            raise ValueError("ContextItem requires content or content_ref")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("content must be text")
        _optional_text(self.content_ref, "content_ref")
        _refs(self.source_refs, "source_refs")
        _refs(self.kernel_refs, "kernel_refs")
        if self.confidence is not None:
            _confidence(self.confidence)
        for name in ("epistemic_status", "visibility", "retrieval_reason"):
            _optional_text(getattr(self, name), name)
        object.__setattr__(self, "provenance", _mapping(self.provenance, "provenance", required=True))
        object.__setattr__(self, "permission_boundary", _mapping(self.permission_boundary, "permission_boundary"))
        object.__setattr__(self, "selection_metadata", _mapping(self.selection_metadata, "selection_metadata"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


@dataclass(frozen=True, kw_only=True)
class ContextBundle:
    bundle_id: str
    protocol_version: str
    generated_at: str
    contributor_adapter_ids: tuple[str, ...]
    items: tuple[ContextItem, ...]
    request_id: str | None = None
    turn_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.bundle_id, "bundle_id")
        _protocol(self.protocol_version)
        _optional_text(self.request_id, "request_id")
        _optional_text(self.turn_id, "turn_id")
        _timestamp(self.generated_at, "generated_at")
        _refs(self.contributor_adapter_ids, "contributor_adapter_ids")
        _records(self.items, "items", ContextItem)
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("items contains duplicate item_id values")
        contributors = set(self.contributor_adapter_ids)
        if any(item.source_adapter_id not in contributors for item in self.items):
            raise ValueError("every ContextItem source adapter must be a contributor")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


@dataclass(frozen=True, kw_only=True)
class CorrectionEnvelope:
    protocol_version: str
    correction_id: str
    idempotency_key: str
    adapter_id: str
    target_refs: tuple[str, ...]
    intent: str
    submitted_at: str
    provenance: Mapping[str, Any]
    correction_evidence: tuple[Evidence, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        for name in ("correction_id", "idempotency_key", "adapter_id", "intent"):
            _text(getattr(self, name), name)
        _refs(self.target_refs, "target_refs", required=True)
        _records(self.correction_evidence, "correction_evidence", Evidence)
        object.__setattr__(
            self,
            "correction_evidence",
            tuple(_clone_evidence(item) for item in self.correction_evidence),
        )
        _refs(self.evidence_refs, "evidence_refs")
        if not (self.correction_evidence or self.evidence_refs):
            raise ValueError("correction requires Evidence or evidence_refs")
        evidence_ids = [item.evidence_id for item in self.correction_evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("correction_evidence contains duplicate evidence identities")
        if set(evidence_ids) & set(self.evidence_refs):
            raise ValueError("correction evidence is ambiguously supplied by value and ref")
        _timestamp(self.submitted_at, "submitted_at")
        object.__setattr__(self, "provenance", _mapping(self.provenance, "provenance", required=True))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))

    def semantic_fingerprint(self) -> str:
        return semantic_fingerprint(self)


@dataclass(frozen=True, kw_only=True)
class InteropResult:
    protocol_version: str
    status: InteropStatus
    adapter_id: str
    request_id: str | None = None
    refs: tuple[str, ...] = ()
    message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        if self.status not in ("accepted", "duplicate", "rejected", "conflict", "unavailable"):
            raise ValueError("invalid result status")
        _text(self.adapter_id, "adapter_id")
        for name in ("request_id", "message"):
            _optional_text(getattr(self, name), name)
        _refs(self.refs, "refs")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


@dataclass(frozen=True, kw_only=True)
class SubmissionResult:
    protocol_version: str
    status: InteropStatus
    adapter_id: str
    submission_id: str
    semantic_fingerprint: str
    refs: tuple[str, ...] = ()
    message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        if self.status not in ("accepted", "duplicate", "rejected", "conflict", "unavailable"):
            raise ValueError("invalid result status")
        for name in ("adapter_id", "submission_id"):
            _text(getattr(self, name), name)
        _semantic_fingerprint_text(self.semantic_fingerprint)
        _refs(self.refs, "refs")
        _optional_text(self.message, "message")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


@dataclass(frozen=True, kw_only=True)
class CorrectionResult:
    protocol_version: str
    status: InteropStatus
    adapter_id: str
    correction_id: str
    semantic_fingerprint: str
    refs: tuple[str, ...] = ()
    message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        if self.status not in ("accepted", "duplicate", "rejected", "conflict", "unavailable"):
            raise ValueError("invalid result status")
        for name in ("adapter_id", "correction_id"):
            _text(getattr(self, name), name)
        _semantic_fingerprint_text(self.semantic_fingerprint)
        _refs(self.refs, "refs")
        _optional_text(self.message, "message")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


def _semantic_fingerprint_text(value: Any) -> None:
    _text(value, "semantic_fingerprint")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("semantic_fingerprint must be a lowercase SHA-256 digest")


@dataclass(frozen=True, kw_only=True)
class AcceptedReview:
    """Explicit policy acceptance bound to exactly one submission payload."""

    protocol_version: str
    review_id: str
    policy_id: str
    submission_id: str
    semantic_fingerprint: str
    reviewed_at: str
    provenance: Mapping[str, Any]
    status: Literal["accepted"] = "accepted"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        for name in ("review_id", "policy_id", "submission_id"):
            _text(getattr(self, name), name)
        if self.status != "accepted":
            raise ValueError("AcceptedReview status must be accepted")
        _semantic_fingerprint_text(self.semantic_fingerprint)
        _timestamp(self.reviewed_at, "reviewed_at")
        object.__setattr__(self, "provenance", _mapping(self.provenance, "provenance", required=True))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))


def validate_accepted_review(
    envelope: SubmissionEnvelope,
    review: AcceptedReview,
) -> AcceptedReview:
    """Validate that an accepted policy decision is bound to ``envelope``."""

    if type(envelope) is not SubmissionEnvelope:
        raise TypeError("envelope must be SubmissionEnvelope")
    if type(review) is not AcceptedReview:
        raise TypeError("review must be AcceptedReview")
    if review.protocol_version != envelope.protocol_version:
        raise ValueError("accepted review protocol_version does not match submission")
    if review.submission_id != envelope.submission_id:
        raise ValueError("accepted review submission_id does not match submission")
    if review.semantic_fingerprint != envelope.semantic_fingerprint():
        raise ValueError("accepted review semantic_fingerprint does not match submission")
    return review


def semantic_fingerprint(envelope: SubmissionEnvelope | CorrectionEnvelope) -> str:
    """Return the canonical semantic fingerprint used by future idempotency stores.

    A matching idempotency key and matching fingerprint may return ``duplicate``;
    a matching key with a different fingerprint must return ``conflict``.  This
    helper records neither key nor result.
    """

    if type(envelope) is SubmissionEnvelope:
        context = envelope.request_context
        candidate_states = tuple(
            {
                "proposal_id": candidate.proposal_id,
                "state": candidate.state,
                "supporting_refs": candidate.supporting_refs,
                "proposed_by": candidate.proposed_by,
                "confidence": candidate.confidence,
                "epistemic_metadata": candidate.epistemic_metadata,
            }
            for candidate in envelope.candidate_states
        )
        candidate_deltas = tuple(
            {
                "proposal_id": candidate.proposal_id,
                "delta": candidate.delta,
                "supporting_refs": candidate.supporting_refs,
                "proposed_by": candidate.proposed_by,
                "confidence": candidate.confidence,
                "epistemic_metadata": candidate.epistemic_metadata,
            }
            for candidate in envelope.candidate_deltas
        )
        return _fingerprint_payload(
            {
                "kind": "submission",
                "protocol_version": envelope.protocol_version,
                "adapter_id": envelope.adapter_id,
                "request_context": {
                    "protocol_version": context.protocol_version,
                    "trigger_kind": context.trigger_kind,
                    "adapter_id": context.adapter_id,
                    "capability_id": context.capability_id,
                    "lease_ref": context.lease_ref,
                },
                "evidence": envelope.evidence,
                # Review status/hints and all metadata are transport observations,
                # not part of the proposed semantic payload.
                "candidate_states": candidate_states,
                "candidate_deltas": candidate_deltas,
                "provenance": envelope.provenance,
            }
        )
    if type(envelope) is CorrectionEnvelope:
        return _fingerprint_payload(
            {
                "kind": "correction",
                "protocol_version": envelope.protocol_version,
                "adapter_id": envelope.adapter_id,
                "target_refs": envelope.target_refs,
                "correction_evidence": envelope.correction_evidence,
                "evidence_refs": envelope.evidence_refs,
                "intent": envelope.intent,
                "provenance": envelope.provenance,
            }
        )
    raise TypeError("expected SubmissionEnvelope or CorrectionEnvelope")


@runtime_checkable
class MemoryAdapter(Protocol):
    """Future plugin boundary: adapters propose or retrieve, never persist Kernel rows."""

    @property
    def descriptor(self) -> AdapterDescriptor: ...

    def submit(self, envelope: SubmissionEnvelope) -> SubmissionResult: ...

    def retrieve(self, context: InteropRequestContext) -> ContextBundle: ...

    def correct(self, envelope: CorrectionEnvelope) -> CorrectionResult: ...


@runtime_checkable
class ReviewPolicy(Protocol):
    """Future policy boundary.  Returning accepted is an explicit review decision."""

    def review(self, envelope: SubmissionEnvelope) -> AcceptedReview | SubmissionResult: ...


@runtime_checkable
class KernelCommitter(Protocol):
    """Future privileged boundary; implementations must accept reviewed input only.

    R0 intentionally provides no implementation.  A future committer may call
    MemoryKernel create/revise APIs after an explicit accepted review, but must
    never grant adapters raw SQL authority or mutate an accepted State.
    """

    def commit(
        self,
        envelope: SubmissionEnvelope,
        accepted_review: AcceptedReview,
    ) -> SubmissionResult: ...


__all__ = [
    "MEMORY_INTEROP_PROTOCOL_VERSION",
    "AcceptedReview",
    "AdapterDescriptor",
    "CandidateDelta",
    "CandidateReviewStatus",
    "CandidateState",
    "ContextBundle",
    "ContextItem",
    "CorrectionEnvelope",
    "CorrectionResult",
    "InteropRequestContext",
    "InteropResult",
    "InteropStatus",
    "KernelCommitter",
    "MemoryAdapter",
    "ReviewPolicy",
    "SubmissionEnvelope",
    "SubmissionResult",
    "semantic_fingerprint",
    "validate_accepted_review",
]

