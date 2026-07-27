"""P-CONTEXT-CLEAN-WINDOW-SHADOW — isolated diagnostic residents for tone A/B.

Each clean session uses the same static system as production chat but omits
all dynamic context injection (state, memory, recall, wake, formal history).
Sessions live in memory only; no DB writes.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import cc_resident
import config_store

NL = chr(10)
SESSION_PREFIX = 'clean-shadow:'
TTL_SECONDS = 30 * 60
MAX_SESSIONS = 5
MAX_TURNS_PER_SESSION = 12
SHADOW_ALLOWED_TOOLS = ''  # schema in static system; execution blocked

SAVE_RE = re.compile(r'\[\[SAVE:\s*(.*?)\]\]', re.DOTALL)

# Substrings that identify side-effect MCP tools (for manifest / tests).
_SIDE_EFFECT_TOOL_MARKERS = (
    'light_', 'save_memory', 'add_todo', 'add_ledger', 'collect_chat_moment',
    'write', 'patch', 'create_file', 'exec_vps', 'post_', 'reply_',
)


def enabled() -> bool:
    return config_store.get_bool('CC_CLEAN_WINDOW_SHADOW_ENABLED', False)


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def is_side_effect_tool(name: str) -> bool:
    base = str(name or '').split('__')[-1].lower()
    return any(marker in base for marker in _SIDE_EFFECT_TOOL_MARKERS)


def shadow_tool_result(tool_name: str) -> str:
    return 'clean_window_shadow: side effect blocked (%s)' % (tool_name or 'unknown')


def strip_save_markers(text: str) -> tuple[str, bool]:
    """Return (cleaned_text, had_save_markers). Never persists."""
    raw = str(text or '')
    had = bool(SAVE_RE.search(raw))
    cleaned = SAVE_RE.sub('', raw).strip()
    return cleaned, had


def build_base_manifest(
    *,
    session_id: str,
    static_system_sha256: str,
    persona_sha256: str,
    provider: str,
    model: str,
    turn_index: int = 0,
    shadow_history_user_turns: int = 0,
    shadow_history_assistant_turns: int = 0,
    save_marker_suppressed: bool = False,
    blocked_tool_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        'static_system_sha256': static_system_sha256,
        'persona_sha256': persona_sha256,
        'provider': provider,
        'model': model,
        'clean_window_shadow': True,
        'clean_session_id': session_id,
        'clean_turn_index': int(turn_index),
        'shadow_history_user_turns': int(shadow_history_user_turns),
        'shadow_history_assistant_turns': int(shadow_history_assistant_turns),
        'state_injected': False,
        'cold_once_injected': False,
        'long_term_memory_injected': False,
        'handoff_injected': False,
        'diary_summary_injected': False,
        'web_memo_injected': False,
        'auto_recall_injected': False,
        'ombre_recall_injected': False,
        'relationship_context_injected': False,
        'wake_bridge_injected': False,
        'one_shot_injected': False,
        'file_context_injected': False,
        'old_tool_history_injected': False,
        'formal_chat_history_injected': False,
        'formal_resident_reused': False,
        'formal_conversation_id_reused': False,
        'side_effect_tools_blocked': True,
        'save_marker_persisted': False,
        'save_marker_suppressed': bool(save_marker_suppressed),
        'blocked_tool_calls': list(blocked_tool_names or []),
    }


def _format_shadow_history(messages: list[dict[str, str]]) -> str:
    lines = []
    for msg in messages:
        role = '用户' if msg.get('role') == 'user' else '费佳'
        lines.append('[%s] %s' % (role, msg.get('content') or ''))
    return NL.join(lines)


@dataclass
class CleanWindowSession:
    session_id: str
    resident: cc_resident.ResidentSession
    messages: list[dict[str, str]]
    created_at: float
    expires_at: float
    static_system_sha256: str
    persona_sha256: str
    provider: str
    model: str
    work_dir: str
    turn_count: int = 0
    blocked_tool_calls: list[str] = field(default_factory=list)
    last_save_suppressed: bool = False

    @property
    def user_turns(self) -> int:
        return sum(1 for m in self.messages if m.get('role') == 'user')

    @property
    def assistant_turns(self) -> int:
        return sum(1 for m in self.messages if m.get('role') == 'assistant')

    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def manifest(self, **overrides) -> dict[str, Any]:
        base = build_base_manifest(
            session_id=self.session_id,
            static_system_sha256=self.static_system_sha256,
            persona_sha256=self.persona_sha256,
            provider=self.provider,
            model=self.model,
            turn_index=self.turn_count,
            shadow_history_user_turns=self.user_turns,
            shadow_history_assistant_turns=self.assistant_turns,
            save_marker_suppressed=self.last_save_suppressed,
            blocked_tool_names=self.blocked_tool_calls,
        )
        base.update(overrides)
        return base

    def close(self):
        try:
            self.resident._kill(quiet=True)
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(self.work_dir, ignore_errors=True)
        except Exception:
            pass


class CleanWindowManager:
    def __init__(
        self,
        *,
        cc_cwd: str,
        cc_token: str,
        mcp_config_path: str,
        get_provider: Callable[[], str],
        get_model: Callable[[], str],
    ):
        self._cc_cwd = cc_cwd
        self._cc_token = cc_token
        self._mcp_config_path = mcp_config_path
        self._get_provider = get_provider
        self._get_model = get_model
        self._sessions: dict[str, CleanWindowSession] = {}
        self._lock = threading.Lock()

    def _purge_expired(self):
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if s.expires_at <= now]
        for sid in expired:
            self._sessions.pop(sid, None)

    def _static_parts(self):
        from chat.system_builder import build_cc_static_parts
        return build_cc_static_parts()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._cc_token:
            env['CLAUDE_CODE_OAUTH_TOKEN'] = self._cc_token
        env.pop('ANTHROPIC_API_KEY', None)
        return env

    def start(self) -> dict[str, Any]:
        if not enabled():
            raise PermissionError('CC_CLEAN_WINDOW_SHADOW_ENABLED=0')
        self._purge_expired()
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError('clean window session limit reached (%d)' % MAX_SESSIONS)

            parts = self._static_parts()
            full_system = parts['full_system']
            static_sha = sha256_text(full_system)
            persona_sha = sha256_text(parts.get('persona') or '')

            session_id = SESSION_PREFIX + uuid.uuid4().hex
            work_dir = tempfile.mkdtemp(prefix='clean-shadow-')
            resident = cc_resident.ResidentSession(
                work_dir,
                SHADOW_ALLOWED_TOOLS,
                self._mcp_config_path,
            )
            resident.ensure_alive(full_system, self._env())

            now = time.time()
            session = CleanWindowSession(
                session_id=session_id,
                resident=resident,
                messages=[],
                created_at=now,
                expires_at=now + TTL_SECONDS,
                static_system_sha256=static_sha,
                persona_sha256=persona_sha,
                provider=self._get_provider(),
                model=self._get_model(),
                work_dir=work_dir,
            )
            self._sessions[session_id] = session
            return {
                'ok': True,
                'session_id': session_id,
                'static_system_sha256': static_sha,
                'persona_sha256': persona_sha,
                'model': session.model,
                'provider': session.provider,
                'expires_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(session.expires_at)),
                'context_manifest': session.manifest(),
            }

    def _get_session(self, session_id: str) -> CleanWindowSession:
        self._purge_expired()
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError('session not found: %s' % session_id)
        if session.expired():
            session.close()
            self._sessions.pop(session_id, None)
            raise KeyError('session expired: %s' % session_id)
        return session

    def turn(self, session_id: str, message: str) -> dict[str, Any]:
        if not enabled():
            raise PermissionError('CC_CLEAN_WINDOW_SHADOW_ENABLED=0')
        message = str(message or '').strip()
        if not message:
            raise ValueError('empty message')

        with self._lock:
            session = self._get_session(session_id)
            if session.turn_count >= MAX_TURNS_PER_SESSION:
                session.close()
                self._sessions.pop(session_id, None)
                raise RuntimeError('clean window turn limit reached (%d)' % MAX_TURNS_PER_SESSION)

            parts = self._static_parts()
            full_system = parts['full_system']
            is_cold = session.resident.ensure_alive(full_system, self._env())

            session.messages.append({'role': 'user', 'content': message})
            if is_cold:
                history = _format_shadow_history(session.messages)
                content = (
                    '以下是本诊断会话内的对话记录：' + NL + NL
                    + history + NL + NL
                    + '请回复最后一条消息。'
                )
            else:
                content = message

            text_parts: list[str] = []
            think_parts: list[str] = []
            usage: dict[str, Any] = {}
            blocked_this_turn: list[str] = []

            for evt, payload in session.resident.send_turn(content, commit_meta={}):
                if evt == 'text':
                    text_parts.append(str(payload))
                elif evt == 'think':
                    think_parts.append(str(payload))
                elif evt == 'tool_use':
                    tool_name = ''
                    if isinstance(payload, dict):
                        tool_name = str(payload.get('name') or '')
                    if is_side_effect_tool(tool_name):
                        blocked_this_turn.append(tool_name)
                elif evt == 'done':
                    if isinstance(payload, (list, tuple)) and len(payload) >= 3:
                        usage = dict(payload[2] or {}) if isinstance(payload[2], dict) else {}
                    break

            raw_text = ''.join(text_parts).strip()
            cleaned, had_save = strip_save_markers(raw_text)
            session.last_save_suppressed = had_save
            if blocked_this_turn:
                session.blocked_tool_calls.extend(blocked_this_turn)

            session.messages.append({'role': 'assistant', 'content': cleaned})
            session.turn_count += 1

            return {
                'ok': True,
                'session_id': session_id,
                'content': cleaned,
                'thinking': ''.join(think_parts).strip(),
                'turn_index': session.turn_count,
                'history_message_count': len(session.messages),
                'static_system_sha256': session.static_system_sha256,
                'context_manifest': session.manifest(),
                'usage': usage,
            }

    def close(self, session_id: str) -> dict[str, Any]:
        if not enabled():
            raise PermissionError('CC_CLEAN_WINDOW_SHADOW_ENABLED=0')
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                raise KeyError('session not found: %s' % session_id)
            session.close()
            return {'ok': True, 'session_id': session_id}

    def reset(self, session_id: Optional[str] = None) -> dict[str, Any]:
        if session_id:
            try:
                self.close(session_id)
            except KeyError:
                pass
        return self.start()


_MANAGER: Optional[CleanWindowManager] = None


def reset_manager_for_tests():
    """Clear the process-global manager singleton (tests only)."""
    global _MANAGER
    _MANAGER = None


def get_manager(
    *,
    cc_cwd: str,
    cc_token: str,
    mcp_config_path: str,
    get_provider: Callable[[], str],
    get_model: Callable[[], str],
) -> CleanWindowManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = CleanWindowManager(
            cc_cwd=cc_cwd,
            cc_token=cc_token,
            mcp_config_path=mcp_config_path,
            get_provider=get_provider,
            get_model=get_model,
        )
    return _MANAGER
