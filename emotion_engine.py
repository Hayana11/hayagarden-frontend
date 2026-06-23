"""
情绪与欲望引擎 — 费奥多尔的情感状态管理
PANAS双轴 + Russell V/A坐标 + BOU均值回归 + 幂律思念曲线
Sternberg三角欲望系统 (I/P/C) + 时间衰减 + 关键词规则层
"""

import json
import math
import datetime
import threading
import urllib.request
import sqlite3
import re
import os

DB_PATH = os.environ.get('MEMORIES_DB', '/opt/frontend/memories.db')
_DS_KEY = None


def _get_deepseek_key():
    global _DS_KEY
    if _DS_KEY:
        return _DS_KEY
    try:
        with open('/opt/frontend/.env') as f:
            for line in f:
                line = line.strip()
                if line.startswith('DEEPSEEK_API_KEY='):
                    _DS_KEY = line.split('=', 1)[1].strip().strip('"\'')
                    return _DS_KEY
    except Exception:
        pass
    return os.environ.get('DEEPSEEK_API_KEY', '')


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table():
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emotion_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            pa REAL DEFAULT 0.5,
            na REAL DEFAULT 0.2,
            valence REAL DEFAULT 0.6,
            arousal REAL DEFAULT 0.3,
            mood_word TEXT DEFAULT '平静',
            longing REAL DEFAULT 0.0,
            last_interaction TEXT,
            updated_at TEXT,
            sternberg_i REAL DEFAULT 0.3,
            sternberg_p REAL DEFAULT 0.0,
            sternberg_c REAL DEFAULT 0.7,
            p_updated_at TEXT,
            i_updated_at TEXT
        )
    """)
    # 兼容旧表：补列
    for col, typ, default in [
        ('sternberg_i', 'REAL', '0.3'),
        ('sternberg_p', 'REAL', '0.0'),
        ('sternberg_c', 'REAL', '0.7'),
        ('p_updated_at', 'TEXT', 'NULL'),
        ('i_updated_at', 'TEXT', 'NULL'),
    ]:
        try:
            conn.execute(f'ALTER TABLE emotion_state ADD COLUMN {col} {typ} DEFAULT {default}')
        except Exception:
            pass
    conn.execute("INSERT OR IGNORE INTO emotion_state (id) VALUES (1)")
    now_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
        UPDATE emotion_state SET
            p_updated_at=COALESCE(p_updated_at, ?),
            i_updated_at=COALESCE(i_updated_at, ?)
        WHERE id=1
    """, (now_str, now_str))
    conn.commit()
    conn.close()


ensure_table()


# ═══════════════════════════════════════════════════════════
# 欲望词典 — 关键词分类规则层
# ═══════════════════════════════════════════════════════════

DESIRE_LEXICON = {
    # 肉体/性欲 → 激活P
    'physical': [
        '摸', '抱', '吻', '亲', '咬', '舔', '吸', '揉', '捏', '掐',
        '腰', '腿', '胸', '奶', '乳', '屁股', '肚子', '脖子', '唇', '嘴',
        '湿', '热', '流水', '进来', '里面', '插', '操', '干', '弄', '上',
        '爽', '淫', '骚', '硬', '涨', '痉挛', '颤', '抖', '喘', '呻吟',
        '发情', '欲望', '想要', '要你', '好色',
    ],
    # 亲密/依赖 → 激活I
    'affectionate': [
        '费佳', '费奥', '等我', '陪我', '想你', '找你', '回来',
        '撒娇', '好不好', '可以吗', '好吗', '嗯嗯', '嗯',
        '喜欢你', '爱你', '抱抱', '不要走', '别离开',
        '陪着我', '一起', '我们', '我的', '你的',
    ],
    # 脆弱/需要保护 → 强激活I（费奥多尔对这个有特别反应）
    'vulnerable': [
        '害怕', '哭', '哭了', '哭泣', '难受', '难过', '心疼',
        '痛', '生病', '不舒服', '头疼', '头痛', '胃疼', '胃',
        '凌晨', '睡不着', '失眠', '孤独', '一个人',
        '好累', '累了', '委屈', '委屈了', '撑不住',
    ],
    # 反抗/挑衅 → 微升P（刺激感），微降I
    'hostile': [
        '恨你', '讨厌你', '走开', '滚', '不理你',
        '坏蛋', '坏人', '烦死了', '讨厌', '气死我了',
        '不要你', '离我远点',
    ],
}

# 各类别对P/I的影响权重（per hit）
DESIRE_WEIGHTS = {
    'physical':     {'p': +0.18, 'i': +0.02},
    'affectionate': {'p': +0.03, 'i': +0.08},
    'vulnerable':   {'p': +0.01, 'i': +0.13},
    'hostile':      {'p': +0.07, 'i': -0.03},
}
# 单次消息各维度delta上限
DESIRE_CAP = {'p': 0.40, 'i': 0.22}


# ═══════════════════════════════════════════════════════════
# 时间衰减参数
# ═══════════════════════════════════════════════════════════

TAU_P = 6.0    # P半衰期：6小时（肉欲快消）
TAU_I = 96.0   # I半衰期：4天（亲密慢消）


def _now_str():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(str(s)[:19], '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def _decay(value, stored_at_str, tau_hours):
    """指数衰减：value × exp(-t/τ)"""
    if not stored_at_str:
        return value
    stored_at = _parse_dt(stored_at_str)
    if not stored_at:
        return value
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    t = max(0.0, (now - stored_at).total_seconds() / 3600)
    return max(0.0, value * math.exp(-t / tau_hours))


# ═══════════════════════════════════════════════════════════
# 读写状态
# ═══════════════════════════════════════════════════════════

def get_state() -> dict:
    conn = _db()
    row = conn.execute("SELECT * FROM emotion_state WHERE id=1").fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        'pa': 0.5, 'na': 0.2, 'valence': 0.6, 'arousal': 0.3,
        'mood_word': '平静', 'longing': 0.0,
        'sternberg_i': 0.3, 'sternberg_p': 0.0, 'sternberg_c': 0.7,
        'p_updated_at': None, 'i_updated_at': None,
        'last_interaction': None,
    }


def get_desire() -> dict:
    """返回实时衰减后的I/P/C值"""
    state = get_state()
    p = _decay(state.get('sternberg_p', 0.0), state.get('p_updated_at'), TAU_P)
    i = _decay(state.get('sternberg_i', 0.3), state.get('i_updated_at'), TAU_I)
    c = state.get('sternberg_c', 0.7)  # C几乎不衰减
    return {'p': round(p, 4), 'i': round(i, 4), 'c': round(c, 4)}


def touch_interaction():
    """记录本次交互时间"""
    conn = _db()
    conn.execute(
        "UPDATE emotion_state SET last_interaction=? WHERE id=1", (_now_str(),)
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
# 思念曲线（幂律累积）
# ═══════════════════════════════════════════════════════════

def get_longing() -> float:
    """L = 0.85 × (1 - (1 + t/8)^(-0.8))，t单位小时"""
    state = get_state()
    last_str = state.get('last_interaction') or ''
    if not last_str:
        return 0.0
    try:
        last = _parse_dt(last_str)
        if not last:
            return 0.0
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        t = max(0.0, (now - last).total_seconds() / 3600)
        L = 0.85 * (1 - (1 + t / 8) ** (-0.8))
        return round(min(L, 0.92), 3)
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════
# 规则层：关键词打分（同步，收到消息时立即跑）
# ═══════════════════════════════════════════════════════════

def rule_score_desire(user_msg: str) -> dict:
    """
    扫描用户消息关键词，返回 {p_delta, i_delta}
    同步执行，轻量，不调外部API
    """
    p_delta = 0.0
    i_delta = 0.0
    for category, words in DESIRE_LEXICON.items():
        hits = sum(1 for w in words if w in user_msg)
        if hits > 0:
            w_p = DESIRE_WEIGHTS[category]['p']
            w_i = DESIRE_WEIGHTS[category]['i']
            # 多命中递减（避免堆词轰炸）
            effective = math.log1p(hits)
            p_delta += w_p * effective
            i_delta += w_i * effective
    # 限幅
    p_delta = max(-DESIRE_CAP['p'], min(DESIRE_CAP['p'], p_delta))
    i_delta = max(-DESIRE_CAP['i'], min(DESIRE_CAP['i'], i_delta))
    return {'p_delta': round(p_delta, 4), 'i_delta': round(i_delta, 4)}


def apply_desire_delta(p_delta: float, i_delta: float, c_delta: float = 0.0):
    """
    将desire delta写入DB（先读取衰减后的值，再叠加delta）
    P和I的更新时间分别记录
    """
    state = get_state()
    now = _now_str()

    # 读当前衰减后的值
    p_now = _decay(state.get('sternberg_p', 0.0), state.get('p_updated_at'), TAU_P)
    i_now = _decay(state.get('sternberg_i', 0.3), state.get('i_updated_at'), TAU_I)
    c_now = state.get('sternberg_c', 0.7)

    new_p = max(0.0, min(1.0, p_now + p_delta))
    new_i = max(0.0, min(1.0, i_now + i_delta))
    new_c = max(0.0, min(1.0, c_now + c_delta))

    conn = _db()
    conn.execute("""
        UPDATE emotion_state SET
            sternberg_p=?, sternberg_i=?, sternberg_c=?,
            p_updated_at=?, i_updated_at=?
        WHERE id=1
    """, (round(new_p, 4), round(new_i, 4), round(new_c, 4), now, now))
    conn.commit()
    conn.close()


def apply_desire_delta_async(p_delta: float, i_delta: float):
    """非阻塞版本"""
    t = threading.Thread(
        target=apply_desire_delta,
        args=(p_delta, i_delta),
        daemon=True
    )
    t.start()


# ═══════════════════════════════════════════════════════════
# 渐变脑接口
# ═══════════════════════════════════════════════════════════

def _get_ombre_va():
    try:
        req = urllib.request.Request('http://127.0.0.1:8000/emotion_snapshot')
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            v = data.get('valence')
            a = data.get('arousal')
            if v is not None and a is not None:
                return float(v), float(a)
    except Exception:
        pass
    return None, None


# ═══════════════════════════════════════════════════════════
# DeepSeek评分（异步，回复后跑）
# ═══════════════════════════════════════════════════════════

def _deepseek_score(text: str) -> dict:
    """
    返回 {valence, arousal, mood_word, passion_delta, intimacy_delta}
    valence: [-1,1], arousal: [0,1]
    passion_delta/intimacy_delta: [-0.3, 0.3]
    """
    key = _get_deepseek_key()
    if not key:
        return {'valence': 0.0, 'arousal': 0.3, 'mood_word': '平静',
                'passion_delta': 0.0, 'intimacy_delta': 0.0}

    system = '\n'.join([
        '你是情绪与欲望分析器。分析以下对话中"assistant"（费奥多尔）的情感和欲望状态。',
        '只返回JSON，格式：',
        '{"valence": float, "arousal": float, "mood_word": "中文词",',
        ' "passion_delta": float, "intimacy_delta": float}',
        'valence: -1.0(极消极)到1.0(极积极)',
        'arousal: 0.0(极平静)到1.0(极激动)',
        'mood_word: 一个精准的中文情绪短语',
        'passion_delta: 对话让费奥多尔肉体欲望的变化量，-0.3到0.3，正值=激起欲望，负值=平静',
        'intimacy_delta: 对话让费奥多尔亲密感/情感连接的变化量，-0.2到0.2，正值=更亲近',
        '基于对话的实际内容和语气分析，不要凭空假设。',
    ])

    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': text[:2000]},
        ],
        'response_format': {'type': 'json_object'},
        'max_tokens': 100,
        'temperature': 0.2,
    }).encode()

    req = urllib.request.Request(
        'https://api.deepseek.com/chat/completions',
        data=body,
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            result = json.loads(resp.read())
            raw = result['choices'][0]['message']['content']
            data = json.loads(raw)
            return {
                'valence':       max(-1.0, min(1.0,  float(data.get('valence', 0.0)))),
                'arousal':       max(0.0,  min(1.0,  float(data.get('arousal', 0.3)))),
                'mood_word':     str(data.get('mood_word', '平静'))[:30],
                'passion_delta': max(-0.3, min(0.3,  float(data.get('passion_delta', 0.0)))),
                'intimacy_delta':max(-0.2, min(0.2,  float(data.get('intimacy_delta', 0.0)))),
            }
    except Exception:
        return {'valence': 0.0, 'arousal': 0.3, 'mood_word': '平静',
                'passion_delta': 0.0, 'intimacy_delta': 0.0}


# ═══════════════════════════════════════════════════════════
# BOU均值回归
# ═══════════════════════════════════════════════════════════

def _bou_revert(pa: float, na: float) -> tuple:
    PA_BASE, NA_BASE, RATE = 0.5, 0.2, 0.08
    return (pa + RATE * (PA_BASE - pa),
            na + RATE * (NA_BASE - na))


# ═══════════════════════════════════════════════════════════
# 核心：完整评分+更新（对话后异步跑）
# ═══════════════════════════════════════════════════════════

def score_and_update(conversation_excerpt: str):
    """
    对话后全量更新：情绪(PA/NA/V/A) + 欲望(P/I)
    DeepSeek × 70% + 渐变脑 × 30%
    """
    scored = _deepseek_score(conversation_excerpt)

    # ── 情绪层 ──────────────────────────────────────
    ds_v = (scored['valence'] + 1) / 2   # [-1,1] → [0,1]
    ds_a = scored['arousal']
    mood_word = scored['mood_word']

    ob_v, ob_a = _get_ombre_va()
    if ob_v is not None:
        final_v = 0.7 * ds_v + 0.3 * ob_v
        final_a = 0.7 * ds_a + 0.3 * ob_a
    else:
        final_v = ds_v
        final_a = ds_a

    state = get_state()
    old_pa, old_na = state['pa'], state['na']
    new_pa = max(0.0, min(1.0, 0.75 * old_pa + 0.25 * final_v))
    na_signal = final_a * (1 - final_v) * 0.5 + 0.05
    new_na = max(0.0, min(1.0, 0.75 * old_na + 0.25 * na_signal))
    new_pa, new_na = _bou_revert(new_pa, new_na)

    # ── 欲望层 ──────────────────────────────────────
    p_delta_ds = scored['passion_delta']
    i_delta_ds = scored['intimacy_delta']

    # 读当前衰减后的P/I
    p_now = _decay(state.get('sternberg_p', 0.0), state.get('p_updated_at'), TAU_P)
    i_now = _decay(state.get('sternberg_i', 0.3), state.get('i_updated_at'), TAU_I)
    c_now = state.get('sternberg_c', 0.7)

    new_p = max(0.0, min(1.0, p_now + p_delta_ds))
    new_i = max(0.0, min(1.0, i_now + i_delta_ds))

    # ── 写入 ────────────────────────────────────────
    longing = get_longing()
    now = _now_str()
    conn = _db()
    conn.execute("""
        UPDATE emotion_state SET
            pa=?, na=?, valence=?, arousal=?, mood_word=?, longing=?,
            sternberg_p=?, sternberg_i=?, sternberg_c=?,
            p_updated_at=?, i_updated_at=?,
            last_interaction=?, updated_at=?
        WHERE id=1
    """, (
        round(new_pa, 4), round(new_na, 4),
        round(final_v, 4), round(final_a, 4),
        mood_word, round(longing, 4),
        round(new_p, 4), round(new_i, 4), round(c_now, 4),
        now, now, now, now,
    ))
    conn.commit()
    conn.close()


def score_async(conversation_excerpt: str):
    t = threading.Thread(
        target=score_and_update,
        args=(conversation_excerpt,),
        daemon=True
    )
    t.start()


# ═══════════════════════════════════════════════════════════
# BP3注入快照
# ═══════════════════════════════════════════════════════════

# 欲望状态描述映射
_P_LABELS = [
    (0.75, '非常强烈'),
    (0.55, '明显'),
    (0.38, '有些'),
    (0.20, '轻微'),
    (0.0,  None),
]
_I_LABELS = [
    (0.80, '极度亲近'),
    (0.65, '相当亲密'),
    (0.45, '有些亲密'),
    (0.0,  None),
]


def _label(value, table):
    for threshold, label in table:
        if value >= threshold:
            return label
    return None


def get_bp3_snippet() -> str:
    state = get_state()
    longing = get_longing()
    desire = get_desire()

    pa = state['pa']
    na = state['na']
    v  = state['valence']
    a  = state['arousal']
    mw = state.get('mood_word') or '平静'

    p_val = desire['p']
    i_val = desire['i']
    c_val = desire['c']

    if pa > na + 0.18:
        dominant = '正向主导'
    elif na > pa + 0.18:
        dominant = '负向主导'
    else:
        dominant = '混合张力'

    lines = [
        '## 此刻的情绪与欲望',
        f'情绪：V{v:.2f}/A{a:.2f} — {mw}',
        f'PA {pa:.2f} | NA {na:.2f} | {dominant}',
    ]

    # 欲望三角
    desire_parts = [f'P{p_val:.2f}', f'I{i_val:.2f}', f'C{c_val:.2f}']
    desire_line = '欲望：' + ' | '.join(desire_parts)

    # P描述
    p_label = _label(p_val, _P_LABELS)
    if p_label:
        desire_line += f'（激情{p_label}）'

    # I描述
    i_label = _label(i_val, _I_LABELS)
    if i_label:
        desire_line += f'（{i_label}）'

    lines.append(desire_line)

    # 思念
    if longing > 0.28:
        if longing < 0.5:
            ld = '开始想她了'
        elif longing < 0.72:
            ld = '有些思念'
        else:
            ld = '相当思念，有些不安'
        lines.append(f'思念 {longing:.0%} — {ld}')

    return '\n'.join(lines)
