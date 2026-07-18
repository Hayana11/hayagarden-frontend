"""User Profile store — chatnest-compatible shape for frontend editing.

Persists to profile.json and injects name / preferences / saved memories
into the chat system prompt.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

PROFILE_PATH = Path(os.environ.get('USER_PROFILE_PATH', '/opt/frontend/profile.json')).expanduser()
DB_PATH = os.environ.get('MEMORIES_DB_PATH', '/opt/frontend/memories.db')
MAX_PROFILE_CHARS = 100_000
SETTINGS_KEY = 'user_profile'
LEGACY_HAYA_KEY = 'haya_profile'


def _now_ms() -> int:
    return int(time.time() * 1000)


def empty_profile() -> dict[str, Any]:
    return {
        'fullName': '',
        'nickname': '',
        'savedMemories': [],
        'preferences': {
            'enabled': True,
            'content': '',
        },
        'claudeExportImport': {},
        'updatedAt': _now_ms(),
    }


def _trim_text(value: Any, limit: int = 10_000) -> str:
    if value is None:
        return ''
    return str(value).strip()[:limit]


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_memory_text(value: str) -> str:
    return re.sub(r'\s+', ' ', value).strip()


def _coerce_memory(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        content = _trim_text(item, 4000)
        raw: dict[str, Any] = {}
    elif isinstance(item, dict):
        content = _trim_text(item.get('content'), 4000)
        raw = item
    else:
        return None
    if not content:
        return None
    now = _now_ms()
    memory = {
        'id': _trim_text(raw.get('id'), 120) or uuid4().hex,
        'content': content,
        'enabled': bool(raw.get('enabled', True)),
        'createdAt': _safe_int(raw.get('createdAt'), now),
        'updatedAt': _safe_int(raw.get('updatedAt'), now),
    }
    source = _trim_text(raw.get('source'), 80) or 'manual'
    if source not in {'manual', 'claude_export', 'migration', 'auto', 'haya_note'}:
        source = 'manual'
    memory['source'] = source
    external_id = _trim_text(raw.get('externalId'), 240)
    if external_id:
        memory['externalId'] = external_id
    imported_at = raw.get('importedAt')
    if imported_at:
        memory['importedAt'] = _safe_int(imported_at, now)
    return memory


def normalize_profile(data: Any) -> dict[str, Any]:
    base = empty_profile()
    if not isinstance(data, dict):
        return base

    base['fullName'] = _trim_text(data.get('fullName'), 200)
    base['nickname'] = _trim_text(data.get('nickname'), 200)

    memories = []
    for item in data.get('savedMemories') or []:
        memory = _coerce_memory(item)
        if memory is not None:
            memories.append(memory)
        if len(memories) >= 200:
            break
    base['savedMemories'] = memories

    preferences = data.get('preferences') or {}
    if isinstance(preferences, str):
        preferences = {'enabled': True, 'content': preferences}
    if not isinstance(preferences, dict):
        preferences = {}
    base['preferences'] = {
        'enabled': bool(preferences.get('enabled', True)),
        'content': _trim_text(preferences.get('content'), 50_000),
    }
    import_state = data.get('claudeExportImport') or {}
    if isinstance(import_state, dict):
        base['claudeExportImport'] = {
            'checkedAt': _safe_int(import_state.get('checkedAt'), 0),
            'importedAt': _safe_int(import_state.get('importedAt'), 0),
            'importedCount': _safe_int(import_state.get('importedCount'), 0),
            'foundCount': _safe_int(import_state.get('foundCount'), 0),
        }
    base['updatedAt'] = _safe_int(data.get('updatedAt'), _now_ms())
    return base


def _read_settings_value(key: str) -> str | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def _write_settings_value(key: str, value: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            'INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)',
            (key, value),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _legacy_from_haya_note() -> dict[str, Any] | None:
    raw = _read_settings_value(LEGACY_HAYA_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    note = _trim_text(data.get('note'), 50_000)
    if not note:
        return None
    profile = empty_profile()
    profile['preferences'] = {'enabled': True, 'content': note}
    profile['legacyMigratedAt'] = _now_ms()
    return profile


def _write_profile_file(profile: dict[str, Any]) -> None:
    payload = json.dumps(profile, ensure_ascii=False, indent=2)
    if len(payload) > MAX_PROFILE_CHARS:
        raise ValueError('profile is too large')
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(payload + '\n', encoding='utf-8')
    try:
        PROFILE_PATH.chmod(0o600)
    except OSError:
        pass
    _write_settings_value(SETTINGS_KEY, payload)


def read_profile(migrate_legacy: bool = True) -> dict[str, Any]:
    try:
        return normalize_profile(json.loads(PROFILE_PATH.read_text(encoding='utf-8')))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        return empty_profile()

    settings_raw = _read_settings_value(SETTINGS_KEY)
    if settings_raw:
        try:
            profile = normalize_profile(json.loads(settings_raw))
            _write_profile_file(profile)
            return profile
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    profile = empty_profile()
    if migrate_legacy:
        legacy = _legacy_from_haya_note()
        if legacy:
            profile = normalize_profile(legacy)
            _write_profile_file(profile)
            return profile
    return profile


def write_profile(data: Any) -> dict[str, Any]:
    profile = normalize_profile(data)
    profile['updatedAt'] = _now_ms()
    _write_profile_file(profile)
    return profile


def build_profile_context(profile: dict[str, Any] | None = None) -> str:
    profile = normalize_profile(profile if profile is not None else read_profile())
    lines: list[str] = []

    full_name = profile.get('fullName', '')
    nickname = profile.get('nickname', '')
    if full_name or nickname:
        lines.append('## 用户 Profile')
        if full_name:
            lines.append(f'- 全名：{full_name}')
        if nickname:
            lines.append(f'- 昵称：{nickname}')

    preferences = profile.get('preferences') or {}
    preference_text = _trim_text(preferences.get('content'), 50_000)
    if preferences.get('enabled', True) and preference_text:
        if lines:
            lines.append('')
        else:
            lines.append('## 用户 Profile')
        lines.append('### 回复偏好')
        lines.append(preference_text)

    enabled_memories = [
        item['content']
        for item in profile.get('savedMemories', [])
        if item.get('enabled', True) and item.get('content')
    ]
    if enabled_memories:
        if lines:
            lines.append('')
        else:
            lines.append('## 用户 Profile')
        lines.append('### 长期记忆')
        lines.extend(f'- {content}' for content in enabled_memories)

    return '\n'.join(lines).strip()
