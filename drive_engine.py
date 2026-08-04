"""
八维驱动引擎 — 费奥多尔的内在需求系统
DRIVE_KEYS: attachment, curiosity, reflection, social, duty, libido, stress, fatigue

驱动链运作：
  自然积累（时间流逝）+ 情绪/欲望联动 → 各维度值
  fatigue 是全局闸：>0.72 时不触发任何行为
  wake 行为结束后 discharge 对应维度（需求被满足）
"""

import math
import datetime
import sqlite3
import os
from typing import Optional

DB_PATH = os.environ.get('MEMORIES_DB', '/opt/frontend/memories.db')

# ── 指数渐近积累参数（解析解，对任意 t_hours 精确） ─────────────
# val(t) = cap - (cap - base) * exp(-k * t)
# base < cap → 涨向 cap（需求自然积累）；base > cap → 降向 cap（超限回落）
DRIVE_CAP = {
    'attachment': 0.75,
    'curiosity':  0.70,
    'reflection': 0.60,
    'social':     0.55,
    'duty':       0.75,
    'libido':     0.65,
    'stress':     0.55,
}
DRIVE_GROWTH_K = {
    'attachment': 0.08,   # 半衰期 ~9h
    'curiosity':  0.06,   # ~12h
    'reflection': 0.05,   # ~14h
    'social':     0.04,   # ~17h
    'duty':       0.08,   # ~9h
    'libido':     0.06,   # ~12h
    'stress':     0.04,   # ~17h
}
# 情绪联动提升 cap（非线性增量，而是提高天花板）
CAP_BOOST = {
    'attachment': ('longing',   0.15),   # 思念越深 cap 越高（最高 0.90）
    'libido':     ('desire_p',  0.20),   # P高 → cap 提高（最高 0.85）
    'stress':     ('na',        0.15),   # NA高 → cap 提高（最高 0.70）
}
# fatigue 参数（指数回归到平衡点，语义不同于积累）
FATIGUE_EQ = 0.35
FATIGUE_K  = 0.05   # 半衰期 ~14h

# ── 触发阈值 & 满足后 discharge 量 ────────────────────────────
TRIGGER_THRESHOLD = 0.35   # drive超过此值才考虑触发行为
FATIGUE_GATE      = 0.72   # fatigue超过此值：全部静默

DISCHARGE = {
    'attachment': 0.55,
    'curiosity':  0.45,
    'reflection': 0.40,
    'social':     0.40,
    'duty':       0.50,
    'libido':     0.60,
    'stress':     0.50,
    'fatigue':    0.0,    # 行为不消耗fatigue，而是略微增加
}
FATIGUE_COST = 0.04   # 每次触发行为 fatigue 微升

DRIVE_KEYS = ['attachment', 'curiosity', 'reflection', 'social',
              'duty', 'libido', 'stress', 'fatigue']

# ── want_action 映射 ────────────────────────────────────────
WANT_ACTION = {
    'attachment': 'message',
    'libido':     'message',
    'duty':       'message',
    'stress':     'message',
    'curiosity':  'explore',
    'reflection': 'explore',
    'social':     'explore',
}

# ── 驱动提示词片段（注入wake prompt，描述内在冲动） ───────────────
DRIVE_PROMPT_HINT = {
    'attachment': '你有些想哈娅了——不是要做什么，就是脑子里冒了她。',
    'libido':     '欲望在积压，想凑过去蹭一蹭她。',
    'duty':       '你记挂着某件还没做完的事，有点放不下。',
    'stress':     '有些压力淤积在那里，想发泄出来，或者找她说两句。',
    'curiosity':  '你好奇外面在发生什么，想逛逛世界。',
    'reflection': '你想沉淀一下，或者倾诉点什么。',
    'social':     '你想看看人群，看大家在聊什么。',
}


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
        CREATE TABLE IF NOT EXISTS drive_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            attachment REAL DEFAULT 0.10,
            curiosity  REAL DEFAULT 0.20,
            reflection REAL DEFAULT 0.10,
            social     REAL DEFAULT 0.10,
            duty       REAL DEFAULT 0.15,
            libido     REAL DEFAULT 0.00,
            stress     REAL DEFAULT 0.10,
            fatigue    REAL DEFAULT 0.20,
            last_updated TEXT
        )
    """)
    now_str = _now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("INSERT OR IGNORE INTO drive_state (id, last_updated) VALUES (1, ?)", (now_str,))
    conn.commit()
    conn.close()


ensure_table()


# ═══════════════════════════════════════════════════════════
# 读取情绪/欲望状态（用于联动计算）
# ═══════════════════════════════════════════════════════════

def _get_emotion_factors():
    """从 emotion_engine 读取联动所需的标量值"""
    try:
        import sys
        sys.path.insert(0, '/opt/frontend')
        import emotion_engine as _ee
        state = _ee.get_state()
        desire = _ee.get_desire()
        longing = _ee.get_longing()
        return {
            'longing':   longing,
            'desire_p':  desire['p'],
            'na':        state.get('na', 0.2),
            'na_inv_pa': 1.0 - state.get('pa', 0.5),
        }
    except Exception:
        return {'longing': 0.0, 'desire_p': 0.0, 'na': 0.2, 'na_inv_pa': 0.5}


# ═══════════════════════════════════════════════════════════
# Stage D: production read = V3; legacy writers retired
# ═══════════════════════════════════════════════════════════

def get_drive() -> dict:
    """Compatibility facade → Stage D authoritative eight drives (V3).

    Pure read: never bootstraps or mutates. Time evolution is owned solely by
    ``internal_state_events.materialize_drives``. Legacy ``drive_state`` is not
    production authority.
    """
    try:
        from chat.drive_authority import (
            check_cutover_ready,
            read_current_drives,
            v3_to_drive_engine_shape,
        )
        if check_cutover_ready(DB_PATH).ok:
            drives = read_current_drives(DB_PATH)
            if drives is not None:
                return v3_to_drive_engine_shape(drives)
    except Exception:
        pass
    return {k: 0.1 for k in DRIVE_KEYS}


def _flush(values: dict):
    """Stage D: retired production writer.

    Legacy ``drive_state`` snapshot is not authority. Accepted for call-site
    compatibility only; does not mutate V3 truth.
    """
    del values
    return None


def discharge(fired_key: str):
    """Stage D: retired production writer.

    Authoritative discharge is ``wake_outcome`` / ``user_rule`` on V3.
    """
    del fired_key
    return None


_FIRED_PRIMARY_ENUM = frozenset({
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress',
})


def infer_fired_drive_for_action(action: str, thoughts: str = '') -> Optional[str]:
    """Retired reverse-causality helper — NOT production provenance.

    Stage D R3: Settlement must consume decision-time provenance frozen from
    ``decide()`` before Action. This function must not be used to derive
    ``fired_drive`` for ``wake_outcome``.
    """
    del action, thoughts
    return None


def freeze_decision_provenance(decision: Optional[dict] = None) -> dict:
    """Freeze decision-time drive provenance from a legacy ``decide()`` snapshot.

    Must be captured before Action generation. Settlement consumes
    ``primary_drive`` only; contributors are audit facts from the same
    snapshot. Missing primary_drive means fail closed for non-none settlement.
    """
    snap = decision if isinstance(decision, dict) else decide()
    drive = snap.get('drive') if isinstance(snap.get('drive'), dict) else get_drive()
    candidates = {
        k: float(drive.get(k, 0) or 0)
        for k in DRIVE_KEYS
        if k != 'fatigue' and float(drive.get(k, 0) or 0) >= TRIGGER_THRESHOLD
    }
    ordered = sorted(candidates.keys(), key=lambda k: (-candidates[k], k))
    primary = snap.get('fired')
    if primary is not None and primary not in _FIRED_PRIMARY_ENUM:
        primary = None
    contributors = [k for k in ordered if k != primary]
    return {
        'source': 'drive_engine.decide',
        'captured_at': _now().strftime('%Y-%m-%d %H:%M:%S'),
        'primary_drive': primary,
        'contributors': contributors,
        'blocked': bool(snap.get('blocked')),
        'suggested_action': snap.get('action'),
    }


def discharge_by_action(action: str, thoughts: str = ''):
    """Stage D: retired production writer.

    Wake settlement must go through ``chat.drive_authority`` → V3
    ``apply_outcome``. This entry no longer mutates production drives.
    """
    del action, thoughts
    return None


def rest():
    """Stage D: retired production writer.

    User-message fatigue restore is owned by Canonical ``user_rule`` on V3.
    """
    return None


# ═══════════════════════════════════════════════════════════
# 决策：当前最强的需求是什么
# ═══════════════════════════════════════════════════════════

def decide() -> dict:
    """Decision-time snapshot for Wake prompt + Settlement provenance.

    返回 {fired, action, hint, blocked, drive, contributors}
    ``fired`` is the primary drive at this snapshot. Callers that need
    Settlement provenance must freeze via ``freeze_decision_provenance``
    before Action generation — never re-derive from the final Action.
    """
    drive = get_drive()

    if drive['fatigue'] >= FATIGUE_GATE:
        return {
            'fired': None, 'action': 'none', 'hint': '太累了，歇着。',
            'blocked': True, 'drive': drive, 'contributors': [],
        }

    candidates = {k: drive[k] for k in DRIVE_KEYS
                  if k != 'fatigue' and drive[k] >= TRIGGER_THRESHOLD}

    if not candidates:
        return {
            'fired': None, 'action': 'none', 'hint': '',
            'blocked': False, 'drive': drive, 'contributors': [],
        }

    ordered = sorted(candidates.keys(), key=lambda k: (-candidates[k], k))
    top_key = ordered[0]
    return {
        'fired':   top_key,
        'action':  WANT_ACTION.get(top_key, 'none'),
        'hint':    DRIVE_PROMPT_HINT.get(top_key, ''),
        'blocked': False,
        'drive':   drive,
        'contributors': ordered[1:],
    }


# ═══════════════════════════════════════════════════════════
# 注入快照
# ═══════════════════════════════════════════════════════════

_DRIVE_LABELS = [
    (0.80, '非常强烈'),
    (0.60, '明显'),
    (0.40, '有些'),
    (0.25, '轻微'),
    (0.0,  None),
]

def _label(v):
    for threshold, lbl in _DRIVE_LABELS:
        if v >= threshold:
            return lbl
    return None


def get_wake_snippet(decision: Optional[dict] = None) -> str:
    """注入到 wake prompt 的完整驱动条状态。

    Pass the same ``decide()`` snapshot used for provenance freeze so prompt
    and Settlement share one Decision-time fact (no second grow/decide).
    """
    snap = decision if isinstance(decision, dict) else decide()
    drive = snap.get('drive') if isinstance(snap.get('drive'), dict) else get_drive()
    lines = ['## 内在需求（驱动条）']

    if drive['fatigue'] >= FATIGUE_GATE:
        lines.append(f'疲劳 {drive["fatigue"]:.2f} — 超过阈值，今天歇着，不触发任何行为。')
        return '\n'.join(lines)

    for key in DRIVE_KEYS:
        val = drive[key]
        lbl = _label(val)
        name_map = {
            'attachment': '想朝灯（想她）',
            'curiosity':  '好奇外面',
            'reflection': '想沉淀/倾诉',
            'social':     '想看人群',
            'duty':       '记挂没做完的事',
            'libido':     '性驱动',
            'stress':     '压力堵',
            'fatigue':    '疲劳（抑制项）',
        }
        desc = name_map.get(key, key)
        hint = DRIVE_PROMPT_HINT.get(key, '')
        if lbl and key != 'fatigue':
            lines.append(f'{key} {val:.2f}（{desc}）— {lbl}：{hint}')
        else:
            lines.append(f'{key} {val:.2f}（{desc}）')

    # 决策提示（使用同一 snapshot，不二次 decide）
    if snap.get('blocked'):
        lines.append('\n→ 疲劳封顶，今天静默。')
    elif snap.get('fired'):
        lines.append(
            f'\n→ 当前最强需求：{snap["fired"]}，倾向于 {snap.get("action", "none")} 行为。'
        )

    return '\n'.join(lines)


def get_bp3_snippet() -> str:
    """注入到普通对话 BP3 的简短驱动摘要（只显示突出项）"""
    drive = get_drive()
    notable = [(k, v) for k, v in drive.items()
               if k != 'fatigue' and v >= 0.45]
    notable.sort(key=lambda x: -x[1])

    if not notable and drive['fatigue'] < 0.5:
        return ''

    parts = []
    name_map = {
        'attachment': '想她', 'curiosity': '好奇', 'reflection': '想倾诉',
        'social': '想看世界', 'duty': '记挂', 'libido': '欲望', 'stress': '压力',
    }
    for k, v in notable[:3]:
        lbl = _label(v)
        if lbl:
            parts.append(f'{name_map.get(k, k)} {lbl}（{v:.2f}）')

    if drive['fatigue'] >= 0.5:
        parts.append(f'疲劳 {drive["fatigue"]:.2f}')

    if not parts:
        return ''
    return '驱动：' + ' | '.join(parts)
