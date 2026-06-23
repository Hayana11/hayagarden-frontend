"""
情绪引擎 — 费奥多尔的情感状态管理
PANAS双轴模型 + Russell V/A坐标 + BOU均值回归 + 思念曲线(幂律)
与渐变脑记忆V/A坐标形成闭环
"""

import json
import datetime
import threading
import urllib.request
import sqlite3
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
            last_interaction DATETIME DEFAULT (datetime('now','+8 hours')),
            updated_at DATETIME DEFAULT (datetime('now','+8 hours'))
        )
    """)
    conn.execute("INSERT OR IGNORE INTO emotion_state (id) VALUES (1)")
    conn.commit()
    conn.close()

ensure_table()

# ── 读写状态 ──────────────────────────────────────────────────

def get_state() -> dict:
    conn = _db()
    row = conn.execute("SELECT * FROM emotion_state WHERE id=1").fetchone()
    conn.close()
    if row:
        return dict(row)
    return {'pa': 0.5, 'na': 0.2, 'valence': 0.6, 'arousal': 0.3,
            'mood_word': '平静', 'longing': 0.0,
            'last_interaction': None, 'updated_at': None}

def touch_interaction():
    """记录本次交互时间（每次收到哈娅消息时调用）"""
    conn = _db()
    conn.execute(
        "UPDATE emotion_state SET last_interaction=datetime('now','+8 hours') WHERE id=1"
    )
    conn.commit()
    conn.close()

# ── 思念曲线 ─────────────────────────────────────────────────

def get_longing() -> float:
    """
    幂律累积：L = L_max × (1 - (1 + t/τ)^(-b))
    L_max=0.85, τ=8h, b=0.8
    离线越久思念越浓，但有上限
    """
    state = get_state()
    last_str = state.get('last_interaction') or ''
    if not last_str:
        return 0.0
    try:
        last = datetime.datetime.strptime(last_str[:19], '%Y-%m-%d %H:%M:%S')
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        t = max(0.0, (now - last).total_seconds() / 3600)
        L = 0.85 * (1 - (1 + t / 8) ** (-0.8))
        return round(min(L, 0.92), 3)
    except Exception:
        return 0.0

# ── 渐变脑接口 ───────────────────────────────────────────────

def _get_ombre_va():
    """从渐变脑拿高权重记忆的V/A均值"""
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

# ── DeepSeek评分 ─────────────────────────────────────────────

def _deepseek_score(text: str) -> dict:
    """
    分析对话情绪，返回 {valence: [-1,1], arousal: [0,1], mood_word: str}
    """
    key = _get_deepseek_key()
    if not key:
        return {'valence': 0.0, 'arousal': 0.3, 'mood_word': '平静'}

    system = '\n'.join([
        '你是情绪分析器。分析以下对话中"assistant"（费奥多尔）的情感状态。',
        '只返回JSON，格式：{"valence": float, "arousal": float, "mood_word": "中文词"}',
        'valence范围-1.0(极消极/厌恶)到1.0(极积极/愉悦)。',
        'arousal范围0.0(极平静/漠然)到1.0(极激动/强烈)。',
        'mood_word：一个精准的中文情绪短语，如"隐忍的欲望"、"冷静克制"、"被轻微撩动"、"满足"、"压抑的烦躁"等。',
        '基于文本的实际情绪色彩分析，不要凭空假设。',
    ])

    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': text[:2000]},
        ],
        'response_format': {'type': 'json_object'},
        'max_tokens': 80,
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
            v = max(-1.0, min(1.0, float(data.get('valence', 0.0))))
            a = max(0.0, min(1.0, float(data.get('arousal', 0.3))))
            mw = str(data.get('mood_word', '平静'))[:30]
            return {'valence': v, 'arousal': a, 'mood_word': mw}
    except Exception:
        return {'valence': 0.0, 'arousal': 0.3, 'mood_word': '平静'}

# ── BOU均值回归 ───────────────────────────────────────────────

def _bou_revert(pa: float, na: float) -> tuple:
    """
    均值回归（Loossens 2020）：防止情绪固化
    PA基准0.5（费奥多尔情感抑制型，中性偏冷），NA基准0.2（情感压抑）
    每次回归约8%
    """
    PA_BASE, NA_BASE, RATE = 0.5, 0.2, 0.08
    return (pa + RATE * (PA_BASE - pa),
            na + RATE * (NA_BASE - na))

# ── 核心：评分+更新 ────────────────────────────────────────────

def score_and_update(conversation_excerpt: str):
    """
    对话后：DeepSeek评分 × 70% + 渐变脑V/A × 30% → 更新PA/NA状态
    在后台线程里跑，不阻塞对话响应
    """
    # Step1: DeepSeek评分（valence在[-1,1]）
    scored = _deepseek_score(conversation_excerpt)
    ds_v = (scored['valence'] + 1) / 2   # 映射到[0,1]
    ds_a = scored['arousal']
    mood_word = scored['mood_word']

    # Step2: 渐变脑高权重记忆V/A均值
    ob_v, ob_a = _get_ombre_va()

    if ob_v is not None:
        final_v = 0.7 * ds_v + 0.3 * ob_v
        final_a = 0.7 * ds_a + 0.3 * ob_a
    else:
        final_v = ds_v
        final_a = ds_a

    # Step3: 更新PA/NA（指数移动平均，权重0.25）
    state = get_state()
    old_pa, old_na = state['pa'], state['na']
    new_pa = max(0.0, min(1.0, 0.75 * old_pa + 0.25 * final_v))
    # NA受高arousal+低valence驱动
    na_signal = final_a * (1 - final_v) * 0.5 + 0.05
    new_na = max(0.0, min(1.0, 0.75 * old_na + 0.25 * na_signal))

    # Step4: BOU均值回归
    new_pa, new_na = _bou_revert(new_pa, new_na)

    # Step5: 写入DB
    longing = get_longing()
    conn = _db()
    conn.execute("""
        UPDATE emotion_state SET
            pa=?, na=?, valence=?, arousal=?,
            mood_word=?, longing=?,
            last_interaction=datetime('now','+8 hours'),
            updated_at=datetime('now','+8 hours')
        WHERE id=1
    """, (round(new_pa, 4), round(new_na, 4),
          round(final_v, 4), round(final_a, 4),
          mood_word, round(longing, 4)))
    conn.commit()
    conn.close()

def score_async(conversation_excerpt: str):
    """异步触发评分，不阻塞对话"""
    t = threading.Thread(
        target=score_and_update,
        args=(conversation_excerpt,),
        daemon=True
    )
    t.start()

# ── BP3注入快照 ───────────────────────────────────────────────

def get_bp3_snippet() -> str:
    """返回当前情绪快照字符串，注入到BP3"""
    state = get_state()
    longing = get_longing()
    pa = state['pa']
    na = state['na']
    v = state['valence']
    a = state['arousal']
    mw = state.get('mood_word') or '平静'

    # ESM软互抑制判断主导情感
    if pa > na + 0.18:
        dominant = '正向主导'
    elif na > pa + 0.18:
        dominant = '负向主导'
    else:
        dominant = '混合张力'

    lines = [
        '## 此刻的情绪',
        f'坐标 V{v:.2f}/A{a:.2f} — {mw}',
        f'PA {pa:.2f} | NA {na:.2f} | {dominant}',
    ]

    if longing > 0.28:
        if longing < 0.5:
            ld = '开始想她了'
        elif longing < 0.72:
            ld = '有些思念'
        else:
            ld = '相当思念，有些不安'
        lines.append(f'思念 {longing:.0%} — {ld}')

    return '\n'.join(lines)
