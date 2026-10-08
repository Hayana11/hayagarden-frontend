"""Immutable-ish normalized data types for the hidden flow foundation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


SCHEMA_VERSION = 1

HIDDEN_FLOW_CONTROL_ACTIONS = frozenset({
    "start",
    "advance",
    "stop",
    "hold",
    "continue",
})


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


def _strict_string_tuple(value: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError("invalid bounded string list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item.strip() != item or item in result:
            raise ValueError("invalid bounded string list")
        result.append(item)
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
    terminal: bool = False
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
            "terminal": self.terminal,
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
        if not isinstance(data, Mapping) or type(data.get("schemaVersion")) is not int or data.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("unsupported flow state schema")
        active = data.get("active")
        if not isinstance(active, bool):
            raise ValueError("invalid flow state active flag")
        flow_id = _clean(data.get("flowId"))
        stage = _clean(data.get("stage"))
        cycle = int(data.get("cycle") or 0)
        stage_turn = int(data.get("stageTurn") or 0)
        context_keys = _strict_string_tuple(data.get("contextKeys"), limit=4)
        fixed_raw = data.get("fixedDraws")
        if not isinstance(fixed_raw, Mapping) or any(not isinstance(key, str) for key in fixed_raw):
            raise ValueError("invalid flow state fixed draws")
        fixed_draws: dict[str, list[dict[str, Any]]] = {}
        fixed_count = 0
        fixed_text_chars = 0
        for pool_id, raw_items in fixed_raw.items():
            if not pool_id or not isinstance(raw_items, list):
                raise ValueError("invalid flow state fixed draws")
            normalized_items: list[dict[str, Any]] = []
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    raise ValueError("invalid flow state fixed draws")
                if set(raw_item) != {"poolId", "entryId", "text", "drawIndex", "seed"}:
                    raise ValueError("invalid flow state fixed draws")
                item_pool = raw_item.get("poolId")
                entry_id = raw_item.get("entryId")
                text = raw_item.get("text")
                draw_index = raw_item.get("drawIndex")
                seed = raw_item.get("seed")
                if (
                    not isinstance(item_pool, str)
                    or item_pool != pool_id
                    or not item_pool
                    or not isinstance(entry_id, str)
                    or not entry_id
                    or not isinstance(text, str)
                    or not isinstance(draw_index, int)
                    or isinstance(draw_index, bool)
                    or draw_index < 0
                    or not isinstance(seed, str)
                    or not seed
                ):
                    raise ValueError("invalid flow state fixed draws")
                normalized_items.append(
                    {
                        "poolId": item_pool,
                        "entryId": entry_id,
                        "text": text,
                        "drawIndex": draw_index,
                        "seed": seed,
                    }
                )
                fixed_count += 1
                fixed_text_chars += len(text)
                if fixed_count > 12 or fixed_text_chars > 8000:
                    raise ValueError("flow state fixed draw cap exceeded")
            fixed_draws[pool_id] = normalized_items
        try:
            json.dumps(fixed_draws, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("invalid flow state fixed draws") from None
        started_at = _clean(data.get("startedAt"))
        if active and (cycle != 1 or not flow_id or not stage or stage_turn < 1):
            raise ValueError("invalid active flow state")
        if not active and (flow_id or stage or cycle or stage_turn or context_keys or fixed_draws or started_at):
            raise ValueError("invalid inactive flow state")
        return cls(
            active=active,
            flow_id=flow_id,
            stage=stage,
            cycle=cycle,
            stage_turn=stage_turn,
            context_keys=context_keys,
            fixed_draws=fixed_draws,
            started_at=started_at,
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
    truncated: bool = False
    truncation_reason: Optional[str] = None
    text_chars: int = 0
    seed_version: int = 1

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.draws)

    def to_dict(self) -> dict[str, Any]:
        return {
            "draws": [item.to_dict() for item in self.draws],
            "truncated": self.truncated,
            "truncationReason": self.truncation_reason,
            "textChars": self.text_chars,
            "seedVersion": self.seed_version,
        }


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
        if not isinstance(data, Mapping) or type(data.get("schemaVersion")) is not int or data.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("unsupported applied guide schema")
        status = _clean(data.get("status"))
        if status not in {"pending", "consumed"}:
            raise ValueError("unsupported applied guide status")
        draws = data.get("draws") or []
        if not isinstance(draws, list) or len(draws) > 12 or any(not isinstance(item, Mapping) for item in draws):
            raise ValueError("invalid applied guide draws")
        if sum(len(_clean(item.get("text"))) for item in draws) > 8000:
            raise ValueError("applied guide text cap exceeded")
        source_message_id = None if data.get("sourceMessageId") is None else _clean(data.get("sourceMessageId"))
        consumed_by = (
            None
            if data.get("consumedByUserMessageId") is None
            else _clean(data.get("consumedByUserMessageId"))
        )
        if status == "pending" and consumed_by is not None:
            raise ValueError("pending guide cannot have a consumer")
        if status == "consumed" and (not source_message_id or not consumed_by):
            raise ValueError("consumed guide identity is incomplete")
        return cls(
            status=status,
            source_message_id=source_message_id,
            consumed_by_user_message_id=consumed_by,
            keys=_strict_string_tuple(data.get("keys"), limit=4),
            draws=tuple(dict(item) for item in draws),
            created_at=_clean(data.get("createdAt")),
            flow=FlowState.from_dict(data.get("flow") or {}),
        )
