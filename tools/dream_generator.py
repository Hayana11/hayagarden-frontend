#!/usr/bin/env python3.11
"""
梦生成器 v3 — 素材入口多样，特征概率出现，质量门只拦明确失败。

Layer 1: 按 FRAGMENT_PLAN 取材（近期加权随机 / 远期 / 日记或思绪 / 活动感官）
Layer 2: 保守去语境化 + primer 拼装（只含材料、不含解释）
Layer 3: 基调推断 → surface-owned background generation → 严重失败检测（最多重生一次）→ 入库
"""
import sqlite3, datetime, random, os, sys, json, re

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')

DB_PATH    = '/opt/frontend/memories.db'
LOG_FILE   = '/var/log/dream_generator.log'

PROMPT_VERSION = 'dream-v3'

FRAGMENT_PLAN = {
    'recent_memory': 2,   # 近3天，加权随机
    'remote_memory': 2,   # 14~365天前，从 posts 取
    'diary_or_wake': 1,   # 日记 或 wake_log 思绪，二选一
    'activity_sensory': 1,  # dream_events → 感官线索
}

ACTIVITY_SENSORY_MAP = {
    '微信': ['口袋里持续的震动', '一段没有发件人的语音'],
    '小红书': ['翻不到底的一叠彩色卡片', '许多陌生人的午后'],
    'default': ['屏幕的冷光', '滚动到一半停住的手指'],
}

DREAM_TRAITS = {
    'spatial_shift': (0.55, '至少一次无过渡的空间切换'),
    'identity_drift': (0.20, "某个人物或'我'的身份中途发生错误"),
    'time_error': (0.30, '时间顺序或时代出现一处错误'),
    'causal_reverse': (0.20, '一处因果倒置：结果先于原因发生'),
    'unresolved_ending': (0.65, '用完整句子收住，但不解释含义；允许悬空，不要半句截断'),
    'missing_center': (0.30, '最重要的现实人物以缺席、物体或声音替代'),
    # 第二批：梦化算子（并入同一概率框架，上限仍 3）
    'condensation': (0.25, '把两个材料熔成一个东西'),
    'literal_metaphor': (0.25, '把一个抽象说法当成字面事实来写'),
    'scale_distortion': (0.15, '一个物体的尺寸明显不对'),
    'role_exchange': (0.15, '主客关系颠倒：本该被我做的事，反过来对我做'),
}

HUMAN_SYMBOLS = [
    '一扇打不开的门', '知道回程的鸟',
    '留在椅背上的外套', '电话里的呼吸',
]

REALITY_ANCHOR_PATTERNS = [
    r'今天(?:我们|她|和她)',
    r'白天(?:发生|聊|说)',
    r'我们(?:聊到|讨论|说好)',
    r'最近(?:几天|一段时间|一直|总是|我们|她|老是)',
    r'现实中',
    r'微信',
    r'小红书',
]

EXPLANATION_PATTERNS = [
    r'我(?:终于|忽然|突然)?明白了',
    r'原来(?:这一切|这就是|它是)',
    r'这意味着',
    r'我(?:终于|这才)?意识到',
    r'我这才知道',
    r'也许这就是',
]

FAILURE_HINTS = {
    'too_short': '正文过短，请写具体梦境（可分段，最多1000字）',
    'explanatory_closure': '不要在结尾解释梦的含义或“明白了什么”',
    'reality_anchor': '不要出现今天/白天/我们聊到/最近/现实中/微信/小红书等现实锚点',
    'primer_copy': '不要原样照抄素材碎片，请改写成梦的感官与动作',
}

FALLBACK_PLACES = [
    '没有出口的地下候车室', '室内还在下雪的房间',
    '楼梯只向下却通向天空的塔', '所有座位都朝向窗外的教室',
]
FALLBACK_OBJECTS = [
    '内部仍在下雪的玻璃杯', '一封装着影子的信',
    '被反复抄写的夜晚', '停在半空的秒针',
]
FALLBACK_ACTIONS = [
    '把日期从每张纸上撕掉', '把影子折好寄出',
    '数一段没有尽头的电梯提示音', '给不存在的门上锁',
]
FALLBACK_SENSATIONS = [
    '潮湿的铁锈味', '指尖发凉却出汗',
    '远处持续的低鸣', '袖口突然变重',
]

_ENTITY_REPLACE = {
    '哈娅': '她',
    '哈雅娜': '她',
    '费奥多尔': '我',
    '费佳': '我',
}

_TIME_MARKER_RE = re.compile(
    r'今天|昨天|前天|明天|后天|'
    r'上午|下午|晚上|凌晨|中午|'
    r'\d{1,2}月\d{1,2}日|'
    r'\d{4}年\d{1,2}月|'
    r'上周|这周|本周|下周|'
    r'刚才|刚刚'
)


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


def ensure_dream_pool_metadata(conn=None):
    """幂等：dream_pool 加 metadata 列。cron 直调本脚本时不经过 app.py。"""
    own = conn is None
    if own:
        conn = _db()
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(dream_pool)').fetchall()]
        if not cols:
            return False
        if 'metadata' not in cols:
            conn.execute('ALTER TABLE dream_pool ADD COLUMN metadata TEXT')
            conn.commit()
            _log('dream_pool: added metadata column')
        return True
    except Exception as e:
        _log(f'ensure metadata failed: {e}')
        return False
    finally:
        if own:
            conn.close()


# ── Layer 1: 取材 ──────────────────────────────────────────

def _frag_weight(f):
    importance = float(f.get('importance') or 5)
    novelty = 1.0 - min(f.get('recall_count', 0) or 0, 10) / 10.0
    valence = float(f['valence'] if f.get('valence') is not None else 0.5)
    arousal = float(f['arousal'] if f.get('arousal') is not None else 0.3)
    unresolved = 1.0 if (
        (f.get('resolved') or 0) == 0 and (valence < 0.4 or arousal >= 0.6)
    ) else 0.3
    return (importance / 10.0) * 0.30 + unresolved * 0.25 \
        + novelty * 0.25 + random.random() * 0.20


def _weighted_sample(items, n):
    if n <= 0 or not items:
        return []
    pool = list(items)
    picked = []
    for _ in range(min(n, len(pool))):
        weights = [max(_frag_weight(f), 1e-6) for f in pool]
        choice = random.choices(pool, weights=weights, k=1)[0]
        picked.append(choice)
        pool.remove(choice)
    return picked


def _row_to_frag(row, source='memory'):
    return {
        'source_id': row['id'],
        'source': source,
        'content': (row['content'] or '').strip(),
        'valence': float(row['valence'] if row['valence'] is not None else 0.5),
        'arousal': float(row['arousal'] if row['arousal'] is not None else 0.3),
        'importance': int(row['importance'] if row['importance'] is not None else 5),
        'recall_count': int(row['recall_count'] if row['recall_count'] is not None else 0),
        'resolved': int(row['resolved'] if row['resolved'] is not None else 0),
    }


def _collect_recent_memories(conn, n=2):
    rows = conn.execute(
        """
        SELECT id, content, valence, arousal, importance, recall_count, resolved
        FROM posts
        WHERE type IN ('MEMORY', 'DIARY')
          AND created_at > datetime('now', '+8 hours', '-3 days')
          AND TRIM(COALESCE(content, '')) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(n * 8, 16),),
    ).fetchall()
    pool = [_row_to_frag(r, 'recent') for r in rows]
    return _weighted_sample(pool, n)


def _collect_remote_memories(conn, n=2):
    """远期记忆：14~365天前，novelty/unresolved 用既有列代理。"""
    if n <= 0:
        return []
    rows = conn.execute(
        """
        SELECT id, content, valence, arousal, importance, recall_count, resolved
        FROM posts
        WHERE type IN ('MEMORY', 'DIARY')
          AND created_at < datetime('now', '+8 hours', '-14 days')
          AND created_at > datetime('now', '+8 hours', '-365 days')
          AND COALESCE(resolved, 0) = 0
          AND TRIM(COALESCE(content, '')) != ''
        ORDER BY COALESCE(recall_count, 0) ASC,
                 COALESCE(last_recalled_at, created_at) ASC,
                 RANDOM()
        LIMIT ?
        """,
        (n * 4,),
    ).fetchall()
    pool = [_row_to_frag(r, 'remote') for r in rows]
    random.shuffle(pool)
    return pool[:n]


def _collect_diary_or_wake(conn):
    """日记或 wake_log 思绪，二选一，不同时塞。"""
    prefer_diary = random.random() < 0.5
    order = ('diary', 'wake') if prefer_diary else ('wake', 'diary')
    for kind in order:
        if kind == 'diary':
            row = conn.execute(
                """
                SELECT id, content, valence, arousal, importance, recall_count, resolved
                FROM posts
                WHERE type='DIARY' AND TRIM(COALESCE(content,'')) != ''
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if row:
                frag = _row_to_frag(row, 'diary')
                frag['content'] = frag['content'][:150]
                return frag, 'diary'
        else:
            row = conn.execute(
                """
                SELECT id, thoughts AS content FROM wake_log
                WHERE TRIM(COALESCE(thoughts,'')) != ''
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if row and row['content']:
                return {
                    'source_id': -int(row['id']),
                    'source': 'wake_log',
                    'content': str(row['content']).strip()[:80],
                    'valence': 0.5,
                    'arousal': 0.4,
                    'importance': 5,
                    'recall_count': 0,
                    'resolved': 0,
                }, 'wake_log'
    return None, None


def _translate_activity(value):
    text = value or ''
    for app, clues in ACTIVITY_SENSORY_MAP.items():
        if app == 'default':
            continue
        if app in text:
            return random.choice(clues)
    return random.choice(ACTIVITY_SENSORY_MAP['default'])


def _collect_activity_sensory(conn, n=1):
    if n <= 0:
        return []
    rows = conn.execute(
        """
        SELECT value FROM dream_events
        WHERE date(created_at) >= date('now', '-2 days')
        ORDER BY id DESC LIMIT 4
        """
    ).fetchall()
    if not rows:
        return []
    clues = []
    seen = set()
    for r in rows:
        clue = _translate_activity(r['value'])
        if clue in seen:
            continue
        seen.add(clue)
        clues.append(clue)
        if len(clues) >= n:
            break
    return clues


def _gather_materials(conn):
    """按 FRAGMENT_PLAN 取材；远期不足时缺额回填 recent，不硬凑。"""
    remote_n = FRAGMENT_PLAN['remote_memory']
    recent_n = FRAGMENT_PLAN['recent_memory']

    remote = _collect_remote_memories(conn, remote_n)
    shortfall = remote_n - len(remote)
    recent = _collect_recent_memories(conn, recent_n + shortfall)

    diary_frag, diary_kind = _collect_diary_or_wake(conn)
    sensory = _collect_activity_sensory(conn, FRAGMENT_PLAN['activity_sensory'])

    frags = list(recent) + list(remote)
    # 日记可能已在 recent（type 含 DIARY）里被抽到——按 source_id 去重，避免 primer 双份
    if diary_frag:
        seen_ids = {f.get('source_id') for f in frags}
        if diary_frag.get('source_id') in seen_ids:
            diary_kind = None
        else:
            frags.append(diary_frag)

    source_mix = {
        'recent': len(recent),
        'remote': len(remote),
        'diary': 1 if diary_kind == 'diary' else 0,
        'wake_log': 1 if diary_kind == 'wake_log' else 0,
        'synthetic': 0,
        'old_motif': 0,
    }

    vals = [f['valence'] for f in frags] or [0.5]
    arous = [f['arousal'] for f in frags] or [0.3]
    return {
        'fragments': frags,
        'sensory': sensory,
        'source_mix': source_mix,
        'avg_v': sum(vals) / len(vals),
        'avg_a': sum(arous) / len(arous),
    }


# ── Layer 2: 去语境化 + primer ─────────────────────────────

def _strip_time_markers(text):
    return _TIME_MARKER_RE.sub('', text)


def _decontextualize_v1(text):
    """轨道A 基础：实体替换 + 概率抹时间。"""
    if not text:
        return ''
    for k, v in _ENTITY_REPLACE.items():
        text = text.replace(k, v)
    if random.random() < 0.65:
        text = _strip_time_markers(text)
    return re.sub(r'\s+', ' ', text).strip()


def _second_person_to_symbol(text, symbol):
    """把第二人称/她 的局部出现替换为符号物（保守：最多一处）。"""
    for token in ('她', '你'):
        if token in text:
            return text.replace(token, symbol, 1)
    return text


def _decontextualize(text):
    """双轨A：v1 + 0.45 概率人→符号。"""
    text = _decontextualize_v1(text)
    if text and random.random() < 0.45:
        text = _second_person_to_symbol(text, random.choice(HUMAN_SYMBOLS))
    return text

def _pick_traits():
    picked = [k for k, (p, _) in DREAM_TRAITS.items() if random.random() < p]
    if len(picked) > 3:
        picked = random.sample(picked, 3)
    if not picked:
        picked = ['unresolved_ending']
    return picked


def _infer_tone(avg_v, avg_a):
    if avg_v >= 0.6 and avg_a >= 0.6:
        return 'vivid'
    if avg_v >= 0.6 and avg_a < 0.6:
        return 'warm'
    if avg_v < 0.4 and avg_a >= 0.6:
        return 'anxious'
    if avg_v < 0.4 and avg_a < 0.6:
        return 'heavy'
    return 'drifting'


TONE_PROMPTS = {
    'vivid':    '这是一个鲜活的梦，色彩浓烈，充满动感，情绪高涨。',
    'warm':     '这是一个温柔的梦，光线柔和，时间像蜂蜜一样流动，有一种被爱包裹的感觉。',
    'anxious':  '这是一个不安的梦，走廊没有尽头，声音在正确的地方缺席，某件重要的事正在错过。',
    'heavy':    '这是一个沉默的梦，颜色是灰的，但不是空的——是那种饱含重量的灰。',
    'drifting': '这是一个漂浮的梦，没有地面，也不需要地面，只是在某种温热的介质里悬浮。',
}


def _build_primer(tone, materials, traits, places=None, objects=None, actions=None,
                  shards=None):
    """只含材料、不含解释。不出现姓名/日期/domain/应用名/事件结论标签。"""
    parts = [f'基调：{TONE_PROMPTS.get(tone, TONE_PROMPTS["drifting"])}']
    if places:
        parts.append('潜在场所：' + '、'.join(places))
    if objects:
        parts.append('潜在物体：' + '、'.join(objects))
    if actions:
        parts.append('潜在动作：' + '、'.join(actions))

    sensory = list(materials.get('sensory') or [])
    if sensory:
        parts.append('潜在感官：' + '、'.join(sensory))

    frag_texts = []
    for f in materials.get('fragments') or []:
        cleaned = _decontextualize((f.get('content') or '')[:120])
        if cleaned:
            frag_texts.append(cleaned)
    if frag_texts:
        parts.append('碎片：' + ' / '.join(frag_texts))

    if shards:
        quoted = ' / '.join(f'“{s}”' for s in shards)
        parts.append(f'碎片残渣：{quoted}')

    trait_lines = [DREAM_TRAITS[k][1] for k in traits if k in DREAM_TRAITS]
    if trait_lines:
        parts.append('本场特征：' + '；'.join(trait_lines))

    return '\n'.join(parts)


def _latents_mod():
    try:
        import dream_latents as dl
        return dl
    except ImportError:
        from tools import dream_latents as dl
        return dl


def _enrich_with_latents(conn, materials):
    """synthetic 概率注入 + 旧母题回流到场所/物体/动作/感官。"""
    dl = _latents_mod()
    dl.ensure_table(conn)
    dl.seed_synthetic_if_empty(conn)

    synthetic_count = random.choices([0, 1, 2, 3], weights=[0.10, 0.35, 0.40, 0.15])[0]
    synth = dl.pick_synthetic(conn, synthetic_count)
    materials['source_mix']['synthetic'] = len(synth)

    motif_count = random.choices([0, 1, 2], weights=[0.55, 0.35, 0.10])[0]
    motifs = dl.pick_motifs(conn, motif_count)
    materials['source_mix']['old_motif'] = len(motifs)

    places, objects, actions = [], [], []
    sensory = list(materials.get('sensory') or [])
    used_ids = []

    for item in list(synth) + list(motifs):
        typ = item.get('type')
        content = (item.get('content') or '').strip()
        if not content:
            continue
        if item.get('id') is not None:
            used_ids.append(item['id'])
        if typ == 'place':
            places.append(content)
        elif typ == 'object':
            objects.append(content)
        elif typ == 'action':
            actions.append(content)
        elif typ == 'sensation':
            sensory.append(content)
        elif typ in ('figure', 'phrase'):
            # 并入碎片侧，避免单独标签解释
            materials.setdefault('fragments', []).append({
                'source_id': -(100000 + int(item.get('id') or 0)),
                'source': 'latent',
                'content': content,
                'valence': float(item.get('valence') or 0.5),
                'arousal': float(item.get('arousal') or 0.4),
                'importance': 5,
                'recall_count': 0,
                'resolved': 0,
            })

    materials['sensory'] = sensory
    shards = dl.extract_shards(materials.get('fragments') or [], max_shards=2)
    return {
        'places': places,
        'objects': objects,
        'actions': actions,
        'shards': shards,
        'used_latent_ids': used_ids,
    }


# ── 严重失败检测 ───────────────────────────────────────────

def _long_verbatim_overlap(dream_text, primer_text, min_len=18):
    """连续子串照抄检测（不用 Jaccard）。"""
    if not dream_text or not primer_text or len(primer_text) < min_len:
        return False
    # 只对「碎片」段做照抄检测，避免基调句误伤
    probe = primer_text
    if '碎片：' in primer_text:
        probe = primer_text.split('碎片：', 1)[1]
    probe = re.sub(r'\s+', '', probe)
    hay = re.sub(r'\s+', '', dream_text)
    if len(probe) < min_len:
        return False
    for i in range(0, len(probe) - min_len + 1):
        chunk = probe[i:i + min_len]
        if chunk and chunk in hay:
            return True
    return False


def _severe_failure(dream_text, primer_text):
    if not dream_text or len(dream_text) < 150:
        return 'too_short'
    tail = dream_text[-200:]
    if any(re.search(p, tail) for p in EXPLANATION_PATTERNS):
        return 'explanatory_closure'
    if any(re.search(p, dream_text) for p in REALITY_ANCHOR_PATTERNS):
        return 'reality_anchor'
    if _long_verbatim_overlap(dream_text, primer_text, min_len=18):
        return 'primer_copy'
    return None


# ── Dream surface-owned model generation + fallback ─────────

def _dream_background_request(tone, primer, extra=None):
    """Build only the Dream surface contract; never inherit generic Wake context."""
    from chat.system_builder import read_persona
    from wake.builder import build_prompt_suffix
    from chat.background_generation import BackgroundGenerationRequest

    primer_payload = primer if not extra else f'{primer}\\n\\n{extra}'
    context = {
        'time': _now().strftime('%Y-%m-%d %H:%M:%S'),
        'dream_tone': tone,
        'dream_tone_desc': TONE_PROMPTS.get(tone, TONE_PROMPTS['drifting']),
        'dream_primer': primer_payload,
    }
    persona = read_persona().strip()
    suffix = build_prompt_suffix('dream', context).strip()
    system_text = '\\n\\n'.join(part for part in (persona, suffix) if part)
    return BackgroundGenerationRequest(
        system_text=system_text,
        prompt_text='[做梦]',
        max_tokens_hint=4096,
        timeout_sec=90,
        task_kind='dream',
    )


def _generate_dream_model(tone, primer, authority, attempt, extra=None):
    """Execute one frozen-authority Dream attempt and accept only message CONTENT."""
    from chat.background_generation import BackgroundGenerationError, generate_background
    from chat.cc_auth import read_cc_oauth_token
    from wake.parser import parse_response

    request = _dream_background_request(tone, primer, extra=extra)
    try:
        result = generate_background(
            request,
            authority,
            cc_token_getter=read_cc_oauth_token,
        )
    except BackgroundGenerationError as exc:
        _log(
            'dream background failed provider=%s model=%s attempt=%s code=%s' % (
                authority.provider, authority.model_identity, attempt, type(exc).__name__,
            )
        )
        return '', 'failed', False
    except Exception as exc:
        _log(
            'dream background failed provider=%s model=%s attempt=%s code=%s' % (
                authority.provider, authority.model_identity, attempt, type(exc).__name__,
            )
        )
        return '', 'failed', False

    _, action, content = parse_response(result.text or '')
    executor = str(result.actual_executor or 'unknown')
    if action != 'message' or not content.strip():
        _log(
            'dream background invalid provider=%s model=%s executor=%s attempt=%s' % (
                authority.provider, authority.model_identity, executor, attempt,
            )
        )
        return '', executor, False
    return content, executor, True


def _fallback_dream(tone, conn=None, *_args):
    """优先 latents 旧母题，静态词库退为冷启动兜底。"""
    places = list(FALLBACK_PLACES)
    objects = list(FALLBACK_OBJECTS)
    actions = list(FALLBACK_ACTIONS)
    sensations = list(FALLBACK_SENSATIONS)
    used_ids = []
    if conn is not None:
        try:
            dl = _latents_mod()
            dl.ensure_table(conn)
            rows = dl.get_fallback_latents(conn, limit=5)
            for r in rows:
                content = (r.get('content') or '').strip()
                if not content:
                    continue
                typ = r.get('type')
                if typ == 'place':
                    places.append(content)
                elif typ == 'object':
                    objects.append(content)
                elif typ == 'action':
                    actions.append(content)
                elif typ == 'sensation':
                    sensations.append(content)
                if r.get('id') is not None:
                    used_ids.append(r['id'])
            if used_ids:
                dl.mark_used(conn, used_ids)
        except Exception as e:
            _log(f'fallback latents skipped: {e}')
    a, b = random.sample(places, 2) if len(places) >= 2 else (places[0], places[0])
    return (
        f'我站在{a}，手里拿着{random.choice(objects)}。'
        f'它一直在{random.choice(actions)}，但周围的人似乎听不见。'
        f'门打开以后不是房间，而是{b}。'
        f'我忽然记不起自己为什么有这双手，只记得{random.choice(sensations)}。'
        f'后来发生了一件很重要的事，醒来只剩一处空白。'
    )


# ── 主函数 ──────────────────────────────────────────────────

def generate_dream():
    _log('dream generation started (v3 fragment-plan + latents)')
    # Capture exactly once before material collection. Every model attempt below
    # receives this immutable authority, even if runtime config changes mid-task.
    from chat.provider_router import capture_generation_authority
    authority = capture_generation_authority()
    _log('dream authority provider=%s model=%s' % (
        authority.provider, authority.model_identity,
    ))

    conn = _db()
    ensure_dream_pool_metadata(conn)

    materials = _gather_materials(conn)
    latent_bits = {'places': [], 'objects': [], 'actions': [],
                   'shards': [], 'used_latent_ids': []}
    try:
        latent_bits = _enrich_with_latents(conn, materials)
    except Exception as e:
        _log(f'latents enrich skipped: {e}')

    tone = _infer_tone(materials['avg_v'], materials['avg_a'])
    traits = _pick_traits()
    primer = _build_primer(
        tone, materials, traits,
        places=latent_bits.get('places'),
        objects=latent_bits.get('objects'),
        actions=latent_bits.get('actions'),
        shards=latent_bits.get('shards'),
    )
    _log(
        f"mix={materials['source_mix']} tone={tone} traits={traits} "
        f'primer_len={len(primer)}'
    )

    used_fallback = False
    regenerated = False
    failure_reason = None
    generation_attempts = 0
    generation_executor = 'not_attempted'
    model_generation_succeeded = False

    from tools.dream_meta import sanitize_dream_content

    generation_attempts += 1
    raw_text, generation_executor, model_generation_succeeded = _generate_dream_model(
        tone, primer, authority, generation_attempts,
    )
    dream_text = sanitize_dream_content(raw_text)
    if not dream_text:
        dream_text = sanitize_dream_content(_fallback_dream(tone, conn))
        used_fallback = True
        _log('using fallback dream text (model unavailable)')
    else:
        reason = _severe_failure(dream_text, primer)
        if reason:
            regenerated = True
            hint = FAILURE_HINTS.get(reason, reason)
            _log(f'severe failure: {reason}, regenerating once')
            generation_attempts += 1
            raw_text, generation_executor, retry_succeeded = _generate_dream_model(
                tone,
                primer,
                authority,
                generation_attempts,
                extra=f'上一次生成失败：{hint}，请避免。',
            )
            model_generation_succeeded = model_generation_succeeded or retry_succeeded
            dream2 = sanitize_dream_content(raw_text)
            if dream2:
                dream_text = dream2
            failure_reason = _severe_failure(dream_text, primer)
            if failure_reason:
                _log(f'still failing after regen: {failure_reason}; storing anyway')
            else:
                _log('regen recovered')
        else:
            _log('got AI-generated dream text')

    # 标记本场用过的母题
    try:
        used_ids = latent_bits.get('used_latent_ids') or []
        if used_ids:
            _latents_mod().mark_used(conn, used_ids)
    except Exception as e:
        _log(f'mark latents used failed: {e}')

    metadata = {
        'prompt_version': PROMPT_VERSION,
        'source_mix': materials['source_mix'],
        'traits': traits,
        'fallback': used_fallback,
        'regenerated': regenerated,
        'failure_reason': failure_reason,
        'generation_provider': authority.provider,
        'generation_model_identity': authority.model_identity,
        'generation_executor': generation_executor,
        'generation_attempts': generation_attempts,
        'model_generation_succeeded': model_generation_succeeded,
    }

    try:
        now_str = _now().strftime('%Y-%m-%d %H:%M:%S')
        cutoff = (_now() - datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        existing = conn.execute(
            'SELECT id FROM dream_pool WHERE content=? AND created_at > ?',
            (dream_text, cutoff),
        ).fetchone()
        if existing:
            _log('duplicate dream skipped')
            conn.close()
            return False

        avg_v = materials['avg_v']
        avg_a = materials['avg_a']
        meta_json = json.dumps(metadata, ensure_ascii=False)
        cols = [r[1] for r in conn.execute('PRAGMA table_info(dream_pool)').fetchall()]
        if 'metadata' in cols:
            conn.execute(
                'INSERT INTO dream_pool (content, valence, arousal, tone, created_at, metadata) '
                'VALUES (?,?,?,?,?,?)',
                (dream_text, avg_v, avg_a, tone, now_str, meta_json),
            )
        else:
            conn.execute(
                'INSERT INTO dream_pool (content, valence, arousal, tone, created_at) '
                'VALUES (?,?,?,?,?)',
                (dream_text, avg_v, avg_a, tone, now_str),
            )
        dream_row_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()

        # 意象回流：fallback 权重 0.2 防回路
        try:
            dl = _latents_mod()
            dl.ensure_table(conn)
            items = dl.extract_dream_latents(dream_text, max_items=3)
            origin = 'fallback_dream' if used_fallback else 'dream'
            weight = dl.MOTIF_EXTRACTION_WEIGHT.get(
                'fallback_dream' if used_fallback else 'normal_dream', 1.0
            )
            added = dl.insert_latents(
                conn, items, origin=origin, source_id=dream_row_id,
                valence=avg_v, arousal=avg_a, weight=weight,
            )
            _log(f'latents reflux: origin={origin} candidates={len(items)} added={added}')
        except Exception as e:
            _log(f'latents reflux skipped: {e}')

        conn.close()

        import memory_tool
        memory_tool.save_memory(
            dream_text,
            type='DREAM',
            layer='recent',
            created_at=now_str,
            tags=tone,
            valence=avg_v,
            arousal=avg_a,
        )
        _log(f'dream stored in pool+posts: {dream_text[:60]}')
        return True
    except Exception as e:
        _log(f'dream store error: {e}')
        try:
            conn.close()
        except Exception:
            pass
        return False

if __name__ == '__main__':
    generate_dream()


