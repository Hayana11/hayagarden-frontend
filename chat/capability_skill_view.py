"""B1-1B — immutable CapabilitySkillView (contract §3.2).

Freezes only after provider + tools are resolved for this Wake attempt.
Answers ``现在能做什么？`` — never Drive→Action commands.

resolved_action_capability = parser ∩ mode ∩ provider contract ∩ executor.
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
_SPECIAL_RITUAL_TYPES = frozenset({'solstice', 'birthday'})


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


def _provider_content_policy(provider: str) -> str:
    """How the provider Wake contract treats ACTION CONTENT.

    CC WAKE_CONTRACT: CONTENT only for message/explore; diary left empty
    (then executor rejects empty diary) ⇒ diary not executor-resolved.
    Relay: diary remains content-aligned executable capability (contract §3.2).
    """
    if str(provider or '').strip() == 'claude_code':
        return 'cc_content_message_explore_only'
    return 'relay_content_aligned'


def mode_action_contract(
    mode: str,
    ritual_type: str = '',
) -> tuple[tuple[str, ...], str]:
    """Return (mode-allowed actions, contract_id) from real Wake prompt truth.

    Mirrors ``wake.builder._load_template`` mode/ritual_type selection:
    - nightwatch → NIGHTWATCH_DECISION_PROMPT (no explore)
    - ritual solstice/birthday → message-only special ritual prompts
    - ritual generic → WAKE_DECISION_PROMPT four-action set
    - normal / morning / self_trigger → four-action Decision prompts
    """
    wake_mode = str(mode or '').strip() or 'normal'
    rtype = str(ritual_type or '').strip().lower()

    if wake_mode not in _BEHAVIOR_MODES:
        return ('none',), 'non_behavior_mode'

    if wake_mode == 'nightwatch':
        return ('none', 'message', 'diary'), 'nightwatch_decision'

    if wake_mode == 'ritual':
        if rtype in _SPECIAL_RITUAL_TYPES:
            # RITUAL_SOLSTICE / RITUAL_BIRTHDAY: "只输出消息"
            return ('message',), f'ritual_{rtype}_message_only'
        # Generic ritual falls back to WAKE_DECISION_PROMPT.
        return tuple(_PARSER_ACTIONS), 'ritual_generic_wake_decision'

    if wake_mode == 'morning':
        return tuple(_PARSER_ACTIONS), 'morning_decision'

    # normal / self_trigger share WAKE_DECISION_PROMPT four-action set.
    return tuple(_PARSER_ACTIONS), f'{wake_mode}_wake_decision'


def resolved_action_capability_for(
    *,
    provider: str,
    mode: str,
    dry_run: bool = False,
    ritual_type: str = '',
) -> tuple[str, ...]:
    """parser ∩ mode ∩ provider ∩ executor — not bare parser enum."""
    del dry_run  # tools emptiness is separate; action families still decidable
    prov = str(provider or '').strip()
    wake_mode = str(mode or '').strip() or 'normal'

    mode_allowed, _contract_id = mode_action_contract(wake_mode, ritual_type)
    if wake_mode not in _BEHAVIOR_MODES:
        return ('none',)

    # parser ∩ mode
    allowed = set(mode_allowed) & set(_PARSER_ACTIONS)

    # Executor preconditions: message/diary require non-empty CONTENT.
    # An action family remains "resolved executable" only if the provider
    # contract allows supplying that CONTENT.
    policy = _provider_content_policy(prov)
    if policy == 'cc_content_message_explore_only':
        # CC contract tells model to leave diary CONTENT empty → executor reject.
        allowed.discard('diary')

    # Stable order matching parser enum.
    return tuple(a for a in _PARSER_ACTIONS if a in allowed)


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
    policy = _provider_content_policy(prov)
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
            'capability_profile': (
                'wake_dry_run' if dry_run
                else ('cc_wake' if prov == 'claude_code' else 'relay_wake')
            ),
            'provider_content_policy': policy,
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
