#!/usr/bin/env python3.11
"""
梦生成器 v2 — 三层记忆整合
Layer 1: 采集碎片 (ombre-brain buckets + dream_events + diary)
Layer 2: 融合相关 (按domain聚类，去重)
Layer 3: 推断基调 (情感坐标 → 梦的温度) → 调用 /wake 生成诗意梦境
"""
import sqlite3, datetime, random, os, sys, json
import frontmatter as fm
import urllib.request

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')

DB_PATH    = '/opt/frontend/memories.db'
BRAIN_DIR  = '/opt/ombre-brain/buckets/dynamic'
GATEWAY    = 'http://localhost:5051'
LOG_FILE   = '/var/log/dream_generator.log'

def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

def _log(msg):
    ts = _now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line)
    except Exception:
        pass

def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

# ── Layer 1: 采集碎片 ──────────────────────────────────────────
def _gather_brain_fragments(days=3):
    """从 ombre-brain dynamic 桶里取最近 days 天的记忆碎片"""
    cutoff = _now() - datetime.timedelta(days=days)
    fragments = []
    if not os.path.isdir(BRAIN_DIR):
        return fragments
    for domain_dir in os.listdir(BRAIN_DIR):
        domain_path = os.path.join(BRAIN_DIR, domain_dir)
        if not os.path.isdir(domain_path):
            continue
        for fname in os.listdir(domain_path):
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(domain_path, fname)
            try:
                post = fm.load(fpath)
                meta = post.metadata
                last_active_str = meta.get('last_active', meta.get('created', ''))
                last_active = datetime.datetime.fromisoformat(str(last_active_str))
                if last_active < cutoff:
                    continue
                fragments.append({
                    'domain': meta.get('domain', [domain_dir]),
                    'valence': float(meta.get('valence', 0.5)),
                    'arousal': float(meta.get('arousal', 0.3)),
                    'importance': int(meta.get('importance', 5)),
                    'name': meta.get('name', fname),
                    'content': post.content.strip()[:120],
                    'last_active': last_active,
                })
            except Exception:
                continue
    # 按重要度+recency排序
    fragments.sort(key=lambda x: (x['importance'], x['last_active']), reverse=True)
    return fragments[:6]

def _gather_db_fragments():
    """从 dream_events、wake_log、posts 取当天碎片"""
    conn = _db()
    today = _now().date()
    result = {'events': [], 'thoughts': [], 'diary': None}

    events = conn.execute(
        "SELECT value FROM dream_events WHERE date(created_at) >= date('now','-2 days') ORDER BY id DESC LIMIT 4"
    ).fetchall()
    result['events'] = [e['value'] for e in events]

    thoughts = conn.execute(
        "SELECT thoughts FROM wake_log WHERE thoughts != '' ORDER BY id DESC LIMIT 2"
    ).fetchall()
    result['thoughts'] = [t['thoughts'][:80] for t in thoughts if t['thoughts']]

    diary = conn.execute(
        "SELECT content FROM posts WHERE type='DIARY' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if diary:
        result['diary'] = diary['content'][:150]

    conn.close()
    return result

# ── Layer 2: 融合相关 ──────────────────────────────────────────
def _merge_by_domain(brain_frags):
    """按 domain 聚类，把相同主题的碎片合并成一个意象"""
    domain_map = {}
    for frag in brain_frags:
        domains = frag['domain'] if isinstance(frag['domain'], list) else [frag['domain']]
        primary = domains[0] if domains else '未知'
        if primary not in domain_map:
            domain_map[primary] = {'contents': [], 'valence': [], 'arousal': []}
        domain_map[primary]['contents'].append(frag['name'])
        domain_map[primary]['valence'].append(frag['valence'])
        domain_map[primary]['arousal'].append(frag['arousal'])

    merged = []
    for domain, data in domain_map.items():
        avg_v = sum(data['valence']) / len(data['valence'])
        avg_a = sum(data['arousal']) / len(data['arousal'])
        merged.append({
            'domain': domain,
            'names': data['contents'],
            'valence': avg_v,
            'arousal': avg_a,
        })
    return merged

# ── Layer 3: 推断基调 ──────────────────────────────────────────
def _infer_dream_tone(merged, db_frags):
    """
    综合所有碎片的情感坐标，推断梦的基调
    返回 (tone, primer_text)
    """
    all_v = [m['valence'] for m in merged]
    all_a = [m['arousal'] for m in merged]
    if not all_v:
        return 'calm', ''

    avg_v = sum(all_v) / len(all_v)
    avg_a = sum(all_a) / len(all_a)

    # Russell 情感平面 → 基调
    if avg_v >= 0.6 and avg_a >= 0.6:
        tone = 'vivid'      # 高效价高唤醒 → 鲜活激动
    elif avg_v >= 0.6 and avg_a < 0.6:
        tone = 'warm'       # 高效价低唤醒 → 温柔平静
    elif avg_v < 0.4 and avg_a >= 0.6:
        tone = 'anxious'    # 低效价高唤醒 → 焦虑紧张
    elif avg_v < 0.4 and avg_a < 0.6:
        tone = 'heavy'      # 低效价低唤醒 → 沉重压抑
    else:
        tone = 'drifting'   # 中间地带 → 漂浮迷离

    # 拼出素材摘要给 AI
    parts = []
    for m in merged[:3]:
        parts.append(f"[{m['domain']}] {', '.join(m['names'][:2])}")
    if db_frags['events']:
        parts.append("活动碎片：" + "、".join(db_frags['events'][:2]))
    if db_frags['diary']:
        parts.append("日记片段：" + db_frags['diary'][:60])

    return tone, "\n".join(parts)

TONE_PROMPTS = {
    'vivid':    '这是一个鲜活的梦，色彩浓烈，充满动感，情绪高涨。',
    'warm':     '这是一个温柔的梦，光线柔和，时间像蜂蜜一样流动，有一种被爱包裹的感觉。',
    'anxious':  '这是一个不安的梦，走廊没有尽头，声音在正确的地方缺席，某件重要的事正在错过。',
    'heavy':    '这是一个沉默的梦，颜色是灰的，但不是空的——是那种饱含重量的灰。',
    'drifting': '这是一个漂浮的梦，没有地面，也不需要地面，只是在某种温热的介质里悬浮。',
}

# ── 调用 /wake 生成梦境文字 ──────────────────────────────────
def _call_wake_for_dream(tone, primer):
    tone_desc = TONE_PROMPTS.get(tone, TONE_PROMPTS['drifting'])
    payload = json.dumps({
        'mode': 'dream',
        'dream_tone': tone,
        'dream_primer': primer,
        'dream_tone_desc': tone_desc,
    }).encode()
    try:
        req = urllib.request.Request(
            f"{GATEWAY}/wake",
            data=payload,
            method='POST',
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            return (data.get('content') or data.get('text', '')).strip()
    except Exception as e:
        _log(f"wake call failed: {e}")
        return None

def _fallback_dream(tone, merged, db_frags):
    """API不可用时的降级诗意拼接"""
    desc = TONE_PROMPTS.get(tone, '')
    elements = []
    for m in merged[:2]:
        elements.append(f"关于{m['domain']}的{m['names'][0]}" if m['names'] else m['domain'])
    if db_frags['events']:
        elements.append(db_frags['events'][0])
    return f"{desc}\n梦里有：" + "，还有".join(elements) if elements else desc

# ── 主函数 ──────────────────────────────────────────────────
def generate_dream():
    _log("dream generation started (v2 three-layer)")

    # Layer 1
    brain_frags = _gather_brain_fragments(days=3)
    db_frags    = _gather_db_fragments()
    _log(f"gathered {len(brain_frags)} brain frags, {len(db_frags['events'])} events")

    # Layer 2
    merged = _merge_by_domain(brain_frags)
    _log(f"merged into {len(merged)} domain groups")

    # Layer 3
    tone, primer = _infer_dream_tone(merged, db_frags)
    _log(f"dream tone: {tone}")

    # 生成梦境文字
    dream_text = _call_wake_for_dream(tone, primer)
    if not dream_text:
        dream_text = _fallback_dream(tone, merged, db_frags)
        _log("using fallback dream text")
    else:
        _log("got AI-generated dream text")

    # 存入 dream_pool（藏起来，等情感共鸣时才浮现）
    try:
        conn = _db()
        now_str = (_now()).strftime('%Y-%m-%d %H:%M:%S')
        # 最近2小时内已有相同梦则跳过
        existing = conn.execute(
            "SELECT id FROM dream_pool WHERE content=? AND created_at > ?",
            (dream_text, (_now() - __import__('datetime').timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'))
        ).fetchone()
        if existing:
            _log("duplicate dream skipped")
            conn.close()
            return False
        # 计算平均情感坐标
        all_v = [m['valence'] for m in merged] or [0.5]
        all_a = [m['arousal'] for m in merged] or [0.5]
        avg_v = sum(all_v) / len(all_v)
        avg_a = sum(all_a) / len(all_a)
        conn.execute(
            "INSERT INTO dream_pool (content, valence, arousal, tone, created_at) VALUES (?,?,?,?,?)",
            (dream_text, avg_v, avg_a, tone, now_str)
        )
        conn.commit()
        conn.close()
        _log(f"dream stored in pool: {dream_text[:60]}")
        return True
    except Exception as e:
        _log(f"dream store error: {e}")
        return False

if __name__ == '__main__':
    generate_dream()
