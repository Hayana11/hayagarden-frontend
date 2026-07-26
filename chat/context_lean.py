"""Feature flags for context lean rollout (observation v3 stays on when disabled)."""
from __future__ import annotations


def _cfg_bool(key: str, default: bool = False) -> bool:
    try:
        import config_store
        return config_store.get_bool(key, default)
    except Exception:
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        import config_store
        return config_store.get_int(key, default)
    except Exception:
        return default


def lean_state_enabled() -> bool:
    return _cfg_bool('CONTEXT_LEAN_STATE_ENABLED', False)


def lean_history_enabled() -> bool:
    return _cfg_bool('CONTEXT_LEAN_HISTORY_ENABLED', False)


def lean_tool_budget_enabled() -> bool:
    return _cfg_bool('CONTEXT_LEAN_TOOL_BUDGET_ENABLED', False)


def lean_file_dedup_enabled() -> bool:
    return _cfg_bool('CONTEXT_LEAN_FILE_DEDUP_ENABLED', False)


def any_lean_enabled() -> bool:
    return (
        lean_state_enabled()
        or lean_history_enabled()
        or lean_tool_budget_enabled()
        or lean_file_dedup_enabled()
    )


def relay_history_high_water() -> int:
    return _cfg_int('RELAY_HISTORY_HIGH_WATER', 28000)


def relay_history_low_water() -> int:
    return _cfg_int('RELAY_HISTORY_LOW_WATER', 20000)


def cc_history_token_budget() -> int:
    return _cfg_int('HISTORY_TOKEN_BUDGET', 24000)
