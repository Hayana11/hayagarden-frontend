import dataclasses
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.memory_interop import (
    MEMORY_INTEROP_PROTOCOL_VERSION,
    AcceptedReview,
    AdapterDescriptor,
    CandidateDelta,
    CandidateState,
    ContextBundle,
    ContextItem,
    CorrectionEnvelope,
    InteropRequestContext,
    SubmissionEnvelope,
    SubmissionResult,
    semantic_fingerprint,
    validate_accepted_review,
)
from tools.memory_kernel import Delta, Evidence, State


UTC_1 = "2026-09-29T08:00:00+00:00"
UTC_2 = "2026-09-29T08:01:00Z"


def evidence(**changes):
    values = {
        "evidence_id": "ev-1",
        "source_type": "adapter",
        "source_ref": "external:1",
        "observed_at": UTC_1,
        "provenance": {"adapter": "example"},
        "origin_kind": "source",
        "content": "observed text",
    }
    values.update(changes)
    return Evidence(**values)


def state(**changes):
    values = {
        "state_id": "state-1",
        "scope": "person",
        "subject_ref": "subject:1",
        "representation": "text",
        "evidence_refs": ("ev-1",),
        "confidence": 0.8,
        "epistemic_status": "supported",
        "status": "active",
        "created_at": UTC_1,
        "content": "candidate state",
    }
    values.update(changes)
    return State(**values)


def delta(**changes):
    values = {
        "delta_id": "delta-1",
        "target_scope": "person",
        "before_ref": None,
        "after_ref": "state-1",
        "trigger_evidence_refs": ("ev-1",),
        "derived_by": {"adapter": "example"},
        "confidence": 0.75,
        "occurred_at": UTC_1,
        "review_status": "proposed",
    }
    values.update(changes)
    return Delta(**values)


def request_context(**changes):
    values = {
        "protocol_version": MEMORY_INTEROP_PROTOCOL_VERSION,
        "request_id": "req-1",
        "turn_id": "turn-1",
        "trigger_kind": "chat",
        "adapter_id": "adapter.example",
        "capability_id": "capability.memory",
        "lease_ref": "lease:1",
        "requested_at": UTC_1,
        "metadata": {"trace": "a"},
    }
    values.update(changes)
    return InteropRequestContext(**values)


def candidate_state(**changes):
    values = {
        "proposal_id": "proposal-state-1",
        "state": state(),
        "supporting_refs": ("ev-1",),
        "proposed_by": {"adapter": "example"},
        "confidence": 0.7,
        "epistemic_metadata": {"basis": "source"},
        "review_status": "proposed",
        "metadata": {"trace": "a"},
    }
    values.update(changes)
    return CandidateState(**values)


def candidate_delta(**changes):
    values = {
        "proposal_id": "proposal-delta-1",
        "delta": delta(),
        "supporting_refs": ("ev-1",),
        "proposed_by": {"adapter": "example"},
        "confidence": 0.7,
        "epistemic_metadata": {"basis": "source"},
        "review_status": "proposed",
        "metadata": {"trace": "a"},
    }
    values.update(changes)
    return CandidateDelta(**values)


def submission(**changes):
    values = {
        "protocol_version": MEMORY_INTEROP_PROTOCOL_VERSION,
        "submission_id": "submission-1",
        "idempotency_key": "idempotency-1",
        "adapter_id": "adapter.example",
        "request_context": request_context(),
        "evidence": (evidence(),),
        "candidate_states": (candidate_state(),),
        "candidate_deltas": (candidate_delta(),),
        "submitted_at": UTC_2,
        "provenance": {"adapter": "example", "run": "1"},
        "metadata": {"trace": "a"},
    }
    values.update(changes)
    return SubmissionEnvelope(**values)


def correction(**changes):
    values = {
        "protocol_version": MEMORY_INTEROP_PROTOCOL_VERSION,
        "correction_id": "correction-1",
        "idempotency_key": "correction-key-1",
        "adapter_id": "adapter.example",
        "target_refs": ("state-1",),
        "correction_evidence": (evidence(evidence_id="ev-correction"),),
        "evidence_refs": (),
        "intent": "supersede after review",
        "submitted_at": UTC_2,
        "provenance": {"adapter": "example"},
        "metadata": {"trace": "a"},
    }
    values.update(changes)
    return CorrectionEnvelope(**values)


class MemoryInteropContractTests(unittest.TestCase):
    def test_contracts_and_nested_metadata_are_immutable(self):
        envelope = submission()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            envelope.submission_id = "changed"
        with self.assertRaises(TypeError):
            envelope.metadata["trace"] = "changed"
        with self.assertRaises(TypeError):
            envelope.request_context.metadata["trace"] = "changed"

    def test_protocol_version_is_exact(self):
        with self.assertRaisesRegex(ValueError, "protocol_version"):
            request_context(protocol_version="1.0")
        with self.assertRaisesRegex(ValueError, "protocol_version"):
            AdapterDescriptor(
                adapter_id="adapter.example",
                adapter_version="1.0",
                protocol_version="1.0",
                supported_operations=("retrieve",),
                readable=True,
                writable=False,
            )

    def test_empty_required_ids_are_rejected(self):
        for build in (
            lambda: request_context(request_id=" "),
            lambda: submission(submission_id=""),
            lambda: correction(correction_id=""),
            lambda: ContextItem(
                item_id="", source_adapter_id="adapter.example",
                provenance={"source": "test"}, content="x",
            ),
        ):
            with self.subTest(build=build):
                with self.assertRaises(ValueError):
                    build()

    def test_all_transport_timestamps_require_timezone(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            request_context(requested_at="2026-09-29T08:00:00")
        with self.assertRaisesRegex(ValueError, "timezone"):
            submission(submitted_at="2026-09-29T08:00:00")
        with self.assertRaisesRegex(ValueError, "timezone"):
            correction(submitted_at="2026-09-29T08:00:00")
        with self.assertRaisesRegex(ValueError, "timezone"):
            ContextBundle(
                bundle_id="bundle-1",
                protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
                request_id="req-1",
                generated_at="2026-09-29T08:00:00",
                contributor_adapter_ids=(),
                items=(),
            )

    def test_mutation_and_correction_provenance_is_required(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            submission(provenance={})
        with self.assertRaisesRegex(ValueError, "provenance"):
            correction(provenance={})

    def test_semantic_fingerprint_is_deterministic_and_transport_metadata_is_excluded(self):
        first = submission()
        same_semantics = submission(
            submission_id="submission-2",
            idempotency_key="other-key",
            submitted_at="2026-09-30T09:00:00+01:00",
            request_context=request_context(
                request_id="req-2",
                turn_id="turn-2",
                requested_at="2026-09-30T08:00:00Z",
                metadata={"trace": "request-b"},
            ),
            candidate_states=(candidate_state(metadata={"trace": "candidate-b"}),),
            candidate_deltas=(candidate_delta(metadata={"trace": "candidate-b"}),),
            metadata={"trace": "envelope-b"},
        )
        self.assertEqual(semantic_fingerprint(first), first.semantic_fingerprint())
        self.assertEqual(first.semantic_fingerprint(), same_semantics.semantic_fingerprint())

    def test_semantic_payload_changes_fingerprint(self):
        first = submission()
        changed = submission(evidence=(evidence(content="different observation"),))
        self.assertNotEqual(first.semantic_fingerprint(), changed.semantic_fingerprint())
        corrected = correction(intent="invalidate after review")
        self.assertNotEqual(correction().semantic_fingerprint(), corrected.semantic_fingerprint())

    def test_semantic_tuple_order_participates_in_fingerprint(self):
        first_evidence = evidence(evidence_id="ev-1", content="first")
        second_evidence = evidence(evidence_id="ev-2", content="second")
        ordered = submission(evidence=(first_evidence, second_evidence))
        reversed_order = submission(evidence=(second_evidence, first_evidence))
        self.assertNotEqual(
            ordered.semantic_fingerprint(), reversed_order.semantic_fingerprint()
        )

    def test_kernel_payloads_are_deep_snapshots_and_fingerprint_cannot_drift(self):
        evidence_provenance = {"adapter": "example", "nested": {"revision": 1}}
        state_value = {"preference": {"colors": ["blue"]}}
        delta_derivation = {"adapter": "example", "nested": {"revision": 1}}
        source_evidence = evidence(provenance=evidence_provenance)
        proposed_state = state(content=None, structured_value=state_value)
        proposed_delta = delta(derived_by=delta_derivation)
        envelope = submission(
            evidence=(source_evidence,),
            candidate_states=(candidate_state(state=proposed_state),),
            candidate_deltas=(candidate_delta(delta=proposed_delta),),
        )
        fingerprint = envelope.semantic_fingerprint()

        evidence_provenance["nested"]["revision"] = 2
        state_value["preference"]["colors"].append("red")
        delta_derivation["nested"]["revision"] = 2

        self.assertEqual(1, envelope.evidence[0].provenance["nested"]["revision"])
        self.assertEqual(
            ("blue",),
            envelope.candidate_states[0].state.structured_value["preference"]["colors"],
        )
        self.assertEqual(
            1, envelope.candidate_deltas[0].delta.derived_by["nested"]["revision"]
        )
        self.assertEqual(fingerprint, envelope.semantic_fingerprint())
        with self.assertRaises(TypeError):
            envelope.evidence[0].provenance["nested"]["revision"] = 3
        with self.assertRaises(TypeError):
            envelope.candidate_states[0].state.structured_value["new"] = "value"
        with self.assertRaises(TypeError):
            envelope.candidate_deltas[0].delta.derived_by["nested"]["revision"] = 3

    def test_invalid_nested_kernel_payloads_are_rejected(self):
        invalid_builders = {
            "empty evidence id": lambda: submission(evidence=(evidence(evidence_id=""),)),
            "naive evidence timestamp": lambda: submission(
                evidence=(evidence(observed_at="2026-09-29T08:00:00"),)
            ),
            "missing evidence content": lambda: submission(
                evidence=(evidence(content=None, content_ref=None),)
            ),
            "empty state id": lambda: candidate_state(state=state(state_id="")),
            "naive state timestamp": lambda: candidate_state(
                state=state(created_at="2026-09-29T08:00:00")
            ),
            "missing state content": lambda: candidate_state(
                state=state(content=None, structured_value=None)
            ),
            "empty delta id": lambda: candidate_delta(delta=delta(delta_id="")),
            "naive delta timestamp": lambda: candidate_delta(
                delta=delta(occurred_at="2026-09-29T08:00:00")
            ),
            "delta self loop": lambda: candidate_delta(
                delta=delta(before_ref="state-1", after_ref="state-1")
            ),
        }
        for name, build in invalid_builders.items():
            with self.subTest(name=name):
                with self.assertRaises((TypeError, ValueError)):
                    build()

    def test_duplicate_underlying_candidate_semantic_ids_are_rejected(self):
        state_one = candidate_state(
            proposal_id="proposal-state-1", state=state(state_id="shared-state")
        )
        state_two = candidate_state(
            proposal_id="proposal-state-2", state=state(state_id="shared-state")
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            submission(
                evidence=(), candidate_states=(state_one, state_two), candidate_deltas=()
            )

        delta_one = candidate_delta(
            proposal_id="proposal-delta-1", delta=delta(delta_id="shared-delta")
        )
        delta_two = candidate_delta(
            proposal_id="proposal-delta-2", delta=delta(delta_id="shared-delta")
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            submission(
                evidence=(), candidate_states=(), candidate_deltas=(delta_one, delta_two)
            )

    def test_duplicate_or_ambiguous_refs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            candidate_state(supporting_refs=("ev-1", "ev-1"))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            correction(target_refs=("state-1", "state-1"))
        with self.assertRaisesRegex(ValueError, "ambiguously"):
            correction(
                correction_evidence=(evidence(evidence_id="ev-shared"),),
                evidence_refs=("ev-shared",),
            )

    def test_context_bundle_preserves_boundary_information_and_order(self):
        first = ContextItem(
            item_id="item-1",
            source_adapter_id="adapter.example",
            source_refs=("external:1",),
            kernel_refs=("state-1",),
            provenance={"source": "adapter.example"},
            confidence=0.62,
            epistemic_status="uncertain",
            visibility="private",
            permission_boundary={"capability_id": "capability.memory"},
            retrieval_reason="explicit query match",
            selection_metadata={"policy": "fixture"},
            content="context text",
        )
        second = ContextItem(
            item_id="item-2",
            source_adapter_id="continuity",
            source_refs=("continuity:2",),
            provenance={"source": "continuity"},
            content_ref="continuity://item/2",
        )
        bundle = ContextBundle(
            bundle_id="bundle-1",
            protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
            request_id="req-1",
            turn_id="turn-1",
            generated_at=UTC_1,
            contributor_adapter_ids=("adapter.example", "continuity"),
            items=(first, second),
            metadata={"trace": "bundle"},
        )
        self.assertEqual(("item-1", "item-2"), tuple(item.item_id for item in bundle.items))
        self.assertEqual(("external:1",), bundle.items[0].source_refs)
        self.assertEqual(("state-1",), bundle.items[0].kernel_refs)
        self.assertEqual("adapter.example", bundle.items[0].provenance["source"])
        self.assertEqual(0.62, bundle.items[0].confidence)
        self.assertEqual("uncertain", bundle.items[0].epistemic_status)
        self.assertEqual("private", bundle.items[0].visibility)
        self.assertEqual(
            "capability.memory", bundle.items[0].permission_boundary["capability_id"]
        )
        self.assertEqual("explicit query match", bundle.items[0].retrieval_reason)

    def test_context_bundle_request_and_turn_ids_are_optional(self):
        bundle = ContextBundle(
            bundle_id="bundle-without-request",
            protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
            request_id=None,
            turn_id=None,
            generated_at=UTC_1,
            contributor_adapter_ids=(),
            items=(),
        )
        self.assertIsNone(bundle.request_id)
        self.assertIsNone(bundle.turn_id)

    def test_candidates_cannot_masquerade_as_accepted_kernel_objects(self):
        state_candidate = candidate_state(review_status="accepted")
        delta_candidate = candidate_delta(review_status="accepted")
        self.assertNotIsInstance(state_candidate, State)
        self.assertNotIsInstance(delta_candidate, Delta)
        self.assertIs(type(state_candidate.state), State)
        self.assertIs(type(delta_candidate.delta), Delta)
        self.assertFalse(hasattr(state_candidate, "commit"))
        self.assertFalse(hasattr(delta_candidate, "promote"))

    def test_accepted_review_is_bound_to_submission_identity_and_fingerprint(self):
        envelope = submission()
        review = AcceptedReview(
            protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
            review_id="review-1",
            policy_id="policy-1",
            submission_id=envelope.submission_id,
            semantic_fingerprint=envelope.semantic_fingerprint(),
            reviewed_at=UTC_2,
            provenance={"reviewer": "policy-1"},
        )
        self.assertIs(review, validate_accepted_review(envelope, review))

        mismatched_id = dataclasses.replace(review, submission_id="submission-other")
        with self.assertRaisesRegex(ValueError, "submission_id"):
            validate_accepted_review(envelope, mismatched_id)
        mismatched_fingerprint = dataclasses.replace(
            review, semantic_fingerprint="0" * 64
        )
        with self.assertRaisesRegex(ValueError, "semantic_fingerprint"):
            validate_accepted_review(envelope, mismatched_fingerprint)
        mismatched_protocol = dataclasses.replace(review)
        # Exercise boundary hardening against an object reconstructed outside the
        # normal constructor (for example by an untrusted transport decoder).
        object.__setattr__(mismatched_protocol, "protocol_version", "1.0")
        with self.assertRaisesRegex(ValueError, "protocol_version"):
            validate_accepted_review(envelope, mismatched_protocol)

    def test_accepted_review_rejects_nonaccepted_status_and_invalid_identity(self):
        values = {
            "protocol_version": MEMORY_INTEROP_PROTOCOL_VERSION,
            "review_id": "review-1",
            "policy_id": "policy-1",
            "submission_id": "submission-1",
            "semantic_fingerprint": submission().semantic_fingerprint(),
            "reviewed_at": UTC_2,
            "provenance": {"reviewer": "policy-1"},
        }
        with self.assertRaisesRegex(ValueError, "status"):
            AcceptedReview(**values, status="rejected")
        with self.assertRaisesRegex(ValueError, "review_id"):
            AcceptedReview(**{**values, "review_id": ""})
        with self.assertRaisesRegex(ValueError, "semantic_fingerprint"):
            AcceptedReview(**{**values, "semantic_fingerprint": "not-a-digest"})

    def test_submission_result_requires_bound_submission_identity_and_fingerprint(self):
        fingerprint = submission().semantic_fingerprint()
        result = SubmissionResult(
            protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
            status="accepted",
            adapter_id="adapter.example",
            submission_id="submission-1",
            semantic_fingerprint=fingerprint,
        )
        self.assertEqual("submission-1", result.submission_id)
        self.assertEqual(fingerprint, result.semantic_fingerprint)
        for changes in (
            {"submission_id": ""},
            {"adapter_id": ""},
            {"semantic_fingerprint": ""},
            {"semantic_fingerprint": "A" * 64},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    SubmissionResult(
                        protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
                        status="accepted",
                        adapter_id=changes.get("adapter_id", "adapter.example"),
                        submission_id=changes.get("submission_id", "submission-1"),
                        semantic_fingerprint=changes.get(
                            "semantic_fingerprint", fingerprint
                        ),
                    )

    def test_contract_construction_and_fingerprinting_perform_no_database_io(self):
        with mock.patch("sqlite3.connect", side_effect=AssertionError("database access")):
            envelope = submission()
            correction_envelope = correction()
            self.assertRegex(envelope.semantic_fingerprint(), r"^[0-9a-f]{64}$")
            self.assertRegex(correction_envelope.semantic_fingerprint(), r"^[0-9a-f]{64}$")

    def test_contract_module_contains_no_raw_sql_or_database_write_calls(self):
        module_source = (
            Path(__file__).resolve().parents[1] / "tools" / "memory_interop.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "import sqlite3",
            "sqlite3.connect",
            ".execute(",
            "INSERT INTO",
            "UPDATE memory_",
            "DELETE FROM",
            "CREATE TABLE",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, module_source)

    def test_fingerprint_rejects_non_json_values(self):
        # Construction rejects lossy values before they can enter a fingerprint.
        with self.assertRaisesRegex(ValueError, "JSON"):
            submission(metadata={"bad": object()})


class MemoryInteropImportIsolationTests(unittest.TestCase):
    def test_import_does_not_load_production_runtime_or_ombre_modules(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = r'''
import json
import sys
import tools.memory_interop

blocked_roots = ("app", "gateway", "chat", "wake", "providers", "cc_resident")
loaded = sorted(
    name for name in sys.modules
    if name == "tools.ombre_adapter"
    or name.startswith("tools.ombre_adapter.")
    or name.split(".", 1)[0] in blocked_roots
)
print(json.dumps(loaded))
'''
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=repo_root,
            env=None,
            text=True,
            capture_output=True,
            check=False,
        )
        # Isolated mode omits cwd; add only the fixed repository root explicitly.
        if completed.returncode != 0 and "No module named 'tools'" in completed.stderr:
            escaped_root = json.dumps(str(repo_root))
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    f"import sys; sys.path.insert(0, {escaped_root});" + script,
                ],
                cwd=repo_root,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()

