"""Immutable-ish normalized data types for the hidden flow foundation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _string_tuple(value: Any, *, limit: Optional[int] = None) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        text = _clean(item)
        if text and text not in result:
            result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return tuple(result)


@dataclass(frozen=True)
class PoolEntry:
    entry_id: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.entry_id, "text": self.text}


@dataclass(frozen=True)
class PoolSpec:
    pool_id: str
    enabled: bool = True
    draw_mode: str = "turn"
    draw_count: int = 1
    entries: tuple[PoolEntry, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.pool_id,
            "enabled": self.enabled,
            "drawMode": self.draw_mode,
            "drawCount": self.draw_count,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class CueSpec:
    cue_id: str
    key: str
    enabled: bool = True
    pool_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.cue_id,
            "key": self.key,
            "enabled": self.enabled,
            "poolIds": list(self.pool_ids),
        }


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    enabled: bool = True
    min_turns: int = 1
    repeat_min_turns: int = 1
    next_stage: Optional[str] = None
    continue_target: Optional[str] = None
    terminal_without_continue: bool = False
    holdable: bool = False
    pool_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.stage_id,
            "enabled": self.enabled,
            "minTurns": self.min_turns,
            "repeatMinTurns": self.repeat_min_turns,
            "nextStage": self.next_stage,
            "continueTarget": self.continue_target,
            "terminalWithoutContinue": self.terminal_without_continue,
            "holdable": self.holdable,
            "poolIds": list(self.pool_ids),
        }


@dataclass(frozen=True)
class FlowConfig:
    flow_id: str
    enabled: bool = False
    initial_stage: Optional[str] = None
    stages: tuple[StageSpec, ...] = ()
    cues: tuple[CueSpec, ...] = ()
    pools: tuple[PoolSpec, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def stage(self, stage_id: str) -> Optional[StageSpec]:
        return next((item for item in self.stages if item.stage_id == stage_id), None)

    def cue_for_key(self, key: str) -> Optional[CueSpec]:
        folded = _clean(key).casefold()
        return next((item for item in self.cues if item.key.casefold() == folded), None)

    def pool(self, pool_id: str) -> Optional[PoolSpec]:
        return next((item for item in self.pools if item.pool_id == pool_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "flowId": self.flow_id,
            "enabled": self.enabled,
            "initialStage": self.initial_stage,
            "stages": [item.to_dict() for item in self.stages],
            "cues": [item.to_dict() for item in self.cues],
            "pools": [item.to_dict() for item in self.pools],
        }


@dataclass
class FlowState:
    active: bool = False
    flow_id: str = ""
    stage: str = ""
    cycle: int = 0
    stage_turn: int = 0
    context_keys: tuple[str, ...] = ()
    fixed_draws: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def inactive(cls) -> "FlowState":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "flowId": self.flow_id,
            "active": bool(self.active),
            "stage": self.stage,
            "cycle": int(self.cycle),
            "stageTurn": int(self.stage_turn),
            "contextKeys": list(self.context_keys),
            "fixedDraws": dict(self.fixed_draws),
            "startedAt": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FlowState":
        if not isinstance(data, Mapping) or data.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("unsupported flow state schema")
        return cls(
            active=_bool(data.get("active")),
            flow_id=_clean(data.get("flowId")),
            stage=_clean(data.get("stage")),
            cycle=max(0, int(data.get("cycle") or 0)),
            stage_turn=max(0, int(data.get("stageTurn") or 0)),
            context_keys=_string_tuple(data.get("contextKeys"), limit=4),
            fixed_draws=dict(data.get("fixedDraws") or {}),
            started_at=_clean(data.get("startedAt")),
        )


@dataclass(frozen=True)
class FlowControl:
    flow_id: str
    action: Optional[str] = None
    keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"flowId": self.flow_id, "action": self.action, "keys": list(self.keys)}


@dataclass(frozen=True)
class DrawItem:
    pool_id: str
    entry_id: str
    text: str
    draw_index: int
    seed: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "poolId": self.pool_id,
            "entryId": self.entry_id,
            "text": self.text,
            "drawIndex": self.draw_index,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class DrawResult:
    draws: tuple[DrawItem, ...] = ()

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.draws)

    def to_dict(self) -> dict[str, Any]:
        return {"draws": [item.to_dict() for item in self.draws]}


@dataclass
class AppliedGuide:
    status: str = "pending"
    source_message_id: Optional[str] = None
    consumed_by_user_message_id: Optional[str] = None
    keys: tuple[str, ...] = ()
    draws: tuple[dict[str, Any], ...] = ()
    created_at: str = ""
    flow: FlowState = field(default_factory=FlowState)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "sourceMessageId": self.source_message_id,
            "consumedByUserMessageId": self.consumed_by_user_message_id,
            "keys": list(self.keys),
            "draws": [dict(item) for item in self.draws],
            "createdAt": self.created_at,
            "flow": self.flow.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppliedGuide":
        if not isinstance(data, Mapping) or data.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("unsupported applied guide schema")
        status = _clean(data.get("status"))
        if status not in {"pending", "consumed"}:
            raise ValueError("unsupported applied guide status")
        draws = data.get("draws") or []
        if not isinstance(draws, list):
            raise ValueError("invalid applied guide draws")
        return cls(
            status=status,
            source_message_id=(None if data.get("sourceMessageId") is None else _clean(data.get("sourceMessageId"))),
            consumed_by_user_message_id=(
                None
                if data.get("consumedByUserMessageId") is None
                else _clean(data.get("consumedByUserMessageId"))
            ),
            keys=_string_tuple(data.get("keys"), limit=4),
            draws=tuple(dict(item) for item in draws if isinstance(item, Mapping)),
            created_at=_clean(data.get("createdAt")),
            flow=FlowState.from_dict(data.get("flow") or {}),
        )

