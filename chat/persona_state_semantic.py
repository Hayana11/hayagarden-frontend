"""Persona-visible semantic state: raw backend snippets → canonical feelings.

Raw state from build_cc_state(lean=True) stays intact for commit/recovery.
Only formatted snapshot/diff strings reach the model.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

_FLOAT_PAIR_RE = re.compile(r'([a-zA-Z_]+)=([0-9.]+)')
_MOOD_RE = re.compile(r'mood=([^\s]+)')

# Discrete tiers: same raw float bucket → identical sentence (hot delta stability).
_FATIGUE_TIERS = (
    (0.65, '身体明显疲惫。'),
    (0.45, '身体有些疲惫。'),
    (0.25, '身体略有些疲惫。'),
    (0.0, '身体状态尚可。'),
)
_STRESS_TIERS = (
    (0.50, '内心紧绷比较明显。'),
    (0.30, '内心略有紧绷。'),
    (0.0, '内心比较松弛。'),
)
_LONGING_TIERS = (
    (0.45, '想念她的感觉比较明显。'),
    (0.25, '心里有一点想念她。'),
)
_INTIMACY_TIERS = (
    (0.75, '对亲近她的欲望很明显。'),
    (0.55, '对亲近她的欲望比较明显。'),
    (0.30, '对亲近她有一点想靠近。'),
)
_SOCIAL_TIERS = (
    (0.55, '更愿意说话，而不是独处。'),
    (0.30, '社交意愿一般。'),
    (0.0, '更想独处。'),
)
_CURIOSITY_TIERS = (
    (0.55, '好奇心比较活跃，想往外探究。'),
    (0.30, '有一点想探究外面的事。'),
)
_REFLECTION_TIERS = (
    (0.55, '注意力向内，容易想事情。'),
    (0.30, '偶尔会陷入琢磨。'),
)

_SECTION_ORDER = ('inner_state', 'environment', 'external_awareness')

_INNER_KEY_ORDER = (
    'emotional_tone',
    'bodily_energy',
    'inner_tension',
    'longing_pull',
    'intimacy_pull',
    'social_openness',
    'mental_direction',
)
_ENV_KEY_ORDER = ('lighting',)
_EXT_KEY_ORDER = ('pocket_relay',)


def _tier_label(value: float, tiers: tuple[tuple[float, str], ...]) -> Optional[str]:
    for threshold, label in tiers:
        if value >= threshold:
            return label
    return None


def _parse_float_pairs(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for match in _FLOAT_PAIR_RE.finditer(str(text or '')):
        try:
            out[match.group(1)] = float(match.group(2))
        except ValueError:
            continue
    return out


def _parse_mood_word(emotion_text: str) -> Optional[str]:
    match = _MOOD_RE.search(str(emotion_text or ''))
    if not match:
        return None
    word = match.group(1).strip()
    if not word or word == 'unknown':
        return None
    return word


def _emotional_tone_sentence(emotion_text: str) -> Optional[str]:
    text = str(emotion_text or '').strip()
    if not text:
        return None
    mood = _parse_mood_word(text)
    if mood == '平静':
        return '情绪整体平稳。'
    if mood:
        return f'情绪基调{mood}。'
    return None


def _intimacy_score(
    emotion_pairs: dict[str, float],
    drive_pairs: dict[str, float],
) -> Optional[float]:
    candidates: list[float] = []
    if 'libido' in drive_pairs:
        candidates.append(drive_pairs['libido'])
    if 'desire_p' in emotion_pairs:
        candidates.append(emotion_pairs['desire_p'])
    if 'desire_c' in emotion_pairs:
        candidates.append(emotion_pairs['desire_c'])
    if 'attachment' in drive_pairs:
        candidates.append(drive_pairs['attachment'])
    if not candidates:
        return None
    return max(candidates)


def _mental_direction_sentence(drive_pairs: dict[str, float]) -> Optional[str]:
    if 'curiosity' not in drive_pairs and 'reflection' not in drive_pairs:
        return None
    curiosity = drive_pairs.get('curiosity', 0.0)
    reflection = drive_pairs.get('reflection', 0.0)
    curiosity_line = _tier_label(curiosity, _CURIOSITY_TIERS)
    reflection_line = _tier_label(reflection, _REFLECTION_TIERS)
    if curiosity >= 0.30 and curiosity >= reflection:
        return curiosity_line
    if reflection >= 0.30:
        return reflection_line
    return None


def _light_phrase(token: str) -> str:
    token = str(token or '').strip().lower()
    if not token:
        return '状态不明'
    if token in ('unknown', '未知', '暂不可读'):
        return '状态不明'
    if token in ('关', 'off', '0', 'false'):
        return '关着'
    if token in ('开', 'on', '1', 'true'):
        return '亮着'
    if '亮' in token or '开' in token:
        return '亮着'
    if '关' in token or '暗' in token:
        return '关着'
    return '状态不明'


def _lighting_sentence(lights_text: str) -> Optional[str]:
    text = str(lights_text or '').strip()
    if not text:
        return None
    if '=' in text and 'main' in text:
        pairs = {}
        for part in text.split():
            if '=' in part:
                k, v = part.split('=', 1)
                pairs[k.strip()] = v.strip()
        main = _light_phrase(pairs.get('main', ''))
        bedside = _light_phrase(pairs.get('bedside', ''))
        return f'主灯{main}，床头灯{bedside}。'
    main_match = re.search(r'主灯\s*([关开着暗亮]+)', text)
    bedside_match = re.search(r'床头灯\s*([关开着暗亮]+)', text)
    if main_match or bedside_match:
        main = _light_phrase(main_match.group(1) if main_match else 'unknown')
        bedside = _light_phrase(bedside_match.group(1) if bedside_match else 'unknown')
        return f'主灯{main}，床头灯{bedside}。'
    return None


def _pocket_relay_sentence(pocket_text: str) -> Optional[str]:
    text = str(pocket_text or '').strip()
    if not text:
        return None
    connected = None
    if 'phone_connected=true' in text.replace(' ', '').lower():
        connected = True
    elif 'phone_connected=false' in text.replace(' ', '').lower():
        connected = False
    elif '在线' in text and '离线' not in text:
        connected = True
    elif '离线' in text:
        connected = False
    if connected is True:
        return '手机 Pocket 浏览器通道目前在线。'
    if connected is False:
        return '手机 Pocket 浏览器通道目前离线。'
    return None


def translate_raw_state_to_persona_semantic(
    raw_state: Optional[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Map backend raw state dict to persona-visible semantic sections."""
    raw = {str(k): str(v or '').strip() for k, v in (raw_state or {}).items()}
    emotion_pairs = _parse_float_pairs(raw.get('emotion', ''))
    drive_pairs = _parse_float_pairs(raw.get('drive', ''))

    inner: dict[str, str] = {}
    tone = _emotional_tone_sentence(raw.get('emotion', ''))
    if tone:
        inner['emotional_tone'] = tone

    if 'fatigue' in drive_pairs:
        fatigue_line = _tier_label(drive_pairs['fatigue'], _FATIGUE_TIERS)
        if fatigue_line:
            inner['bodily_energy'] = fatigue_line

    if 'stress' in drive_pairs:
        stress_line = _tier_label(drive_pairs['stress'], _STRESS_TIERS)
        if stress_line:
            inner['inner_tension'] = stress_line

    if 'longing' in emotion_pairs:
        longing_line = _tier_label(emotion_pairs['longing'], _LONGING_TIERS)
        if longing_line:
            inner['longing_pull'] = longing_line

    intimacy_value = _intimacy_score(emotion_pairs, drive_pairs)
    if intimacy_value is not None:
        intimacy_line = _tier_label(intimacy_value, _INTIMACY_TIERS)
        if intimacy_line:
            inner['intimacy_pull'] = intimacy_line

    if 'social' in drive_pairs:
        social_line = _tier_label(drive_pairs['social'], _SOCIAL_TIERS)
        if social_line:
            inner['social_openness'] = social_line

    mental_line = _mental_direction_sentence(drive_pairs)
    if mental_line:
        inner['mental_direction'] = mental_line

    environment: dict[str, str] = {}
    lighting_line = _lighting_sentence(raw.get('lights', ''))
    if lighting_line:
        environment['lighting'] = lighting_line

    external: dict[str, str] = {}
    pocket_line = _pocket_relay_sentence(raw.get('pocket', ''))
    if pocket_line:
        external['pocket_relay'] = pocket_line

    return {
        'inner_state': inner,
        'environment': environment,
        'external_awareness': external,
    }


def _flatten_semantic(semantic: Optional[Mapping[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    semantic = semantic or {}
    key_orders = {
        'inner_state': _INNER_KEY_ORDER,
        'environment': _ENV_KEY_ORDER,
        'external_awareness': _EXT_KEY_ORDER,
    }
    for section in _SECTION_ORDER:
        bucket = semantic.get(section) or {}
        if not isinstance(bucket, dict):
            continue
        seen: set[str] = set()
        for key in key_orders.get(section, ()):
            value = str(bucket.get(key) or '').strip()
            if value:
                out[f'{section}.{key}'] = value
                seen.add(key)
        for key, value in bucket.items():
            if key in seen:
                continue
            text = str(value or '').strip()
            if text:
                out[f'{section}.{key}'] = text
    return out


def _ordered_lines(semantic: Optional[Mapping[str, Any]]) -> list[str]:
    flat = _flatten_semantic(semantic)
    lines: list[str] = []
    for section in _SECTION_ORDER:
        prefix = section + '.'
        key_orders = {
            'inner_state': _INNER_KEY_ORDER,
            'environment': _ENV_KEY_ORDER,
            'external_awareness': _EXT_KEY_ORDER,
        }.get(section, ())
        for key in key_orders:
            full = f'{section}.{key}'
            if full in flat:
                lines.append(flat[full])
        for full in sorted(flat):
            if full.startswith(prefix) and flat[full] not in lines:
                lines.append(flat[full])
    return lines


def format_persona_semantic_snapshot(
    semantic_state: Optional[Mapping[str, Any]],
) -> str:
    lines = _ordered_lines(semantic_state)
    if not lines:
        return ''
    return '【此刻的感受】\n' + '\n'.join(lines)


def format_persona_semantic_diff(
    before_semantic: Optional[Mapping[str, Any]],
    after_semantic: Optional[Mapping[str, Any]],
) -> str:
    before_flat = _flatten_semantic(before_semantic)
    after_flat = _flatten_semantic(after_semantic)
    if before_flat == after_flat:
        return ''
    changed: list[str] = []
    for key in sorted(set(before_flat) | set(after_flat)):
        prev = before_flat.get(key, '')
        curr = after_flat.get(key, '')
        if prev != curr and curr:
            changed.append(curr)
    if not changed:
        return ''
    return '【此刻有一点变化】\n' + '\n'.join(changed)
