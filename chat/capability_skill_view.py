"""B1-1B — immutable CapabilitySkillView (contract §3.2).

Freezes only after provider + tools are resolved for this Wake attempt.
Answers ``现在能做什么？`` — never Drive→Action commands.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

_PARSER_ACTIONS = ('none', 'message', 'diary', 'explore')


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


def resolve_model_identity(provider: str) -> str:
    """Resolved model truth for this Wake attempt (MODEL-1B compatible).

    claude_code → cc_model_snapshot identity (default / explicit:<id>)
    api_relay   → current RelayManager.model
    """
    prov = str(provider or '').strip()
    if prov == 'claude_code':
        from chat.cc_model import cc_model_snapshot
        _raw, identity, _argv = cc_model_snapshot()
        return str(identity or 'default')
    if prov == 'api_relay':
        from relay.manager import relay as _relay
        _relay._reload_env()
        return str(_relay.model or '').strip() or 'relay:unset'
    return f'unknown:{prov or "none"}'


def _resolved_action_capability(*, provider: str, mode: str, dry_run: bool) -> tuple[str, ...]:
    """parser ∩ mode ∩ provider ∩ executor — not bare parser enum alone.

    Live Behavior Wake surfaces share the same action families; executor
    requires non-empty CONTENT for message/diary (recorded in preconditions).
    dry_run still may propose families but tools are empty (no side effects).
    """
    del provider, mode, dry_run  # reserved for future provider-specific cuts
    return tuple(_PARSER_ACTIONS)


@dataclass(frozen=True)
class CapabilitySkillView:
    """Immutable capability facts for one Wake attempt."""

    wake_run_id: Optional[str]
    captured_at: str
    provider: str
    model_identity: str
    wake_mode: str
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
    captured_at: Optional[datetime.datetime] = None,
) -> CapabilitySkillView:
    """Freeze CapabilitySkillView after prepare_tools_for_provider."""
    at = _now_str(captured_at)
    run_id = str(wake_run_id).strip() if wake_run_id else None
    if run_id == '':
        run_id = None
    prov = str(provider or '').strip() or 'unknown'
    wake_mode = str(mode or '').strip() or 'normal'
    tools = _tool_names(prepared_tools)
    model_identity = resolve_model_identity(prov)
    actions = _resolved_action_capability(
        provider=prov, mode=wake_mode, dry_run=bool(dry_run),
    )
    return CapabilitySkillView(
        wake_run_id=run_id,
        captured_at=at,
        provider=prov,
        model_identity=model_identity,
        wake_mode=wake_mode,
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
            'dry_run': bool(dry_run),
            'capability_profile': (
                'wake_dry_run' if dry_run
                else ('cc_wake' if prov == 'claude_code' else 'relay_wake')
            ),
        }),
        preconditions=_proxy({
            'message_requires_non_empty_content': True,
            'diary_requires_non_empty_content': True,
            'explore_content_optional': True,
            'none_content_empty': True,
            'tools_callable': bool(tools) and not dry_run,
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
