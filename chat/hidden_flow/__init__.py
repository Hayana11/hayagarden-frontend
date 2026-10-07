"""Provider-neutral, disabled-by-default hidden flow primitives."""

from .config import DEFAULT_FLOW_CONFIG, HIDDEN_FLOW_ENGINE_ENABLED, normalize_flow_config
from .control import parse_hidden_flow_control, sanitize_hidden_flow_text
from .draw import draw_for_guide, draw_for_guide_with_state, draw_pool, render_private_guide
from .engine import apply_flow_control, start_flow
from .stream_filter import HiddenFlowStreamFilter
from .runtime import (
    HiddenFlowTransition,
    HiddenFlowTurnPlan,
    append_private_request_block,
    build_activation_block,
    build_pending_snapshot,
    control_from_dict,
    disabled_hidden_flow_plan,
    prepare_hidden_flow_turn,
    propose_transition,
    render_plan_request,
)
from .types import (
    AppliedGuide,
    CueSpec,
    DrawItem,
    DrawResult,
    FlowConfig,
    FlowControl,
    FlowState,
    PoolEntry,
    PoolSpec,
    StageSpec,
)

__all__ = [
    "AppliedGuide",
    "HiddenFlowTransition",
    "HiddenFlowTurnPlan",
    "CueSpec",
    "DEFAULT_FLOW_CONFIG",
    "DrawItem",
    "DrawResult",
    "FlowConfig",
    "FlowControl",
    "FlowState",
    "HIDDEN_FLOW_ENGINE_ENABLED",
    "HiddenFlowStreamFilter",
    "PoolEntry",
    "PoolSpec",
    "StageSpec",
    "append_private_request_block",
    "apply_flow_control",
    "build_activation_block",
    "build_pending_snapshot",
    "control_from_dict",
    "disabled_hidden_flow_plan",
    "draw_for_guide",
    "draw_for_guide_with_state",
    "draw_pool",
    "normalize_flow_config",
    "parse_hidden_flow_control",
    "prepare_hidden_flow_turn",
    "propose_transition",
    "render_plan_request",
    "render_private_guide",
    "sanitize_hidden_flow_text",
    "start_flow",
]
