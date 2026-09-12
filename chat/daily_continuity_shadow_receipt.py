"""Process-local installed-context shadow receipt for Daily R4-R4D1.

The receipt is an ephemeral proof artifact.  It deliberately contains only
immutable identity and measurement metadata; it never persists or selects
context and is never sent to a provider.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional


MEASUREMENT_SEMANTICS = 'heuristic_cjk1_ascii4_v1'


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class InstalledSourceMemberIdentity:
    """Minimal immutable identity projected from one SourceMember."""

    source_ref: str
    source_revision: str
    source_kind: str
    content_hash: str
    seq: int
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    evidence_refs: tuple[str, ...] = ()

    @classmethod
    def from_member(cls, member: Any) -> 'InstalledSourceMemberIdentity':
        source_ref = str(getattr(member, 'source_ref', '') or '')
        return cls(
            source_ref=source_ref,
            source_revision=str(getattr(member, 'source_revision', '') or ''),
            source_kind=str(getattr(member, 'source_kind', '') or ''),
            content_hash=str(getattr(member, 'content_hash', '') or ''),
            seq=int(getattr(member, 'seq', 0) or 0),
            span_start=(
                int(member.span_start)
                if getattr(member, 'span_start', None) is not None else None
            ),
            span_end=(
                int(member.span_end)
                if getattr(member, 'span_end', None) is not None else None
            ),
            evidence_refs=(source_ref,) if source_ref else (),
        )

    def as_metadata(self) -> dict[str, Any]:
        return {
            'source_ref': self.source_ref,
            'source_revision': self.source_revision,
            'source_kind': self.source_kind,
            'content_hash': self.content_hash,
            'seq': self.seq,
            'span_start': self.span_start,
            'span_end': self.span_end,
            'evidence_refs': self.evidence_refs,
        }


def membership_hash(members: Iterable[Any]) -> str:
    identities = [
        InstalledSourceMemberIdentity.from_member(member).as_metadata()
        for member in members
    ]
    return hashlib.sha256(_canonical(identities).encode('utf-8')).hexdigest()


def _freeze_fixed_fingerprints(value: Iterable[Any]) -> tuple[tuple[Any, ...], ...]:
    frozen: list[tuple[Any, ...]] = []
    for item in value or ():
        if isinstance(item, dict):
            frozen.append((
                str(item.get('kind') or ''),
                str(item.get('source_ref') or ''),
                str(item.get('content_hash') or ''),
                int(item.get('estimated_tokens') or 0),
            ))
            continue
        if isinstance(item, (tuple, list)) and len(item) >= 4:
            frozen.append((
                str(item[0] or ''),
                str(item[1] or ''),
                str(item[2] or ''),
                int(item[3] or 0),
            ))
            continue
        frozen.append((
            str(getattr(item, 'kind', '') or ''),
            str(getattr(item, 'source_ref', '') or ''),
            str(getattr(item, 'content_hash', '') or ''),
            int(getattr(item, 'estimated_tokens', 0) or 0),
        ))
    return tuple(frozen)


@dataclass(frozen=True)
class InstalledContextShadowReceipt:
    """Committed process-local proof of a resident's installed source."""

    context_id: int
    context_epoch: int
    resident_generation: int
    resident_key: str
    claude_session_id: str
    process_generation: int
    base_plan_id: str
    base_plan_hash: str
    installed_source_members: tuple[InstalledSourceMemberIdentity, ...]
    fixed_section_fingerprints: tuple[tuple[Any, ...], ...]
    production_content_hash: str
    production_content_token_estimate: int
    source_turn_kind: str
    receipt_state: str
    membership_hash: str
    measurement_semantics: str = MEASUREMENT_SEMANTICS

    @classmethod
    def build(
        cls,
        *,
        context_id: int,
        context_epoch: int,
        resident_generation: int,
        resident_key: str,
        claude_session_id: str,
        process_generation: int,
        base_plan_id: str,
        base_plan_hash: str,
        source_members: Iterable[Any],
        fixed_section_fingerprints: Iterable[Any] = (),
        production_content_hash: str = '',
        production_content_token_estimate: int = 0,
        source_turn_kind: str,
        receipt_state: str = 'committed',
        measurement_semantics: str = MEASUREMENT_SEMANTICS,
    ) -> 'InstalledContextShadowReceipt':
        members = tuple(
            InstalledSourceMemberIdentity.from_member(member)
            for member in source_members
        )
        return cls(
            context_id=int(context_id),
            context_epoch=int(context_epoch),
            resident_generation=int(resident_generation),
            resident_key=str(resident_key),
            claude_session_id=str(claude_session_id),
            process_generation=int(process_generation),
            base_plan_id=str(base_plan_id),
            base_plan_hash=str(base_plan_hash),
            installed_source_members=members,
            fixed_section_fingerprints=_freeze_fixed_fingerprints(
                fixed_section_fingerprints,
            ),
            production_content_hash=str(production_content_hash or ''),
            production_content_token_estimate=int(
                production_content_token_estimate or 0
            ),
            source_turn_kind=str(source_turn_kind),
            receipt_state=str(receipt_state),
            membership_hash=hashlib.sha256(_canonical([item.as_metadata() for item in members]).encode('utf-8')),
            measurement_semantics=str(measurement_semantics),
        )

    def matches_live(
        self,
        *,
        context_id: int,
        context_epoch: int,
        resident_generation: int,
        resident_key: str,
        claude_session_id: str,
        process_generation: int,
    ) -> bool:
        return (
            int(self.context_id) == int(context_id)
            and int(self.context_epoch) == int(context_epoch)
            and int(self.resident_generation) == int(resident_generation)
            and self.resident_key == str(resident_key)
            and self.claude_session_id == str(claude_session_id)
            and int(self.process_generation) == int(process_generation)
        )


_STORE: dict[tuple[int, int, int], InstalledContextShadowReceipt] = {}
_LOCK = threading.RLock()


def receipt_key(context_id: int, context_epoch: int, resident_generation: int) -> tuple[int, int, int]:
    return (int(context_id), int(context_epoch), int(resident_generation))


def get(
    context_id: int,
    context_epoch: int,
    resident_generation: int,
) -> Optional[InstalledContextShadowReceipt]:
    with _LOCK:
        return _STORE.get(receipt_key(context_id, context_epoch, resident_generation))


def commit(receipt: InstalledContextShadowReceipt) -> InstalledContextShadowReceipt:
    if not isinstance(receipt, InstalledContextShadowReceipt):
        raise TypeError('receipt must be InstalledContextShadowReceipt')
    with _LOCK:
        _STORE[receipt_key(
            receipt.context_id,
            receipt.context_epoch,
            receipt.resident_generation,
        )] = receipt
    return receipt

def advance(
    receipt: InstalledContextShadowReceipt,
    *,
    new_members: Iterable[Any],
    production_content_hash: Optional[str] = None,
    production_content_token_estimate: Optional[int] = None,
    source_turn_kind: Optional[str] = None,
) -> InstalledContextShadowReceipt:
    """Return an immutable replacement receipt with appended canonical members."""
    prior = tuple(receipt.installed_source_members)
    offset = (int(prior[-1].seq) + 1) if prior else 0
    appended = []
    for index, member in enumerate(new_members):
        identity = (
            member
            if isinstance(member, InstalledSourceMemberIdentity)
            else InstalledSourceMemberIdentity.from_member(member)
        )
        appended.append(replace(identity, seq=offset + index))
    combined = prior + tuple(appended)
    return replace(
        receipt,
        installed_source_members=combined,
        membership_hash=hashlib.sha256(
            _canonical([item.as_metadata() for item in combined]).encode('utf-8')
        ).hexdigest(),
        production_content_hash=(
            receipt.production_content_hash
            if production_content_hash is None
            else str(production_content_hash or '')
        ),
        production_content_token_estimate=(
            receipt.production_content_token_estimate
            if production_content_token_estimate is None
            else int(production_content_token_estimate or 0)
        ),
        source_turn_kind=(
            receipt.source_turn_kind
            if source_turn_kind is None
            else str(source_turn_kind)
        ),
    )


def drop(context_id: int, context_epoch: int, resident_generation: int) -> None:
    with _LOCK:
        _STORE.pop(receipt_key(context_id, context_epoch, resident_generation), None)


def clear_for_tests() -> None:
    with _LOCK:
        _STORE.clear()


__all__ = [
    'InstalledContextShadowReceipt',
    'InstalledSourceMemberIdentity',
    'MEASUREMENT_SEMANTICS',
    'clear_for_tests',
    'commit',
    'drop',
    'advance',
    'get',
    'membership_hash',
    'receipt_key',
]
