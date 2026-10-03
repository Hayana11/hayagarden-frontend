"""Durable ContextReceipt storage contract for the runtime database.

This module stores metadata-only proof of a successfully installed ContextPlan.
It owns no planning, source selection, runtime validation, or provider payload.
Connections are supplied by the caller; schema creation is additive and
idempotent.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Optional

CONTEXT_RECEIPT_SCHEMA_VERSION = 'context_receipt_v1'
INSTALL_PROOF_VERSION = 'context_receipt_install_proof_v1'
INSTALL_PROOF_OK = 'SAFE_FORK_REUSE'
INSTALL_PROOF_REQUIRED = 'context_receipt_install_proof_required'
INSTALL_PROOF_MISSING = 'context_receipt_install_proof_missing'
INSTALL_PROOF_INVALID = 'context_receipt_install_proof_invalid'
INSTALL_PROOF_BOUNDARY_MISMATCH = 'context_receipt_install_proof_boundary_mismatch'
INSTALL_PROOF_PARTIAL_REPRESENTATION = 'context_receipt_install_proof_partial_representation'
INSTALL_PROOF_MISMATCH = 'context_receipt_install_proof_mismatch'
INSTALL_PROOF_RUNTIME_IDENTITY_MISMATCH = 'context_receipt_install_proof_runtime_identity_mismatch'
RECEIPT_RESULT_INSTALLED = 'installed'
RECEIPT_RESULT_SUPERSEDED = 'superseded'


class ContextReceiptConflict(RuntimeError):
    """The durable receipt cannot be overwritten with a different proof."""


@dataclass(frozen=True)
class ContextReceiptMember:
    installed_order: int
    representation_id: str
    representation_kind: str
    source_ref: str
    source_revision: str
    source_kind: str
    content_hash: str
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    branch_id: str = ''

    def __post_init__(self) -> None:
        if int(self.installed_order) < 0:
            raise ValueError('installed_order must be non-negative')
        for name in (
            'representation_id',
            'representation_kind',
            'source_ref',
            'source_revision',
            'source_kind',
            'content_hash',
        ):
            if not str(getattr(self, name)):
                raise ValueError('%s must be non-empty' % name)
        if self.span_start is not None and int(self.span_start) < 0:
            raise ValueError('span_start must be non-negative')
        if self.span_end is not None and int(self.span_end) < 0:
            raise ValueError('span_end must be non-negative')
        if (
            self.span_start is not None
            and self.span_end is not None
            and int(self.span_end) < int(self.span_start)
        ):
            raise ValueError('span_end must not precede span_start')

    def identity_payload(self) -> dict[str, Any]:
        return {
            'installed_order': int(self.installed_order),
            'representation_id': str(self.representation_id),
            'representation_kind': str(self.representation_kind),
            'source_ref': str(self.source_ref),
            'source_revision': str(self.source_revision),
            'source_kind': str(self.source_kind),
            'content_hash': str(self.content_hash),
            'span_start': (
                int(self.span_start) if self.span_start is not None else None
            ),
            'span_end': int(self.span_end) if self.span_end is not None else None,
            'branch_id': str(self.branch_id),
        }


@dataclass(frozen=True)
class ContextReceipt:
    context_id: int
    context_epoch: int
    resident_generation: int
    resident_key: str
    provider: str
    model_identity: str
    session_id: str
    process_generation: int
    plan_id: str
    plan_hash: str
    budget_policy_version: str
    measurement_semantics: str
    membership_hash: str
    installed_source_watermark: int
    receipt_revision: int = 0
    result: str = RECEIPT_RESULT_INSTALLED
    committed_at: str = ''
    updated_at: str = ''
    superseded_by_generation: Optional[int] = None
    # Canonical metadata-only ContextPlan install proof. Empty is retained for
    # legacy receipts; the shared rewrite verifier fails closed when absent.
    install_proof: str = ''

    @classmethod
    def build(
        cls,
        *,
        context_id: int,
        context_epoch: int,
        resident_generation: int,
        resident_key: str,
        provider: str,
        model_identity: str,
        session_id: str,
        process_generation: int,
        plan_id: str,
        plan_hash: str,
        budget_policy_version: str,
        measurement_semantics: str,
        installed_source_watermark: int,
        members: Iterable[ContextReceiptMember],
        context_plan: Any = None,
        install_proof: str = '',
        committed_at: str = '',
        updated_at: str = '',
    ) -> 'ContextReceipt':
        normalized = _normalize_members(members)
        if context_plan is not None:
            computed_proof = canonical_install_proof(context_plan)
            if install_proof and str(install_proof) != computed_proof:
                raise ValueError('install_proof does not match context_plan')
            install_proof = computed_proof
        install_proof = _normalize_install_proof(install_proof)
        now = _utc_now() if not committed_at else str(committed_at)
        return cls(
            context_id=int(context_id),
            context_epoch=int(context_epoch),
            resident_generation=int(resident_generation),
            resident_key=str(resident_key),
            provider=str(provider),
            model_identity=str(model_identity),
            session_id=str(session_id),
            process_generation=int(process_generation),
            plan_id=str(plan_id),
            plan_hash=str(plan_hash),
            budget_policy_version=str(budget_policy_version),
            measurement_semantics=str(measurement_semantics),
            membership_hash=installed_membership_hash(normalized),
            installed_source_watermark=int(installed_source_watermark),
            receipt_revision=0,
            result=RECEIPT_RESULT_INSTALLED,
            committed_at=now,
            updated_at=str(updated_at or now),
            install_proof=install_proof,
        )


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _context_plan_member_identity(member: Any) -> dict[str, Any]:
    """Use the canonical ContextPlan member identity for receipt proofs."""
    from continuity.context_plan import _member_identity
    return dict(_member_identity(member))


def _context_plan_representation_payload(representation: Any) -> dict[str, Any]:
    return {
        'representation_id': str(getattr(representation, 'representation_id', '') or ''),
        'kind': str(getattr(representation, 'kind', '') or ''),
        'source_members': [
            _context_plan_member_identity(member)
            for member in tuple(getattr(representation, 'source_members', ()) or ())
        ],
        'source_refs': [
            str(ref) for ref in tuple(getattr(representation, 'source_refs', ()) or ())
        ],
        'source_revisions': [
            str(revision)
            for revision in tuple(getattr(representation, 'source_revisions', ()) or ())
        ],
        'source_hash': str(getattr(representation, 'source_hash', '') or ''),
        'estimated_tokens': int(getattr(representation, 'estimated_tokens', 0) or 0),
        'chunk_id': getattr(representation, 'chunk_id', None),
        'candidate_id': getattr(representation, 'candidate_id', None),
        'snapshot_id': getattr(representation, 'snapshot_id', None),
        'provenance': [
            list(item) if isinstance(item, (tuple, list)) else item
            for item in tuple(getattr(representation, 'provenance', ()) or ())
        ],
    }


def _context_plan_section_payload(section: Any) -> dict[str, Any]:
    return {
        'kind': str(getattr(section, 'kind', '') or ''),
        'source_ref': str(getattr(section, 'source_ref', '') or ''),
        'content_hash': str(getattr(section, 'content_hash', '') or ''),
        'estimated_tokens': int(getattr(section, 'estimated_tokens', 0) or 0),
        'representation_id': getattr(section, 'representation_id', None),
    }


def canonical_install_proof(context_plan: Any) -> str:
    """Serialize provider-visible ContextPlan identity, excluding current_request.

    This is a projection only: ContextPlan remains the sole planner/source
    selector. Bodies and provider payloads never enter the durable proof.
    """
    if context_plan is None:
        raise ValueError('context_plan is required')
    representations = tuple(getattr(context_plan, 'representations', ()) or ())
    representation_payloads = tuple(
        _context_plan_representation_payload(representation)
        for representation in representations
    )
    selected_raw_source_seqs = sorted({
        int(member.get('seq'))
        for representation in representation_payloads
        if str(representation.get('kind') or '') == 'raw'
        for member in tuple(representation.get('source_members') or ())
        if member.get('seq') is not None
    })
    sections = tuple(
        section for section in tuple(getattr(context_plan, 'ordered_sections', ()) or ())
        if str(getattr(section, 'kind', '') or '') != 'current_request'
    )
    policy = getattr(context_plan, 'budget_policy', None)
    payload = {
        'version': INSTALL_PROOF_VERSION,
        'measurement_semantics': str(
            getattr(context_plan, 'measurement_semantics', '') or ''
        ),
        'budget_policy_version': str(
            getattr(context_plan, 'budget_policy_version', '') or ''
        ),
        'budget_policy': ({
            'token_budget': int(policy.token_budget),
            'reserve_budget': int(policy.reserve_budget),
            'recent_raw_target': int(policy.recent_raw_target),
        } if policy is not None else None),
        'budget_status': str(getattr(context_plan, 'budget_status', '') or ''),
        'recent_raw_source_seqs': selected_raw_source_seqs,
        'representations': list(representation_payloads),
        'ordered_sections': [
            _context_plan_section_payload(section) for section in sections
        ],
    }
    return _canonical(payload)


def _normalize_install_proof(value: object) -> str:
    if value is None or value == '':
        return ''
    if not isinstance(value, str):
        raise ValueError('install_proof must be canonical JSON text')
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('install_proof is not valid JSON') from exc
    if not isinstance(parsed, dict) or parsed.get('version') != INSTALL_PROOF_VERSION:
        raise ValueError('install_proof version is unsupported')
    canonical = _canonical(parsed)
    if canonical != value:
        raise ValueError('install_proof is not canonical JSON')
    return canonical


def _source_ref_turn_ids(source_ref: object) -> Optional[tuple[int, int]]:
    parts = str(source_ref or '').split(':')
    if len(parts) != 3 or parts[0] != 'turn':
        return None
    try:
        user_id, assistant_id = int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None
    if user_id <= 0 or assistant_id <= 0:
        return None
    return user_id, assistant_id


def _member_is_prefix(
    member: Mapping[str, Any],
    *,
    fork_boundary_message_id: int,
    rewrite_user_message_id: int,
) -> bool:
    ids = _source_ref_turn_ids(member.get('source_ref'))
    return bool(
        ids is not None
        and int(ids[1]) <= int(fork_boundary_message_id)
        and int(ids[0]) < int(rewrite_user_message_id)
    )


def _representation_prefix_flags(
    representation: Mapping[str, Any],
    *,
    fork_boundary_message_id: int,
    rewrite_user_message_id: int,
) -> tuple[bool, ...]:
    members = tuple(representation.get('source_members') or ())
    if not members:
        raise ValueError('representation source_members are missing')
    if any(not isinstance(member, Mapping) for member in members):
        raise ValueError('representation source_members are invalid')
    return tuple(
        _member_is_prefix(
            member,
            fork_boundary_message_id=fork_boundary_message_id,
            rewrite_user_message_id=rewrite_user_message_id,
        )
        for member in members
    )


def _prefix_install_proof(
    proof: Mapping[str, Any],
    *,
    fork_boundary_message_id: int,
    rewrite_user_message_id: int,
    reject_post_boundary: bool,
) -> tuple[Optional[dict[str, Any]], str]:
    """Project only whole representations; never retain a partial one."""
    representations = tuple(proof.get('representations') or ())
    kept: list[dict[str, Any]] = []
    for representation in representations:
        if not isinstance(representation, Mapping):
            return None, INSTALL_PROOF_INVALID
        try:
            flags = _representation_prefix_flags(
                representation,
                fork_boundary_message_id=fork_boundary_message_id,
                rewrite_user_message_id=rewrite_user_message_id,
            )
        except ValueError:
            return None, INSTALL_PROOF_INVALID
        if all(flags):
            kept.append(dict(representation))
        elif any(flags):
            return None, INSTALL_PROOF_PARTIAL_REPRESENTATION
        elif reject_post_boundary:
            return None, INSTALL_PROOF_BOUNDARY_MISMATCH

    kept_ids = {
        str(item.get('representation_id') or '') for item in kept
    }
    sections: list[dict[str, Any]] = []
    for section in tuple(proof.get('ordered_sections') or ()):
        if not isinstance(section, Mapping):
            return None, INSTALL_PROOF_INVALID
        if str(section.get('kind') or '') == 'current_request':
            return None, INSTALL_PROOF_INVALID
        representation_id = section.get('representation_id')
        if representation_id is not None and str(representation_id) not in kept_ids:
            continue
        sections.append(dict(section))

    projected = dict(proof)
    projected['representations'] = kept
    projected['ordered_sections'] = sections
    projected['recent_raw_source_seqs'] = sorted({
        int(member['seq'])
        for representation in kept
        if str(representation.get('kind') or '') == 'raw'
        for member in tuple(representation.get('source_members') or ())
        if isinstance(member, Mapping) and member.get('seq') is not None
    })
    return projected, ''


def verify_install_proof(
    receipt: Any,
    context_plan: Any,
    *,
    fork_boundary_message_id: int,
    rewrite_user_message_id: int,
    runtime_identity: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Shared fail-closed proof verifier for native fork reuse."""
    proof_meta: dict[str, Any] = {}
    if context_plan is None:
        return False, INSTALL_PROOF_REQUIRED, proof_meta
    if not bool(getattr(context_plan, 'valid', False)):
        return False, INSTALL_PROOF_INVALID, proof_meta
    raw_receipt_proof = (
        str(getattr(receipt, 'install_proof', '') or '') if receipt else ''
    )
    if not raw_receipt_proof:
        return False, INSTALL_PROOF_MISSING, proof_meta
    try:
        receipt_proof = json.loads(_normalize_install_proof(raw_receipt_proof))
        expected_raw = canonical_install_proof(context_plan)
        expected_proof = json.loads(expected_raw)
        expected_prefix, expected_reason = _prefix_install_proof(
            expected_proof,
            fork_boundary_message_id=int(fork_boundary_message_id),
            rewrite_user_message_id=int(rewrite_user_message_id),
            reject_post_boundary=True,
        )
        if expected_prefix is None:
            return False, expected_reason or INSTALL_PROOF_BOUNDARY_MISMATCH, proof_meta
        installed_prefix, installed_reason = _prefix_install_proof(
            receipt_proof,
            fork_boundary_message_id=int(fork_boundary_message_id),
            rewrite_user_message_id=int(rewrite_user_message_id),
            reject_post_boundary=False,
        )
        if installed_prefix is None:
            return False, installed_reason or INSTALL_PROOF_INVALID, proof_meta
        if _canonical(installed_prefix) != _canonical(expected_prefix):
            return False, INSTALL_PROOF_MISMATCH, proof_meta
        runtime = dict(runtime_identity or {})
        if runtime:
            expected_static = str(runtime.get('static_system_sha256') or '')
            invariant_sections = tuple(
                section for section in (expected_prefix.get('ordered_sections') or ())
                if str(section.get('kind') or '') == 'invariant_system'
            )
            if (
                not expected_static
                or len(invariant_sections) != 1
                or str(invariant_sections[0].get('content_hash') or '')
                    != expected_static
                or str(getattr(receipt, 'provider', '') or '')
                    != str(runtime.get('provider') or '')
                or str(getattr(receipt, 'model_identity', '') or '')
                    != str(runtime.get('model') or '')
                or str(getattr(receipt, 'session_id', '') or '')
                    != str(runtime.get('session_id') or '')
                or int(getattr(receipt, 'context_id', -1))
                    != int(runtime.get('context_id'))
                or int(getattr(receipt, 'context_epoch', -1))
                    != int(runtime.get('context_epoch'))
                or int(getattr(receipt, 'resident_generation', -1))
                    != int(runtime.get('resident_generation'))
            ):
                return False, INSTALL_PROOF_RUNTIME_IDENTITY_MISMATCH, proof_meta
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False, INSTALL_PROOF_INVALID, proof_meta

    proof_meta.update({
        'status': INSTALL_PROOF_OK,
        'install_proof_version': INSTALL_PROOF_VERSION,
        'install_proof_hash': hashlib.sha256(
            _canonical(expected_prefix).encode('utf-8')
        ).hexdigest(),
        'receipt_install_proof_hash': hashlib.sha256(
            raw_receipt_proof.encode('utf-8')
        ).hexdigest(),
        'representation_count': len(expected_prefix.get('representations') or ()),
        'fixed_section_count': sum(
            1 for section in (expected_prefix.get('ordered_sections') or ())
            if str(section.get('kind') or '') in {
                'invariant_system', 'accepted_state', 'accepted_open_loops',
            }
        ),
    })
    return True, '', proof_meta


def installed_membership_hash(
    members: Iterable[ContextReceiptMember],
) -> str:
    """Hash ordered, metadata-only installed membership identity."""
    normalized = _normalize_members(members)
    return hashlib.sha256(
        _canonical([member.identity_payload() for member in normalized]).encode('utf-8')
    ).hexdigest()


def _normalize_member(value: ContextReceiptMember | Mapping[str, Any]) -> ContextReceiptMember:
    if isinstance(value, ContextReceiptMember):
        return value
    if isinstance(value, Mapping):
        return ContextReceiptMember(
            installed_order=int(value['installed_order']),
            representation_id=str(value['representation_id']),
            representation_kind=str(value['representation_kind']),
            source_ref=str(value['source_ref']),
            source_revision=str(value['source_revision']),
            source_kind=str(value['source_kind']),
            content_hash=str(value['content_hash']),
            span_start=(
                int(value['span_start'])
                if value.get('span_start') is not None else None
            ),
            span_end=(
                int(value['span_end']) if value.get('span_end') is not None else None
            ),
            branch_id=str(value.get('branch_id', '')),
        )
    raise TypeError('members must contain ContextReceiptMember values')


def _normalize_members(
    members: Iterable[ContextReceiptMember | Mapping[str, Any]],
) -> tuple[ContextReceiptMember, ...]:
    normalized = tuple(sorted(
        (_normalize_member(member) for member in members),
        key=lambda member: int(member.installed_order),
    ))
    expected = tuple(range(len(normalized)))
    actual = tuple(int(member.installed_order) for member in normalized)
    if actual != expected:
        raise ValueError('installed_order must be a contiguous sequence from zero')
    return normalized


def _validate_receipt(receipt: ContextReceipt) -> None:
    if not isinstance(receipt, ContextReceipt):
        raise TypeError('receipt must be ContextReceipt')
    if receipt.context_id is None or receipt.context_epoch is None:
        raise ValueError('context identity is required')
    if int(receipt.resident_generation) < 0:
        raise ValueError('resident_generation must be non-negative')
    if int(receipt.process_generation) < 0:
        raise ValueError('process_generation must be non-negative')
    if int(receipt.installed_source_watermark) < 0:
        raise ValueError('installed_source_watermark must be non-negative')
    if int(receipt.receipt_revision) < 0:
        raise ValueError('receipt_revision must be non-negative')
    if receipt.result not in (
        RECEIPT_RESULT_INSTALLED,
        RECEIPT_RESULT_SUPERSEDED,
    ):
        raise ValueError('unsupported receipt result')
    for name in (
        'resident_key',
        'provider',
        'model_identity',
        'session_id',
        'plan_id',
        'plan_hash',
        'budget_policy_version',
        'measurement_semantics',
        'membership_hash',
        'committed_at',
        'updated_at',
    ):
        if not str(getattr(receipt, name)):
            raise ValueError('%s must be non-empty' % name)
    if receipt.result == RECEIPT_RESULT_INSTALLED and (
        receipt.superseded_by_generation is not None
    ):
        raise ValueError('installed receipt cannot have superseded target')


def _receipt_key(receipt: ContextReceipt) -> tuple[int, int, int]:
    return (
        int(receipt.context_id),
        int(receipt.context_epoch),
        int(receipt.resident_generation),
    )


def _receipt_proof(receipt: ContextReceipt) -> tuple[Any, ...]:
    return (
        _receipt_key(receipt),
        receipt.resident_key,
        receipt.provider,
        receipt.model_identity,
        receipt.session_id,
        int(receipt.process_generation),
        receipt.plan_id,
        receipt.plan_hash,
        receipt.budget_policy_version,
        receipt.measurement_semantics,
        receipt.membership_hash,
        int(receipt.installed_source_watermark),
        receipt.result,
        (
            int(receipt.superseded_by_generation)
            if receipt.superseded_by_generation is not None else None
        ),
        receipt.install_proof,
    )


def _member_insert_values(
    key: tuple[int, int, int],
    member: ContextReceiptMember,
) -> tuple[Any, ...]:
    return (
        *key,
        int(member.installed_order),
        member.representation_id,
        member.representation_kind,
        member.source_ref,
        member.source_revision,
        member.source_kind,
        member.content_hash,
        member.span_start,
        member.span_end,
        member.branch_id,
    )


def _receipt_insert_values(receipt: ContextReceipt) -> tuple[Any, ...]:
    return (
        *_receipt_key(receipt),
        receipt.resident_key,
        receipt.provider,
        receipt.model_identity,
        receipt.session_id,
        int(receipt.process_generation),
        receipt.plan_id,
        receipt.plan_hash,
        receipt.budget_policy_version,
        receipt.measurement_semantics,
        receipt.membership_hash,
        int(receipt.installed_source_watermark),
        int(receipt.receipt_revision),
        receipt.result,
        receipt.committed_at,
        receipt.updated_at,
        (
            int(receipt.superseded_by_generation)
            if receipt.superseded_by_generation is not None else None
        ),
        receipt.install_proof,
    )


def ensure_context_receipt_schema(conn: sqlite3.Connection) -> None:
    """Create only the two additive durable ContextReceipt tables."""
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS context_receipts (
            context_id INTEGER NOT NULL,
            context_epoch INTEGER NOT NULL,
            resident_generation INTEGER NOT NULL,
            resident_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_identity TEXT NOT NULL,
            session_id TEXT NOT NULL,
            process_generation INTEGER NOT NULL,
            plan_id TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            budget_policy_version TEXT NOT NULL,
            measurement_semantics TEXT NOT NULL,
            membership_hash TEXT NOT NULL,
            installed_source_watermark INTEGER NOT NULL,
            receipt_revision INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL CHECK (result IN ('installed', 'superseded')),
            committed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            superseded_by_generation INTEGER NULL,
            install_proof TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (context_id, context_epoch, resident_generation)
        );

        CREATE TABLE IF NOT EXISTS context_receipt_members (
            context_id INTEGER NOT NULL,
            context_epoch INTEGER NOT NULL,
            resident_generation INTEGER NOT NULL,
            installed_order INTEGER NOT NULL,
            representation_id TEXT NOT NULL,
            representation_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            span_start INTEGER NULL,
            span_end INTEGER NULL,
            branch_id TEXT NOT NULL,
            PRIMARY KEY (
                context_id,
                context_epoch,
                resident_generation,
                installed_order
            ),
            FOREIGN KEY (
                context_id, context_epoch, resident_generation
            ) REFERENCES context_receipts(
                context_id, context_epoch, resident_generation
            ) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_context_receipt_members_source
            ON context_receipt_members(source_ref, source_revision);

        CREATE INDEX IF NOT EXISTS idx_context_receipts_session
            ON context_receipts(session_id);
        '''
    )
    columns = {
        row[1] for row in conn.execute('PRAGMA table_info(context_receipts)')
    }
    if 'install_proof' not in columns:
        conn.execute(
            "ALTER TABLE context_receipts ADD COLUMN install_proof TEXT NOT NULL DEFAULT ''"
        )


@contextmanager
def _atomic(conn: sqlite3.Connection):
    owns_transaction = not conn.in_transaction
    savepoint = 'context_receipt_mutation'
    if owns_transaction:
        conn.execute('BEGIN IMMEDIATE')
    else:
        conn.execute('SAVEPOINT %s' % savepoint)
    try:
        yield
    except Exception:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute('ROLLBACK TO SAVEPOINT %s' % savepoint)
            conn.execute('RELEASE SAVEPOINT %s' % savepoint)
        raise
    else:
        if owns_transaction:
            conn.commit()
        else:
            conn.execute('RELEASE SAVEPOINT %s' % savepoint)


def _row_to_receipt(row: sqlite3.Row | tuple[Any, ...]) -> ContextReceipt:
    if isinstance(row, sqlite3.Row):
        data = {key: row[key] for key in row.keys()}
    else:
        columns = (
            'context_id', 'context_epoch', 'resident_generation',
            'resident_key', 'provider', 'model_identity', 'session_id',
            'process_generation', 'plan_id', 'plan_hash',
            'budget_policy_version', 'measurement_semantics', 'membership_hash',
            'installed_source_watermark', 'receipt_revision', 'result',
            'committed_at', 'updated_at', 'superseded_by_generation',
            'install_proof',
        )
        data = dict(zip(columns, row))
    return ContextReceipt(
        context_id=int(data['context_id']),
        context_epoch=int(data['context_epoch']),
        resident_generation=int(data['resident_generation']),
        resident_key=str(data['resident_key']),
        provider=str(data['provider']),
        model_identity=str(data['model_identity']),
        session_id=str(data['session_id']),
        process_generation=int(data['process_generation']),
        plan_id=str(data['plan_id']),
        plan_hash=str(data['plan_hash']),
        budget_policy_version=str(data['budget_policy_version']),
        measurement_semantics=str(data['measurement_semantics']),
        membership_hash=str(data['membership_hash']),
        installed_source_watermark=int(data['installed_source_watermark']),
        receipt_revision=int(data['receipt_revision']),
        result=str(data['result']),
        committed_at=str(data['committed_at']),
        updated_at=str(data['updated_at']),
        superseded_by_generation=(
            int(data['superseded_by_generation'])
            if data['superseded_by_generation'] is not None else None
        ),
        install_proof=_normalize_install_proof(data.get('install_proof', '') or ''),
    )


def _member_from_row(row: sqlite3.Row | tuple[Any, ...]) -> ContextReceiptMember:
    if isinstance(row, sqlite3.Row):
        data = {key: row[key] for key in row.keys()}
    else:
        columns = (
            'installed_order', 'representation_id', 'representation_kind',
            'source_ref', 'source_revision', 'source_kind', 'content_hash',
            'span_start', 'span_end', 'branch_id',
        )
        data = dict(zip(columns, row))
    return ContextReceiptMember(
        installed_order=int(data['installed_order']),
        representation_id=str(data['representation_id']),
        representation_kind=str(data['representation_kind']),
        source_ref=str(data['source_ref']),
        source_revision=str(data['source_revision']),
        source_kind=str(data['source_kind']),
        content_hash=str(data['content_hash']),
        span_start=(
            int(data['span_start']) if data['span_start'] is not None else None
        ),
        span_end=(
            int(data['span_end']) if data['span_end'] is not None else None
        ),
        branch_id=str(data['branch_id']),
    )


def get_receipt(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
) -> Optional[ContextReceipt]:
    row = conn.execute(
        '''SELECT context_id, context_epoch, resident_generation,
                  resident_key, provider, model_identity, session_id,
                  process_generation, plan_id, plan_hash,
                  budget_policy_version, measurement_semantics, membership_hash,
                  installed_source_watermark, receipt_revision, result,
                  committed_at, updated_at, superseded_by_generation,
                  install_proof
           FROM context_receipts
           WHERE context_id=? AND context_epoch=? AND resident_generation=?''',
        (int(context_id), int(context_epoch), int(resident_generation)),
    ).fetchone()
    return _row_to_receipt(row) if row is not None else None


def get_receipt_members(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
) -> tuple[ContextReceiptMember, ...]:
    rows = conn.execute(
        '''SELECT installed_order, representation_id, representation_kind,
                  source_ref, source_revision, source_kind, content_hash,
                  span_start, span_end, branch_id
           FROM context_receipt_members
           WHERE context_id=? AND context_epoch=? AND resident_generation=?
           ORDER BY installed_order ASC''',
        (int(context_id), int(context_epoch), int(resident_generation)),
    ).fetchall()
    return tuple(_member_from_row(row) for row in rows)


def create_receipt(
    conn: sqlite3.Connection,
    receipt: ContextReceipt,
    members: Iterable[ContextReceiptMember | Mapping[str, Any]],
) -> ContextReceipt:
    """Insert a successful receipt and all members atomically, or no-op exactly."""
    _validate_receipt(receipt)
    if receipt.result != RECEIPT_RESULT_INSTALLED or receipt.receipt_revision != 0:
        raise ValueError('create_receipt requires a new installed revision zero receipt')
    normalized = _normalize_members(members)
    if receipt.membership_hash != installed_membership_hash(normalized):
        raise ValueError('membership_hash does not match receipt members')
    key = _receipt_key(receipt)
    with _atomic(conn):
        existing_row = conn.execute(
            '''SELECT context_id, context_epoch, resident_generation,
                      resident_key, provider, model_identity, session_id,
                      process_generation, plan_id, plan_hash,
                      budget_policy_version, measurement_semantics, membership_hash,
                      installed_source_watermark, receipt_revision, result,
                      committed_at, updated_at, superseded_by_generation,
                      install_proof
               FROM context_receipts
               WHERE context_id=? AND context_epoch=? AND resident_generation=?''',
            key,
        ).fetchone()
        if existing_row is not None:
            existing = _row_to_receipt(existing_row)
            existing_members = get_receipt_members(
                conn,
                context_id=key[0],
                context_epoch=key[1],
                resident_generation=key[2],
            )
            if (
                _receipt_proof(existing) == _receipt_proof(receipt)
                and existing_members == normalized
            ):
                return existing
            raise ContextReceiptConflict(
                'receipt primary key already contains a different proof'
            )
        conn.execute(
            '''INSERT INTO context_receipts (
                   context_id, context_epoch, resident_generation,
                   resident_key, provider, model_identity, session_id,
                   process_generation, plan_id, plan_hash,
                   budget_policy_version, measurement_semantics, membership_hash,
                   installed_source_watermark, receipt_revision, result,
                   committed_at, updated_at, superseded_by_generation,
                   install_proof
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            _receipt_insert_values(receipt),
        )
        conn.executemany(
            '''INSERT INTO context_receipt_members (
                   context_id, context_epoch, resident_generation,
                   installed_order, representation_id, representation_kind,
                   source_ref, source_revision, source_kind, content_hash,
                   span_start, span_end, branch_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            [_member_insert_values(key, member) for member in normalized],
        )
    return receipt


def hot_advance_receipt(
    conn: sqlite3.Connection,
    *,
    expected_receipt_revision: int,
    receipt: ContextReceipt,
    members: Iterable[ContextReceiptMember | Mapping[str, Any]],
) -> ContextReceipt:
    """CAS-advance one generation, replacing its complete member set."""
    _validate_receipt(receipt)
    if receipt.result != RECEIPT_RESULT_INSTALLED:
        raise ValueError('hot advance requires an installed receipt')
    if receipt.superseded_by_generation is not None:
        raise ValueError('hot advance cannot target a superseded receipt')
    normalized = _normalize_members(members)
    if receipt.membership_hash != installed_membership_hash(normalized):
        raise ValueError('membership_hash does not match receipt members')
    key = _receipt_key(receipt)
    with _atomic(conn):
        current = get_receipt(
            conn,
            context_id=key[0],
            context_epoch=key[1],
            resident_generation=key[2],
        )
        if current is None:
            raise ContextReceiptConflict('cannot advance a missing receipt')
        if (
            current.result != RECEIPT_RESULT_INSTALLED
            or current.superseded_by_generation is not None
        ):
            raise ContextReceiptConflict(
                'receipt is superseded and cannot advance'
            )
        if current.receipt_revision != int(expected_receipt_revision):
            raise ContextReceiptConflict('receipt revision CAS mismatch')
        if receipt.installed_source_watermark < current.installed_source_watermark:
            raise ContextReceiptConflict('source watermark regressed')
        current_members = get_receipt_members(
            conn,
            context_id=key[0],
            context_epoch=key[1],
            resident_generation=key[2],
        )
        if receipt.installed_source_watermark == current.installed_source_watermark:
            if (
                _receipt_proof(current) == _receipt_proof(receipt)
                and current_members == normalized
            ):
                return current
            raise ContextReceiptConflict(
                'same watermark contains a different proof'
            )
        if (
            current.resident_key != receipt.resident_key
            or current.provider != receipt.provider
            or current.model_identity != receipt.model_identity
            or current.session_id != receipt.session_id
            or current.process_generation != receipt.process_generation
        ):
            raise ContextReceiptConflict(
                'runtime identity changed within one resident generation'
            )
        next_revision = current.receipt_revision + 1
        updated = replace(receipt, receipt_revision=next_revision)
        changed = conn.execute(
            '''UPDATE context_receipts
               SET resident_key=?, provider=?, model_identity=?, session_id=?,
                   process_generation=?, plan_id=?, plan_hash=?,
                   budget_policy_version=?, measurement_semantics=?,
                   membership_hash=?, installed_source_watermark=?,
                   receipt_revision=?, result=?, committed_at=?, updated_at=?,
                   superseded_by_generation=?, install_proof=?
               WHERE context_id=? AND context_epoch=? AND resident_generation=?
                 AND receipt_revision=?''',
            (
                updated.resident_key, updated.provider, updated.model_identity,
                updated.session_id, int(updated.process_generation),
                updated.plan_id, updated.plan_hash,
                updated.budget_policy_version, updated.measurement_semantics,
                updated.membership_hash, int(updated.installed_source_watermark),
                int(updated.receipt_revision), updated.result,
                updated.committed_at, updated.updated_at,
                updated.superseded_by_generation, updated.install_proof,
                *key, int(expected_receipt_revision),
            ),
        )
        if changed.rowcount != 1:
            raise ContextReceiptConflict('receipt revision changed during advance')
        conn.execute(
            '''DELETE FROM context_receipt_members
               WHERE context_id=? AND context_epoch=? AND resident_generation=?''',
            key,
        )
        conn.executemany(
            '''INSERT INTO context_receipt_members (
                   context_id, context_epoch, resident_generation,
                   installed_order, representation_id, representation_kind,
                   source_ref, source_revision, source_kind, content_hash,
                   span_start, span_end, branch_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            [_member_insert_values(key, member) for member in normalized],
        )
    return updated


def mark_superseded(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    expected_receipt_revision: int,
    target_generation: int,
) -> ContextReceipt:
    """Mark a source generation superseded after its target is proven successful."""
    target = int(target_generation)
    resident = int(resident_generation)
    key = (int(context_id), int(context_epoch), resident)
    with _atomic(conn):
        current = get_receipt(
            conn,
            context_id=key[0],
            context_epoch=key[1],
            resident_generation=key[2],
        )
        if current is None:
            raise ContextReceiptConflict('cannot supersede a missing receipt')
        if current.result == RECEIPT_RESULT_SUPERSEDED:
            if current.superseded_by_generation == target:
                return current
            raise ContextReceiptConflict('receipt already superseded by another generation')
        if target != resident + 1:
            raise ValueError(
                'supersede target must equal resident_generation + 1'
            )
        if current.receipt_revision != int(expected_receipt_revision):
            raise ContextReceiptConflict('receipt revision CAS mismatch')
        updated = replace(
            current,
            receipt_revision=current.receipt_revision + 1,
            result=RECEIPT_RESULT_SUPERSEDED,
            superseded_by_generation=target,
            updated_at=_utc_now(),
        )
        changed = conn.execute(
            '''UPDATE context_receipts
               SET receipt_revision=?, result=?, updated_at=?,
                   superseded_by_generation=?
               WHERE context_id=? AND context_epoch=? AND resident_generation=?
                 AND receipt_revision=?''',
            (
                int(updated.receipt_revision), updated.result, updated.updated_at,
                int(updated.superseded_by_generation),
                *key, int(expected_receipt_revision),
            ),
        )
        if changed.rowcount != 1:
            raise ContextReceiptConflict('receipt revision changed during supersede')
    return updated


__all__ = [
    'CONTEXT_RECEIPT_SCHEMA_VERSION',
    'INSTALL_PROOF_VERSION',
    'INSTALL_PROOF_OK',
    'INSTALL_PROOF_REQUIRED',
    'INSTALL_PROOF_MISSING',
    'INSTALL_PROOF_INVALID',
    'INSTALL_PROOF_BOUNDARY_MISMATCH',
    'INSTALL_PROOF_PARTIAL_REPRESENTATION',
    'INSTALL_PROOF_MISMATCH',
    'INSTALL_PROOF_RUNTIME_IDENTITY_MISMATCH',
    'RECEIPT_RESULT_INSTALLED',
    'RECEIPT_RESULT_SUPERSEDED',
    'ContextReceipt',
    'ContextReceiptConflict',
    'ContextReceiptMember',
    'canonical_install_proof',
    'create_receipt',
    'ensure_context_receipt_schema',
    'get_receipt',
    'get_receipt_members',
    'hot_advance_receipt',
    'installed_membership_hash',
    'mark_superseded',
    'verify_install_proof',
]
