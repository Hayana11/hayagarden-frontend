"""Provider-neutral, disabled-by-default hidden flow primitives."""

from .config import DEFAULT_FLOW_CONFIG, HIDDEN_FLOW_ENGINE_ENABLED, normalize_flow_config
from .control import parse_hidden_flow_control, sanitize_hidden_flow_text
from .draw import draw_for_guide, draw_for_guide_with_state, draw_pool, render_private_guide
from .engine import apply_flow_control, start_flow
from .stream_filter import HiddenFlowStreamFilter
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
    "apply_flow_control",
    "draw_for_guide",
    "draw_for_guide_with_state",
    "draw_pool",
    "normalize_flow_config",
    "parse_hidden_flow_control",
    "render_private_guide",
    "sanitize_hidden_flow_text",
    "start_flow",
]
