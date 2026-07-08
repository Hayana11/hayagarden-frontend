"""记忆库四层语义：核心 / 长期 / 短期 / 未消化。

核心：两人长期不变的事实（习惯、偏好、居住、猫、亲密偏好）+ 感情浓度很深的对话。
长期：印象深刻的事；或被召回次数 ≥ RECALL_PROMOTE_LONG。
短期：已消化、分量不重的随手记（晚饭、买了裙子）。
未消化：processed=0 尚未夜巡，或 importance≤2 的碎屑。
"""
import re

RECALL_PROMOTE_LONG = 3

# 一次性琐事 —— 不应进核心，甚至不应标成 FACT
EPHEMERAL_MARKERS = (
    '买了', '吃了', '晚饭', '午饭', '早餐', '宵夜', '裙子', '衣服', '快递',
    '今天去', '刚买', '收到', '下单', '外卖', '奶茶', '咖啡',
)

# 稳定事实 / 关系基石
STABLE_FACT_MARKERS = (
    '习惯', '偏好', '住在', '地址', '居住', '饮食', '猫', '裸睡', '乳夹',
    '喜欢', '讨厌', '约定', '承诺', '纪念日', '我们俩', '两人', '长期',
    '一直', '平时', '通常', '家里',
)

# 感情浓度深的对话线索
DEEP_DIALOGUE_MARKERS = (
    '对话', '说过', '聊', '汗', '盐', '信任', '陪伴', '称呼', '照顾',
)


def recall_promote_threshold():
    try:
        import config_store as cs
        return cs.get_int('RECALL_PROMOTE_LONG', RECALL_PROMOTE_LONG)
    except Exception:
        return RECALL_PROMOTE_LONG


def is_ephemeral_note(content):
    """随手一记：今天吃了什么、买了裙子等一次性琐事。"""
    c = (content or '').strip()
    if not c:
        return False
    if any(m in c for m in EPHEMERAL_MARKERS):
        return True
    if re.search(r'今天.{0,8}(买|吃|去)', c):
        return True
    return False


def has_stable_couple_fact(content):
    """两人长期不变的事实：居住、饮食偏好、习惯、猫、亲密偏好等。"""
    c = (content or '').strip()
    return bool(c) and any(m in c for m in STABLE_FACT_MARKERS)


def has_deep_emotional(content, tags=''):
    """感情浓度很深的对话或亲密记忆。"""
    c = (content or '').strip()
    t = (tags or '').lower()
    if '情绪' in t or '色色' in t:
        return True
    return any(m in c for m in DEEP_DIALOGUE_MARKERS)


def row_int(row, key, default=0):
    try:
        if key not in row.keys():
            return default
        v = row[key]
        return default if v is None else int(v)
    except Exception:
        return default


def compute_display_weight(row):
    """Map DB row → UI weight 1–5（未消化/短期/长期/核心）。"""
    processed = row_int(row, 'processed', 0)
    importance = row_int(row, 'importance', 0)
    recall = row_int(row, 'recall_count', 0)
    pinned = row_int(row, 'pinned', 0)
    layer = (row['layer'] or 'recent').strip()
    ptype = (row['type'] or 'MEMORY').strip()
    tags = (row['tags'] or '').lower()
    content = (row['content'] or '').strip()
    threshold = recall_promote_threshold()

    if pinned:
        return 5

    if not processed:
        return 1

    if importance <= 2 and ptype not in ('FACT',) and recall < threshold:
        if layer not in ('core', 'long-term'):
            return 1

    ephemeral = is_ephemeral_note(content)

    # 误标成 FACT 的琐事 → 短期（可被 recall 升到长期）
    if ephemeral and importance < 9:
        if recall >= threshold:
            return 4
        return 2

    is_core_material = (
        layer == 'core'
        or (ptype == 'FACT' and layer == 'core')
        or importance >= 9
        or (importance >= 8 and (has_deep_emotional(content, tags) or has_stable_couple_fact(content)))
        or (importance >= 7 and has_stable_couple_fact(content) and not ephemeral)
        or (importance >= 5 and has_stable_couple_fact(content) and not ephemeral)
        or (importance >= 8 and has_deep_emotional(content, tags))
    )
    if is_core_material:
        return 5

    if recall >= threshold:
        return 4
    if layer in ('long', 'long-term'):
        return 4
    if importance >= 6:
        return 4
    if ptype == 'DAILY_SUMMARY':
        return 4

    return 2
