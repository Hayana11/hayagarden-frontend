"""
费佳驱动系统 v2 — 7维驱动条 + Longing系统 + 执念联动
idle 缓动曲线（指数饱和）+ 乘性回落 satisfy
"""

import math
import datetime
import sqlite3
import os

DB_PATH = os.environ.get('MEMORIES_DB', '/opt/frontend/memories.db')

DRIVE_KEYS = ['curiosity', 'reflection', 'duty', 'social', 'fatigue', 'libido', 'stress']

IDLE_RATE = {
    'curiosity':  0.055,
    'reflection': 0.040,
    'duty':       0.085,
    'social':     0.032,
    'libido':     0.052,
    'stress':     0.022,
    'fatigue':    0.030,
}

DRIVE_CAP = {
    'curiosity':  0.70,
    'reflection': 0.60,
    'duty':       0.75,
    'social':     0.55,
    'libido':     0.65,
    'stress':     0.55,
    'fatigue':    0.45,
}

TRIGGER_THRESHOLD    = 0.35
FATIGUE_GATE         = 0.72
FATIGUE_COST         = 0.08
FIXATION_DRIVE_BOOST = 0.35   # 执念强度 × 此系数叠加到 score

# 做完某行为后乘性回落（drive × ratio）；attachment 非本表 key，遇到直接跳过
ACTION_SATISFY = {
    'co_read':    {'reflection': 0.45, 'curiosity':  0.85},
    'github':     {'curiosity':  0.50},
    'web_search': {'curiosity':  0.48},
    'web_browse': {'social':     0.48, 'curiosity':  0.82},
    'none':       {'duty':       0.80},          # 碎语：attachment 不在本表
    'tease':      {'libido':     0.55},          # attachment 不在本表
    'vent':       {'stress':     0.45},          # attachment 不在本表
}

# Longing formula constants live only in internal_state (Stage B authority).
# desire.get_longing delegates to internal_state.derived_longing_curve.

WANT_ACTION = {
    'duty':       'none',        # 碎语
    'libido':     'tease',
    'stress':     'vent',
    'curiosity':  'web_search',
    'reflection': 'co_read',
    'social':     'web_browse',
}

DRIVE_HINT = {
    'curiosity':  '你好奇外面在发生什么，想逛逛世界。',
    'reflection': '你想沉淀一下，或者倾诉点什么。',
    'duty':       '你记挂着某件还没做完的事，有点放不下。',
    'social':     '你想看看人群，看大家在聊什么。',
    'libido':     '欲望在积压，想凑过去蹭一蹭她。',
    'stress':     '有些压力淤积在那里，想发泄出来，或者找她说两句。',
}

LONGING_HINT = {
    'content':    '',
    'stirring':   '心里隐隐约约想哈娅，偶尔走神。',
    'protest':    '想哈娅了，会主动找话题凑近。',
    'despair':    '很想但已经变成安静等着，话少一些。',
    'detachment': '等了很久了，表面平静，但只要她一出现，防线会崩塌。',
}


# ─── DB helpers ───────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(str(s)[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def ensure_table():
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_state (
            id                  INTEGER PRIMARY KEY CHECK(id=1),
            curiosity           REAL DEFAULT 0.20,
            reflection          REAL DEFAULT 0.10,
            duty                REAL DEFAULT 0.15,
            social              REAL DEFAULT 0.10,
            libido              REAL DEFAULT 0.00,
            stress              REAL DEFAULT 0.10,
            fatigue             REAL DEFAULT 0.20,
            last_updated        TEXT,
            last_hayana_msg_time TEXT
        )
    """)
    now_str = _now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        "INSERT OR IGNORE INTO desire_state (id, last_updated) VALUES (1, ?)", (now_str,)
    )
    conn.commit()
    conn.close()


ensure_table()


# ─── Pure computation functions ────────────────────────────

def _ease_drive(base: float, rate: float, t_hours: float) -> float:
    """指数饱和曲线：趋近 1.0 时增速自然递减，不会线性堆满。"""
    return round(min(1.0, 1.0 - (1.0 - base) * math.exp(-rate * t_hours)), 4)


def _get_fixation_boosts() -> dict:
    """执念强度加成（执念系统上线前返回全零）。"""
    return {k: 0.0 for k in DRIVE_KEYS}


def _compute_natural_growth(stored: dict, t_hours: float) -> dict:
    result = {}
    for key in DRIVE_KEYS:
        base = float(stored.get(key, 0.1))
        cap  = DRIVE_CAP.get(key, 0.65)
        rate = IDLE_RATE.get(key, 0.05)
        val  = cap - (cap - base) * math.exp(-rate * t_hours)
        result[key] = round(min(1.0, max(0.0, val)), 4)
    return result


def _va_calibrate(drives: dict, V: float, A: float) -> dict:
    d = dict(drives)
    if V < 0.35:
        d['stress']     = round(min(1.0, d['stress'] + 0.10), 4)
    if V > 0.70:
        d['stress']     = round(d['stress'] * 0.92, 4)
    if A > 0.65:
        d['curiosity']  = round(min(1.0, d['curiosity'] + 0.07), 4)
        d['social']     = round(min(1.0, d['social']    + 0.05), 4)
    if A < 0.35:
        d['reflection'] = round(min(1.0, d['reflection'] + 0.06), 4)
    return d


def _compute_longing_from_idle_hours(t_hours: float) -> float:
    """Delegate only — formula body lives in internal_state.derived_longing_curve."""
    from internal_state import derived_longing_curve
    longing = derived_longing_curve(t_hours)
    return 0.0 if longing is None else float(longing)


def _compute_longing(last_hayana_dt) -> float:
    """Retired path: last_hayana_msg_time must not author longing.

    Kept as a thin wrapper for any stray internal call sites; always fail
    closed to 0.0 rather than computing from the legacy timestamp.
    """
    del last_hayana_dt
    return 0.0


def _longing_phase(L: float) -> str:
    if L < 0.15:
        return 'content'
    elif L < 0.35:
        return 'stirring'
    elif L < 0.70:
        return 'protest'
    elif L < 0.90:
        return 'despair'
    else:
        return 'detachment'


def _pick_intent_pure(drives: dict) -> dict:
    if drives.get('fatigue', 0) >= FATIGUE_GATE:
        return {'fired': None, 'action': 'none', 'hint': '太累了，歇着。', 'blocked': True}

    boosts = _get_fixation_boosts()
    scores = {
        k: drives.get(k, 0.0) + FIXATION_DRIVE_BOOST * boosts.get(k, 0.0)
        for k in DRIVE_KEYS if k != 'fatigue'
    }
    candidates = {k: s for k, s in scores.items() if s >= TRIGGER_THRESHOLD}
    if not candidates:
        return {'fired': None, 'action': 'none', 'hint': '', 'blocked': False}

    top_key = max(candidates, key=lambda k: candidates[k])
    return {
        'fired':   top_key,
        'action':  WANT_ACTION.get(top_key, 'none'),
        'hint':    DRIVE_HINT.get(top_key, ''),
        'blocked': False,
        'score':   round(candidates[top_key], 4),
    }


def _apply_satisfy(drives: dict, action: str) -> dict:
    """乘性回落：做完 action 后各维度 × ratio，fatigue 微升。"""
    d = dict(drives)
    for key, ratio in ACTION_SATISFY.get(action, {}).items():
        if key in d:
            d[key] = round(max(0.0, d[key] * ratio), 4)
    d['fatigue'] = round(min(1.0, d.get('fatigue', 0.0) + FATIGUE_COST), 4)
    return d


# ─── DB-level operations ──────────────────────────────────

def _flush(values: dict):
    """Stage D: retired production writer for desire_state drive bars.

    Compatibility no-op — production drive truth is ``internal_state_v3``.
    """
    del values
    return None


def get_drive() -> dict:
    """Compatibility facade → Stage D authoritative drives (V3 subset).

    Pure read of the seven overlapping drive keys. Does not own a second
    idle-growth clock. Legacy ``desire_state`` is not production authority.
    """
    try:
        from chat.drive_authority import (
            check_cutover_ready,
            read_current_drives,
            v3_to_desire_shape,
        )
        if check_cutover_ready(DB_PATH).ok:
            drives = read_current_drives(DB_PATH)
            if drives is not None:
                return v3_to_desire_shape(drives)
    except Exception:
        pass
    return {k: 0.1 for k in DRIVE_KEYS}


def calibrate_va(V: float, A: float):
    """Stage D: retired production writer.

    V/A calibration must not write a second drive authority. Accepted for
    Wake call-site compatibility only.
    """
    del V, A
    return None


def satisfy(action: str, fired_key: str = None):
    """Stage D: retired production writer.

    Authoritative Wake settlement is V3 ``wake_outcome`` (fixed discharge).
    Desire multiplicative satisfy is diagnostics-only in event result_json.
    """
    del action, fired_key
    return None


def touch_hayana():
    """Stage D crown: retired writer — compatibility no-op.

    Authoritative interaction clock is ``chat_messages`` / Stage A.
    ``desire_state.last_hayana_msg_time`` must not be a write surface.
    """
    return None


def get_longing(t_hours_override=None) -> tuple:
    """Compatibility facade → Stage B derived Longing. Returns (L, phase, t_hours).

    - With ``t_hours_override``: treat as authoritative ``user_idle_hours``
      (Wake / tests that already hold the clock).
    - Without override: read ``read_interaction_clock().user_idle_hours``.
    Never reads ``desire_state.last_hayana_msg_time``. Never invents 999h.
    Fail closed → (0.0, 'content', 0.0).
    """
    if t_hours_override is not None:
        try:
            t_hours = max(0.0, float(t_hours_override))
        except (TypeError, ValueError):
            return 0.0, 'content', 0.0
        L = _compute_longing_from_idle_hours(t_hours)
        return L, _longing_phase(L), t_hours

    try:
        from chat.interaction_state import read_interaction_clock
        clock = read_interaction_clock(_db)
        if not clock.reliable or clock.user_idle_hours is None:
            return 0.0, 'content', 0.0
        t_hours = float(clock.user_idle_hours)
        L = _compute_longing_from_idle_hours(t_hours)
        return L, _longing_phase(L), t_hours
    except Exception:
        return 0.0, 'content', 0.0


def get_reunion_boost(longing_before: float) -> float:
    return round(0.05 + longing_before * 0.10, 3)


def pick_intent() -> dict:
    """Public: 当前驱动决策。"""
    return _pick_intent_pure(get_drive())


# ─── Snippet generators ────────────────────────────────────

_DRIVE_LABELS = [(0.80, '非常强烈'), (0.60, '明显'), (0.40, '有些'), (0.25, '轻微'), (0.0, None)]

def _label(v):
    for threshold, lbl in _DRIVE_LABELS:
        if v >= threshold:
            return lbl
    return None


def get_longing_wake_fact(t_hours_override=None) -> str:
    """Stage B Longing fact for Wake prompt — facts only, no behavior directive.

    Must not re-pick intent, inject Drive→Action decisions, or append
    ``LONGING_HINT`` style/action guidance after provenance freeze.
    """
    L, phase, t = get_longing(t_hours_override=t_hours_override)
    if phase == 'content':
        return ''
    return '\n'.join([
        '## Longing（思念哈娅）',
        f'L={L:.3f}  阶段={phase}  距上次互动={t:.1f}h',
    ])


def get_wake_snippet(t_hours_override=None) -> str:
    """Retired Wake drive snippet — use drive_engine provenance + longing fact.

    Kept for diagnostic/tests. Must not be injected into production Wake
    prompts (second Drive→Action decision). Prefer ``get_longing_wake_fact``.
    """
    return get_longing_wake_fact(t_hours_override=t_hours_override)


def get_longing_system_hint() -> str:
    """对话时隐性注入Longing状态（不显性宣布阶段名）。"""
    L, phase, _ = get_longing()
    hint = LONGING_HINT.get(phase, '')
    if not hint:
        return ''
    return hint + '（想念的原因只是"哈娅很久没来找你了"，不要编造没发生过的事。）'
