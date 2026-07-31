"""v0.2 Transcript Core data model.

Pure data types only — no Flask, no DB, no env, no filesystem I/O.
Events are never mutated in place by consumers of these types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class EventRole(str, Enum):
    """Logical role after structure inspection (not raw message.role alone).

    Ordinary JSONL ``user`` rows that are not tool_result-only are only
    *candidates*. Confirmed kitten/user identity requires an authoritative
    application message-event mapping at Transform time.
    """

    CANDIDATE_USER = 'candidate_user'
    TOOL_RESULT_USER = 'tool_result_user'
    ASSISTANT = 'assistant'
    SYSTEM = 'system'
    SUMMARY = 'summary'
    SIDECHAIN = 'sidechain'
    META = 'meta'
    UNKNOWN = 'unknown'


class EventType(str, Enum):
    USER = 'user'
    ASSISTANT = 'assistant'
    SYSTEM = 'system'
    SUMMARY = 'summary'
    UNKNOWN = 'unknown'


class ThinkingPolicy(str, Enum):
    """Spike-executable thinking strategies (explicit Transform input)."""

    KEEP = 'keep'
    DROP = 'drop'


class SidechainPolicy(str, Enum):
    """v0.2 supports EXCLUDE only (whole affected round).

    Sidechain KEEP is not supported in v0.2 — there is no executable KEEP entry.
    """

    EXCLUDE = 'exclude'


class SummaryPolicy(str, Enum):
    """v0.2 supports DROP only — summary events are never migrated.

    Summary KEEP is not supported in v0.2 — there is no executable KEEP entry.
    """

    DROP = 'drop'


class UnknownEventPolicy(str, Enum):
    DROP = 'drop'
    REJECT = 'reject'


@dataclass(frozen=True)
class ToolUseRef:
    tool_use_id: str
    event_uuid: str
    name: str = ''
    line_number: int = 0


@dataclass(frozen=True)
class ToolResultRef:
    tool_use_id: str
    event_uuid: str
    line_number: int = 0
    is_error: bool = False


@dataclass(frozen=True)
class TranscriptEvent:
    """One JSONL row, preserved as an immutable snapshot."""

    event_uuid: str
    parent_uuid: Optional[str]
    session_id: str
    event_role: EventRole
    event_type: EventType
    raw: Mapping[str, Any]
    line_number: int
    is_sidechain: bool = False
    byte_offset: Optional[int] = None

    def raw_copy(self) -> dict[str, Any]:
        """Deep-ish JSON-safe copy of the original object."""
        import copy

        return copy.deepcopy(dict(self.raw))


@dataclass(frozen=True)
class CandidateConversationRound:
    """One candidate user turn and its main-chain assistant/tool logic.

    Sidechain impact is recorded via full parent-graph attribution (order-
    independent). Under ``SidechainPolicy.EXCLUDE``, a non-empty
    ``sidechain_impact_uuids`` means Transform must drop the *entire* round.
    """

    candidate_user_event_uuid: str
    event_uuids: tuple[str, ...]
    tool_use_ids: tuple[str, ...] = ()
    has_assistant: bool = False
    sidechain_impact_uuids: tuple[str, ...] = ()

    @property
    def has_sidechain_impact(self) -> bool:
        return bool(self.sidechain_impact_uuids)

    @property
    def starts_with_candidate_user(self) -> bool:
        return bool(self.candidate_user_event_uuid) and (
            not self.event_uuids
            or self.event_uuids[0] == self.candidate_user_event_uuid
        )


@dataclass
class TranscriptGraph:
    """Indexed view of a source transcript. Does not mutate source events."""

    session_id: str
    events: list[TranscriptEvent] = field(default_factory=list)
    by_uuid: dict[str, TranscriptEvent] = field(default_factory=dict)
    children_by_parent: dict[Optional[str], list[str]] = field(default_factory=dict)
    tool_uses: dict[str, ToolUseRef] = field(default_factory=dict)
    tool_results: dict[str, ToolResultRef] = field(default_factory=dict)
    summary_uuids: list[str] = field(default_factory=list)
    sidechain_uuids: list[str] = field(default_factory=list)
    system_uuids: list[str] = field(default_factory=list)
    unknown_uuids: list[str] = field(default_factory=list)
    candidate_rounds: list[CandidateConversationRound] = field(default_factory=list)
    source_path: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def event(self, event_uuid: str) -> Optional[TranscriptEvent]:
        return self.by_uuid.get(event_uuid)
