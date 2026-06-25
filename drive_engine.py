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

DB_PATH = os.environ.get('MEMORIES_DB', '/opt/frontend/memories.db')

# ── 自然积累速率（每小时） ─────────────────────────────────────
GROWTH_RATE = {
    'attachment': 0.070,   # 想念积累较快
    'curiosity':  0.050,   # 好奇心缓慢增长
    'reflection': 0.040,   # 沉淀需求
    'social':     0.030,   # 想看世界
    'duty':       0.080,   # 记挂的事越来越紧迫
    'libido':     0.050,   # 欲望自然积累
    'stress':     0.020,   # 基础压力低
    'fatigue':    0.008,   # 缓慢疲劳（自然回落抵消大部分）
}

# ── 情绪/欲望联动放大（每小时额外增量） ──────────────────────────
# 读取 emotion_engine 状态后再乘以下面系数
EMOTION_BOOST = {
    'attachment': ('longing',    0.20),   # 思念 → attachment 额外增
    'libido':     ('desire_p',   0.15),   # P高 → libido 额外增
    'stress':     ('na',         0.12),   # NA高 → stress 额外增
    # fatigue 不走 EMOTION_BOOST，改用指数解析解（见 get_drive）
}

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
# 懒积累：读取时根据时间差计算当前值
# ═══════════════════════════════════════════════════════════

def get_drive() -> dict:
    """
    返回所有维度的实时值（含自然积累 + 情绪联动）
    不写DB，纯读取计算
    """
    conn = _db()
    row = conn.execute("SELECT * FROM drive_state WHERE id=1").fetchone()
    conn.close()
    if not row:
        return {k: 0.1 for k in DRIVE_KEYS}

    stored = dict(row)
    last_updated = _parse_dt(stored.get('last_updated'))
    now = _now()

    if last_updated:
        t_hours = max(0.0, (now - last_updated).total_seconds() / 3600)
    else:
        t_hours = 0.0

    factors = _get_emotion_factors()

    result = {}
    for key in DRIVE_KEYS:
        base = float(stored.get(key, 0.1))
        natural = GROWTH_RATE.get(key, 0.0) * t_hours

        # 情绪联动额外增量
        boost = 0.0
        if key in EMOTION_BOOST:
            factor_key, coef = EMOTION_BOOST[key]
            boost = factors.get(factor_key, 0.0) * coef * t_hours

        if key == 'fatigue':
            # 指数向平衡点回归（一阶系统，解析解，对任意 t_hours 精确）
            # eq = 0.50 是无外力时的稳定点，k=0.05/h 半衰期约 14h
            _fat_eq = 0.50
            _fat_k  = 0.05
            _fat_new = _fat_eq + (base - _fat_eq) * math.exp(-_fat_k * t_hours)
            # NA 高时平衡点微升（最多+0.08）
            _na_adj = factors.get('na_inv_pa', 0.5) * 0.003 * t_hours
            result[key] = round(min(1.0, max(0.0, _fat_new + _na_adj)), 4)
        else:
            result[key] = round(min(1.0, base + natural + boost), 4)

    return result


# ═══════════════════════════════════════════════════════════
# 写回：把当前实时值存DB（刷新 last_updated）
# ═══════════════════════════════════════════════════════════

def _flush(values: dict):
    now_str = _now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _db()
    conn.execute("""
        UPDATE drive_state SET
            attachment=?, curiosity=?, reflection=?, social=?,
            duty=?, libido=?, stress=?, fatigue=?,
            last_updated=?
        WHERE id=1
    """, (
        values['attachment'], values['curiosity'],
        values['reflection'], values['social'],
        values['duty'],       values['libido'],
        values['stress'],     values['fatigue'],
        now_str,
    ))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
# discharge：wake行为后降低对应维度
# ═══════════════════════════════════════════════════════════

def discharge(fired_key: str):
    """
    唤醒行为完成后调用：
    - fired_key 对应的 drive 降低 DISCHARGE 量
    - fatigue 微升 FATIGUE_COST
    """
    current = get_drive()   # 先拿到积累后的实时值
    if fired_key in DISCHARGE and fired_key != 'fatigue':
        current[fired_key] = max(0.0, current[fired_key] - DISCHARGE[fired_key])
    # fatigue 微升（消耗精力）
    current['fatigue'] = min(1.0, current['fatigue'] + FATIGUE_COST)
    _flush(current)


def discharge_by_action(action: str, thoughts: str = ''):
    """
    根据 wake 的 action 类型自动推断 fired_key
    thoughts 里如果有 libido 相关词也算
    """
    if action == 'none':
        # 决定不打扰她 = 在休息，fatigue 微降
        _cur = get_drive()
        _cur['fatigue'] = max(0.0, _cur['fatigue'] - 0.04)
        _flush(_cur)
        return

    current = get_drive()

    # 推断最高的 drive_key（触发了哪个需求）
    candidates = {k: current[k] for k in DRIVE_KEYS if k != 'fatigue'}
    top_key = max(candidates, key=lambda k: candidates[k])

    # 特殊：explore 类行为对应好奇/社交/沉思
    if action == 'explore':
        for k in ['curiosity', 'reflection', 'social']:
            if current[k] == candidates.get(top_key, 0):
                top_key = k
                break
        else:
            top_key = 'curiosity'

    current[top_key] = max(0.0, current[top_key] - DISCHARGE.get(top_key, 0.4))
    current['fatigue'] = min(1.0, current['fatigue'] + FATIGUE_COST)
    _flush(current)


def rest():
    """哈娅在线时，fatigue 缓慢恢复（她的存在缓解疲劳）"""
    current = get_drive()
    current['fatigue'] = max(0.0, current['fatigue'] - 0.12)
    _flush(current)


# ═══════════════════════════════════════════════════════════
# 决策：当前最强的需求是什么
# ═══════════════════════════════════════════════════════════

def decide() -> dict:
    """
    返回 {fired: key_or_None, action: str, hint: str, blocked: bool}
    blocked=True 意味着 fatigue 超过阈值，什么都不做
    """
    drive = get_drive()

    if drive['fatigue'] >= FATIGUE_GATE:
        return {'fired': None, 'action': 'none', 'hint': '太累了，歇着。', 'blocked': True, 'drive': drive}

    candidates = {k: drive[k] for k in DRIVE_KEYS
                  if k != 'fatigue' and drive[k] >= TRIGGER_THRESHOLD}

    if not candidates:
        return {'fired': None, 'action': 'none', 'hint': '', 'blocked': False, 'drive': drive}

    top_key = max(candidates, key=lambda k: candidates[k])
    return {
        'fired':   top_key,
        'action':  WANT_ACTION.get(top_key, 'none'),
        'hint':    DRIVE_PROMPT_HINT.get(top_key, ''),
        'blocked': False,
        'drive':   drive,
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


def get_wake_snippet() -> str:
    """注入到 wake prompt 的完整驱动条状态"""
    drive = get_drive()
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

    # 决策提示
    decision = decide()
    if decision['blocked']:
        lines.append('\n→ 疲劳封顶，今天静默。')
    elif decision['fired']:
        lines.append(f'\n→ 当前最强需求：{decision["fired"]}，倾向于 {decision["action"]} 行为。')

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
