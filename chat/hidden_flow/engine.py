"""Pure flow state transitions with fail-closed boundaries."""

from __future__ import annotations

from typing import Optional

from .config import normalize_flow_config
from .types import FlowConfig, FlowControl, FlowState


def _inactive() -> FlowState:
    return FlowState.inactive()


def start_flow(
    config: Optional[FlowConfig],
    control: Optional[FlowControl],
    *,
    started_at: str = "",
) -> FlowState:
    """Create cycle one only for an explicit matching start action."""
    normalized = normalize_flow_config(config)
    if (
        normalized is None
        or not normalized.enabled
        or control is None
        or control.flow_id != normalized.flow_id
        or control.action != "start"
        or not normalized.initial_stage
    ):
        return _inactive()
    return FlowState(
        active=True,
        flow_id=normalized.flow_id,
        stage=normalized.initial_stage,
        cycle=1,
        stage_turn=1,
        context_keys=tuple(control.keys[:4]),
        fixed_draws={},
        started_at=str(started_at or ""),
    )


def apply_flow_control(
    config: Optional[FlowConfig],
    state: Optional[FlowState],
    control: Optional[FlowControl] = None,
    *,
    boundary_override: bool = False,
) -> FlowState:
    """Advance one turn, or fail closed when the caller reports a boundary."""
    normalized = normalize_flow_config(config)
    if boundary_override or normalized is None or not normalized.enabled:
        return _inactive()
    current = state or _inactive()
    if not current.active:
        return start_flow(normalized, control)
    if current.flow_id != normalized.flow_id:
        return _inactive()
    stage = normalized.stage(current.stage)
    if stage is None:
        return _inactive()
    if control is not None and control.flow_id != normalized.flow_id:
        return FlowState(
            active=current.active,
            flow_id=current.flow_id,
            stage=current.stage,
            cycle=current.cycle,
            stage_turn=current.stage_turn,
            context_keys=current.context_keys,
            fixed_draws=dict(current.fixed_draws),
            started_at=current.started_at,
        )
    if control is not None and control.action == "stop":
        return _inactive()
    if control is not None and control.action == "start":
        control = None
    if stage.terminal_without_continue:
        if control is not None and control.action == "continue" and stage.continue_target:
            return FlowState(
                active=True,
                flow_id=normalized.flow_id,
                stage=stage.continue_target,
                cycle=current.cycle + 1,
                stage_turn=1,
                context_keys=current.context_keys,
                fixed_draws={},
                started_at=current.started_at,
            )
        return _inactive()
    minimum = stage.repeat_min_turns if current.cycle > 1 else stage.min_turns
    if current.stage_turn < minimum:
        return FlowState(
            active=True,
            flow_id=current.flow_id,
            stage=current.stage,
            cycle=current.cycle,
            stage_turn=current.stage_turn + 1,
            context_keys=current.context_keys,
            fixed_draws=dict(current.fixed_draws),
            started_at=current.started_at,
        )
    if control is not None and control.action == "hold" and stage.holdable:
        return FlowState(
            active=True,
            flow_id=current.flow_id,
            stage=current.stage,
            cycle=current.cycle,
            stage_turn=current.stage_turn + 1,
            context_keys=current.context_keys,
            fixed_draws=dict(current.fixed_draws),
            started_at=current.started_at,
        )
    if not stage.next_stage:
        return _inactive()
    return FlowState(
        active=True,
        flow_id=current.flow_id,
        stage=stage.next_stage,
        cycle=current.cycle,
        stage_turn=1,
        context_keys=current.context_keys,
        fixed_draws=dict(current.fixed_draws),
        started_at=current.started_at,
    )

