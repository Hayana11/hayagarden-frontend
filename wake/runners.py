"""Wake model runners: same parser/executor, different thinking line.

ApiRelayWakeRunner wraps the existing relay agent loop (behavior unchanged).
ClaudeCodeWakeRunner uses an independent CC Wake resident — never the chat one.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from chat.provider_router import resolve_provider
from wake.cc_tools import (
    cc_wake_allowed_tools,
    cc_wake_nudge_text,
    cc_wake_tool_names,
    filter_wake_tools_for_cc,
)
from wake.usage import build_wake_cache_info

NL = chr(10)

# Modes that stay on BACKGROUND_PROVIDER in B1 — api_relay only (scheme A).
BACKGROUND_WAKE_MODES = frozenset(('summarize',))
# First-cut CC Wake modes.
CC_WAKE_MODES = frozenset(('normal', 'morning', 'nightwatch', 'ritual', 'self_trigger'))


class UnsupportedWakeModeError(ValueError):
    """Legal config that this B1 cut does not implement (fail at route time)."""
    pass

WAKE_CONTRACT = (
    '【自主唤醒合同】\n'
    '你正在做唤醒检查，不是日常聊天。不要混入值班机器人腔调到白天对话里——'
    '这次输出只服务唤醒决策。\n'
    '最终必须输出以下三行（可先用工具，再输出）：\n'
    'THOUGHTS: <你看到了什么、为什么这么决定；选 none 也要有原因链>\n'
    'ACTION: <从 none / message / diary / explore 中选一个>\n'
    'CONTENT: <ACTION=message 时写消息（不超过80字）；explore 写调研摘要；其他留空>\n'
    '禁止假装调用过不存在的工具。'
)

FORMAT_NUDGE = (
    '现在请只输出以下三行，不要其他任何内容：\n'
    'THOUGHTS: <写清楚你刚才看到了什么、为什么这么决定——即使选 none 也要有原因链>\n'
    'ACTION: <从 none / message / diary / explore 中选一个>\n'
    'CONTENT: <若 ACTION=message 则写消息内容（不超过80字）；explore 写调研摘要；其他留空>'
)


@dataclass
class WakeRequest:
    mode: str
    system: object
    messages: list
    tools: list
    t_hours: float
    wake_run_id: str = ''
    max_rounds: int = 6
    dry_run: bool = False


@dataclass
class WakeResult:
    raw_text: str
    cache_info: dict
    provider: str
    model: str


class WakeRunner(Protocol):
    def run(self, request: WakeRequest) -> WakeResult: ...


def select_wake_provider(mode: str) -> str:
    """Select only live legacy Wake routes.

    Dream is surface-owned since Provider-A3 and must never reach a Wake
    runner. Summarize remains the sole BACKGROUND_PROVIDER legacy mode.
    """
    mode = str(mode or 'normal')
    if mode == 'dream':
        raise UnsupportedWakeModeError(
            'dream generation 已迁移到 surface-owned Background Generation Adapter'
        )
    if mode in BACKGROUND_WAKE_MODES:
        provider = resolve_provider('background')
        if provider != 'api_relay':
            raise UnsupportedWakeModeError(
                'summarize 仅支持 BACKGROUND_PROVIDER=api_relay，当前为 %s' % provider
            )
        return provider
    return resolve_provider('wake')


def prepare_tools_for_provider(
    provider: str,
    tools: list,
    mode: str,
    *,
    dry_run: bool = False,
) -> list:
    if dry_run:
        # Safest dry_run contract: model may think, but no tool side effects.
        return []
    if provider == 'claude_code' and mode in CC_WAKE_MODES:
        return filter_wake_tools_for_cc(tools)
    return list(tools or [])


def should_prompt_readonly_tools(
    *,
    dry_run: bool,
    tools: list | None,
    generative: bool,
    tools_called: bool,
    t_hours: float,
    round_i: int,
    max_rounds: int,
) -> bool:
    """Whether the Relay wake loop may append a "please call a tool" nudge.

    dry_run or empty tools must never nudge — there is nothing callable.
    """
    return (
        (not dry_run)
        and bool(tools)
        and (not generative)
        and (not tools_called)
        and float(t_hours or 0.0) >= 1.0
        and round_i < max_rounds - 1
    )


def split_wake_system(system: object) -> tuple[str, str]:
    """Stable (cacheable) vs dynamic wake blocks for the CC resident system/prompt."""
    if isinstance(system, list):
        stable_parts: list[str] = []
        dynamic_parts: list[str] = []
        for block in system:
            if not isinstance(block, dict):
                continue
            text = str(block.get('text') or '').strip()
            if not text:
                continue
            if block.get('cache_control'):
                stable_parts.append(text)
            else:
                dynamic_parts.append(text)
        if not stable_parts and dynamic_parts:
            stable_parts.append(dynamic_parts.pop(0))
        return NL.join(stable_parts), (NL + NL).join(dynamic_parts)
    text = str(system or '').strip()
    return text, ''


def _messages_trigger(messages: list) -> str:
    parts = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get('content')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append(' '.join(
                b.get('text', '') for b in content
                if isinstance(b, dict) and b.get('text')
            ))
    return NL.join(p for p in parts if p).strip() or '[唤醒检查]'


class ApiRelayWakeRunner:
    """Thin wrapper around the existing relay wake agent loop."""

    def __init__(
        self,
        loop_fn: Callable[..., tuple[str, dict]],
        *,
        model_getter: Optional[Callable[[], str]] = None,
    ):
        self._loop_fn = loop_fn
        self._model_getter = model_getter

    def run(self, request: WakeRequest) -> WakeResult:
        raw_text, cache_info = self._loop_fn(
            request.system,
            request.messages,
            max_rounds=request.max_rounds,
            tools=request.tools,
            t_hours=request.t_hours,
            mode=request.mode,
            dry_run=bool(request.dry_run),
        )
        cache_info = dict(cache_info or {})
        cache_info['provider'] = 'api_relay'
        cache_info['source'] = 'wake'
        model = str(cache_info.get('model') or '')
        if not model and self._model_getter:
            try:
                model = str(self._model_getter() or '')
            except Exception:
                model = ''
        if model:
            cache_info['model'] = model
        return WakeResult(
            raw_text=raw_text or '',
            cache_info=cache_info,
            provider='api_relay',
            model=model or str(cache_info.get('model') or ''),
        )


class ClaudeCodeWakeRunner:
    """Independent CC Wake resident — never reuses the solo-chat resident."""

    def __init__(
        self,
        resident,
        *,
        token: str,
        cwd: str,
        payload_builder: Callable[..., dict],
        mcp_config_path: str = '',
    ):
        self._resident = resident
        self._token = token
        self._cwd = cwd
        self._payload_builder = payload_builder
        self._mcp_config_path = mcp_config_path or (cwd.rstrip('/') + '/cc-tools.json')

    def run(self, request: WakeRequest) -> WakeResult:
        if not self._token:
            raise RuntimeError('未配置订阅 token，无法使用 claude_code Wake')
        if request.mode not in CC_WAKE_MODES:
            raise UnsupportedWakeModeError(
                'claude_code Wake 暂不支持 mode=%s；'
                'summarize 请保持 BACKGROUND_PROVIDER=api_relay'
                % request.mode
            )

        started = time.monotonic()
        stable, dynamic = split_wake_system(request.system)
        full_system = stable.strip()
        if WAKE_CONTRACT not in full_system:
            full_system = (full_system + NL + NL + WAKE_CONTRACT).strip()

        os.makedirs(self._cwd, exist_ok=True)
        env = dict(os.environ)
        env['CLAUDE_CODE_OAUTH_TOKEN'] = self._token
        env.pop('ANTHROPIC_API_KEY', None)

        # dry_run: empty allowlist. Otherwise match filtered tool table.
        if request.dry_run or not request.tools:
            allowed = ''
        else:
            allowed = cc_wake_allowed_tools(request.tools)
        if getattr(self._resident, '_allowed_tools', None) != allowed:
            # Tool allowlist change requires respawn (system_changed alone is not enough).
            self._resident._allowed_tools = allowed
            if getattr(self._resident, '_system_text', None) is not None:
                self._resident._system_text = None  # force ensure_alive to respawn

        self._resident.ensure_alive(full_system, env)

        tool_names = cc_wake_tool_names(request.tools)
        nudge = cc_wake_nudge_text(
            request.t_hours, tool_names, dry_run=bool(request.dry_run),
        )
        trigger = _messages_trigger(request.messages)
        pieces = [p for p in (dynamic.strip(), nudge, trigger) if p]
        content = (NL + NL).join(pieces)

        text_acc: list[str] = []
        usage_rounds: list[dict] = []
        last_usage: dict = {}
        text_acc.append(self._drain_turn(content, usage_rounds, last_usage))

        combined = NL.join(t for t in text_acc if t).strip()
        if 'ACTION' not in combined.upper():
            text_acc.append(self._drain_turn(FORMAT_NUDGE, usage_rounds, last_usage))
            combined = NL.join(t for t in text_acc if t).strip()

        model = 'claude-code'
        cache_info = build_wake_cache_info(
            usage_rounds,
            elapsed_sec=time.monotonic() - started,
            cache_supported=True,
            mode=request.mode,
            model=model,
            payload_builder=self._payload_builder,
            provider='claude_code',
            resident_turn_count=last_usage.get('resident_turn_count'),
            respawn_reason=last_usage.get('respawn_reason') or '',
        )
        return WakeResult(
            raw_text=combined,
            cache_info=cache_info,
            provider='claude_code',
            model=model,
        )

    def _drain_turn(self, content: str, usage_rounds: list, last_usage: dict) -> str:
        text = ''
        for evt, payload in self._resident.send_turn(content):
            if evt != 'done':
                continue
            if isinstance(payload, tuple) and len(payload) >= 3:
                text = str(payload[0] or '')
                thinking = str(payload[1] or '')
                usage = payload[2] if isinstance(payload[2], dict) else {}
                last_usage.clear()
                last_usage.update(usage)
                for row in usage.get('rounds') or []:
                    if isinstance(row, dict):
                        usage_rounds.append(dict(row))
                if thinking and 'THOUGHTS' not in text:
                    text = (thinking.strip() + NL + text).strip()
        return text


_RUNNER_FACTORY: dict[str, Callable[[], WakeRunner]] = {}


def register_wake_runners(
    *,
    api_relay: WakeRunner,
    claude_code: WakeRunner,
) -> None:
    _RUNNER_FACTORY['api_relay'] = lambda: api_relay
    _RUNNER_FACTORY['claude_code'] = lambda: claude_code


def get_wake_runner(provider: str) -> WakeRunner:
    provider = str(provider or '').strip().lower()
    factory = _RUNNER_FACTORY.get(provider)
    if factory is None:
        raise RuntimeError('没有可用的 Wake runner: provider=%s' % provider)
    return factory()


def inspect_wake_plan(
    *,
    mode: str,
    system: object,
    messages: list,
    tools: list,
    t_hours: float,
    wake_run_id: str = '',
) -> dict[str, Any]:
    """Build-only view for inspect_only — no model call."""
    provider = select_wake_provider(mode)
    prepared = prepare_tools_for_provider(provider, tools, mode, dry_run=False)
    stable, dynamic = split_wake_system(system)
    flat = (stable + NL + dynamic).lower()
    return {
        'provider': provider,
        'mode': mode,
        'wake_run_id': wake_run_id,
        't_hours': t_hours,
        'capability_profile': 'cc_wake' if provider == 'claude_code' else 'relay_wake',
        'tool_names': [t.get('name') for t in prepared if t.get('name')],
        'relay_only_removed': (
            sorted({
                str(t.get('name')) for t in (tools or [])
                if t.get('name') and t.get('name') not in {
                    x.get('name') for x in prepared
                }
            }) if provider == 'claude_code' else []
        ),
        'stable_chars': len(stable),
        'dynamic_chars': len(dynamic),
        'trigger': _messages_trigger(messages),
        'cc_allowed_tools': (
            cc_wake_allowed_tools(prepared).split(',')
            if provider == 'claude_code' else []
        ),
        # Sanity flags for reviewers / deploy checklist.
        # Use Relay-brochure-unique phrases (not the CC "不可用：…" denylist).
        'prompt_claims_relay_only_tools': any(
            needle in flat for needle in (
                '查看与发布留言板', '随心所欲', '请求手机截屏', '查位置',
                'pocket 浏览器', 'pocket_*',
            )
        ) if provider == 'claude_code' else False,
    }
