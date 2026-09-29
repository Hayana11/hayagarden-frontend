"""B1-1B — immutable CapabilitySkillView (contract §3.2).

Freezes only after provider + tools are resolved for this Wake attempt.
Answers ``现在能做什么？`` — never Drive→Action commands.

resolved_action_capability comes from the current production route and
executor ownership, not a retired Claude CONTENT contract.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

_PARSER_ACTIONS = ('none', 'message', 'diary', 'explore')
_BEHAVIOR_MODES = frozenset({
    'normal', 'morning', 'nightwatch', 'ritual', 'self_trigger',
})
_UNIFIED_ACTION_ORDER = ('none', 'message')


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _now_str(dt: Optional[datetime.datetime] = None) -> str:
    return (dt or _now_beijing()).strftime('%Y-%m-%d %H:%M:%S')


def _proxy(data: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(data))


def _tool_names(tools: Sequence[Any]) -> tuple[str, ...]:
    names: list[str] = []
    for tool in tools or ():
        if isinstance(tool, dict):
            name = str(tool.get('name') or '').strip()
            if name:
                names.append(name)
        else:
            name = str(getattr(tool, 'name', '') or '').strip()
            if name:
                names.append(name)
    return tuple(names)


def _isolated_relay_manager():
    """Fresh RelayManager instance — never mutates the production global singleton."""
    from relay.manager import RelayManager
    return RelayManager()


def resolve_model_identity(provider: str) -> str:
    """Resolved model truth for this Wake attempt (MODEL-1B compatible).

    claude_code → cc_model_snapshot identity (default / explicit:<id>)
    api_relay   → isolated RelayManager snapshot (not global relay._reload_env)
    """
    prov = str(provider or '').strip()
    if prov == 'claude_code':
        from chat.cc_model import cc_model_snapshot
        _raw, identity, _argv = cc_model_snapshot()
        return str(identity or 'default')
    if prov == 'api_relay':
        mgr = _isolated_relay_manager()
        return str(mgr.model or '').strip() or 'relay:unset'
    return f'unknown:{prov or "none"}'


def _disabled_wake_mode(mode: str) -> bool:
    from wake.runners import wake_mode_disabled
    return wake_mode_disabled(mode)


def _unified_claude_normal_actions() -> tuple[str, ...]:
    """Canonical Claude normal Wake: B2 owns none, B3 owns message."""
    from chat.behavior_authority_b2 import owned_actions as b2_owned
    from chat.behavior_authority_b3 import owned_actions as b3_owned
    owned = set(b2_owned()) | set(b3_owned())
    return tuple(action for action in _UNIFIED_ACTION_ORDER if action in owned)


def route_capability_authority(provider: str, mode: str) -> str:
    """Why resolved_action_capability is this set — current route, not CONTENT policy."""
    prov = str(provider or '').strip()
    wake_mode = str(mode or '').strip() or 'normal'
    if _disabled_wake_mode(wake_mode):
        return 'wake_mode_disabled'
    if wake_mode == 'dream':
        return 'surface_owned_background_generation'
    if wake_mode == 'summarize':
        return 'api_relay_background'
    if prov == 'claude_code' and wake_mode == 'normal':
        return 'unified_b2_b3_route'
    if prov == 'api_relay':
        return 'relay_parser_executor'
    return 'unknown_route'


def mode_action_contract(
    mode: str,
    ritual_type: str = '',
) -> tuple[tuple[str, ...], str]:
    """Return (mode-allowed actions, contract_id) from current production route.

    Retired Claude Wake modes have no production Action. Ritual type is ignored
    because ritual itself is disabled. Diary/Web parity is not claimed here.
    """
    del ritual_type
    wake_mode = str(mode or '').strip() or 'normal'

    if _disabled_wake_mode(wake_mode):
        return (), 'wake_mode_disabled'

    if wake_mode == 'dream':
        return (), 'surface_owned'

    if wake_mode == 'summarize':
        return tuple(_PARSER_ACTIONS), 'api_relay_background'

    if wake_mode not in _BEHAVIOR_MODES:
        return ('none',), 'non_behavior_mode'

    # normal: parser vocabulary before provider/route intersection.
    return tuple(_PARSER_ACTIONS), 'normal_wake_decision'


def resolved_action_capability_for(
    *,
    provider: str,
    mode: str,
    dry_run: bool = False,
    ritual_type: str = '',
) -> tuple[str, ...]:
    """Route ∩ executor ownership — not a retired Claude CONTENT contract."""
    del dry_run  # tools emptiness is separate; action families still decidable
    prov = str(provider or '').strip()
    wake_mode = str(mode or '').strip() or 'normal'

    if _disabled_wake_mode(wake_mode):
        return ()

    if wake_mode == 'dream':
        return ()

    if prov == 'claude_code' and wake_mode == 'normal':
        return _unified_claude_normal_actions()

    mode_allowed, _contract_id = mode_action_contract(wake_mode, ritual_type)
    if wake_mode not in _BEHAVIOR_MODES and wake_mode != 'summarize':
        return ('none',)

    allowed = set(mode_allowed) & set(_PARSER_ACTIONS)
    return tuple(action for action in _PARSER_ACTIONS if action in allowed)


def _capability_profile(provider: str, mode: str, *, dry_run: bool) -> str:
    if dry_run:
        return 'wake_dry_run'
    wake_mode = str(mode or '').strip() or 'normal'
    if _disabled_wake_mode(wake_mode):
        return 'wake_mode_disabled'
    prov = str(provider or '').strip()
    if prov == 'claude_code' and wake_mode == 'normal':
        return 'unified_hot_resident'
    if wake_mode == 'summarize' or prov == 'api_relay':
        return 'relay_wake'
    return 'unknown'


@dataclass(frozen=True)
class CapabilitySkillView:
    """Immutable capability facts for one Wake attempt."""

    wake_run_id: Optional[str]
    captured_at: str
    provider: str
    model_identity: str
    wake_mode: str
    ritual_type: str
    resolved_action_capability: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    available_tools: tuple[str, ...]
    provider_availability: Mapping[str, Any]
    mode_contract: Mapping[str, Any]
    preconditions: Mapping[str, Any]
    external_effect_class: Mapping[str, Any]
    source: str = 'wake_run_resolved_facts'
    frozen_after: str = 'prepare_tools_for_provider'
    immutable: bool = True

    @property
    def allowed_action_families(self) -> tuple[str, ...]:
        """Alias for contract / task wording."""
        return self.resolved_action_capability

    @property
    def external_effect_capabilities(self) -> Mapping[str, Any]:
        return self.external_effect_class


def freeze_capability_skill_view(
    *,
    wake_run_id: Optional[str],
    provider: str,
    mode: str,
    prepared_tools: Sequence[Any],
    dry_run: bool = False,
    ritual_type: str = '',
    captured_at: Optional[datetime.datetime] = None,
) -> CapabilitySkillView:
    """Freeze CapabilitySkillView after prepare_tools_for_provider."""
    at = _now_str(captured_at)
    run_id = str(wake_run_id).strip() if wake_run_id else None
    if run_id == '':
        run_id = None
    prov = str(provider or '').strip() or 'unknown'
    wake_mode = str(mode or '').strip() or 'normal'
    rtype = str(ritual_type or '').strip().lower()
    tools = _tool_names(prepared_tools)
    model_identity = resolve_model_identity(prov)
    authority = route_capability_authority(prov, wake_mode)
    mode_allowed, mode_contract_id = mode_action_contract(wake_mode, rtype)
    actions = resolved_action_capability_for(
        provider=prov,
        mode=wake_mode,
        dry_run=bool(dry_run),
        ritual_type=rtype,
    )
    return CapabilitySkillView(
        wake_run_id=run_id,
        captured_at=at,
        provider=prov,
        model_identity=model_identity,
        wake_mode=wake_mode,
        ritual_type=rtype,
        resolved_action_capability=actions,
        tool_allowlist=tools,
        available_tools=tools,
        provider_availability=_proxy({
            'provider': prov,
            'model_identity': model_identity,
            'selected': True,
        }),
        mode_contract=_proxy({
            'mode': wake_mode,
            'ritual_type': rtype,
            'mode_contract_id': mode_contract_id,
            'mode_action_vocabulary': list(mode_allowed),
            'dry_run': bool(dry_run),
            'capability_profile': _capability_profile(
                prov, wake_mode, dry_run=bool(dry_run),
            ),
            'route_capability_authority': authority,
        }),
        preconditions=_proxy({
            'message_requires_non_empty_content': True,
            'diary_requires_non_empty_content': True,
            'explore_content_optional': True,
            'none_content_empty': True,
            'tools_callable': bool(tools) and not dry_run,
            'diary_executor_resolved': 'diary' in actions,
            'explore_mode_resolved': 'explore' in actions,
        }),
        external_effect_class=_proxy({
            'none': 'no_external_effect',
            'message': 'chat_delivery',
            'diary': 'diary_post',
            'explore': 'readonly_or_summary',
        }),
        source='wake_run_resolved_facts',
        frozen_after='prepare_tools_for_provider',
        immutable=True,
    )
