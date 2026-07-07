"""Build MemoryLibrary JSON for the React memory screen from posts table."""
import re
from collections import Counter, defaultdict

LIBRARY_TYPES = ('MEMORY', 'DIARY', 'FACT', 'THOUGHT', 'DREAM', 'DAILY_SUMMARY')

TOPIC_CATALOG = {
    '日常': {'key': 'life', 'emoji': '🍞', 'name': '日常', 'desc': '账目、天气、日常琐事与随口一提的小事。'},
    '情绪': {'key': 'emotion', 'emoji': '🌙', 'name': '情绪与陪伴', 'desc': '心情、低气压、亲密对话与彼此照护。'},
    '技术': {'key': 'tech', 'emoji': '⚙️', 'name': '技术', 'desc': '系统、代码、工具链与调试记录。'},
    'fact': {'key': 'facts', 'emoji': '📌', 'name': '长期事实', 'desc': '稳定的人物偏好与生活事实。'},
    '色色': {'key': 'intimate', 'emoji': '💝', 'name': '亲密', 'desc': '亲密互动与身体相关的私密记忆。'},
    'DIARY': {'key': 'diary', 'emoji': '📓', 'name': '日记', 'desc': '按日写下的生活记录。'},
    'DREAM': {'key': 'dream', 'emoji': '🌙', 'name': '梦境', 'desc': '睡梦、潜意识与夜间的画面。'},
    'THOUGHT': {'key': 'thought', 'emoji': '💭', 'name': '想法', 'desc': '零散思绪与灵感碎片。'},
    'DAILY_SUMMARY': {'key': 'summary', 'emoji': '📅', 'name': '日摘要', 'desc': '系统自动整理的一天回顾。'},
    'MEMORY': {'key': 'memory', 'emoji': '✨', 'name': '记忆', 'desc': '被明确存入记忆库的内容。'},
}


def _parse_tags(tags_raw):
    tags, assocs = [], []
    for part in (tags_raw or '').split(','):
        part = part.strip()
        if not part:
            continue
        if part.startswith('assoc:'):
            assocs.extend(a.strip() for a in part[6:].split('|') if a.strip())
        elif not part.startswith('assoc'):
            tags.append(part)
    return tags, assocs


def _author_who(author):
    a = (author or '').strip().lower()
    if a in ('haya', 'haya11'):
        return '哈娅'
    return '费佳'


def _title_from_content(content):
    text = (content or '').strip()
    if not text:
        return '未命名记忆'
    line = text.split('\n')[0].strip()
    line = re.sub(r'^#+\s*', '', line)
    line = re.sub(r'^【[^】]+】\s*', '', line).strip()
    if len(line) > 52:
        return line[:52] + '…'
    return line or '未命名记忆'


def _post_weight(row):
    layer = (row['layer'] or 'recent').strip()
    importance = int(row['importance'] or 0)
    pinned = int(row['pinned'] or 0)
    if pinned:
        return 5
    if layer == 'core':
        return 5
    if layer in ('long', 'long-term'):
        return 5 if importance >= 6 else 4
    if importance >= 6:
        return 4
    if importance >= 4:
        return 3
    if importance >= 2:
        return 2
    return 1


def _topic_key_for_row(row, tags):
    if tags:
        first = tags[0]
        meta = TOPIC_CATALOG.get(first)
        if meta:
            return meta['key']
        slug = re.sub(r'[^\w\u4e00-\u9fff-]+', '-', first).strip('-').lower() or 'misc'
        return f'tag-{slug}'
    ptype = (row['type'] or 'MEMORY').strip()
    return TOPIC_CATALOG.get(ptype, TOPIC_CATALOG['MEMORY'])['key']


def _topic_meta(key, label_hint=None):
    for meta in TOPIC_CATALOG.values():
        if meta['key'] == key:
            return dict(meta)
    name = label_hint or key.replace('tag-', '').replace('-', ' ')
    return {
        'key': key,
        'emoji': '🏷',
        'name': name,
        'desc': f'与「{name}」相关的记忆集合。',
    }


def _ai_blurb(name, entries):
    if not entries:
        return f'关于{name}的记忆还在慢慢积累。'
    dates = sorted(e['date'] for e in entries)
    span = ''
    if dates[0] != dates[-1]:
        span = f'从 {dates[0].replace("-", ".")} 到 {dates[-1].replace("-", ".")}，'
    core_n = sum(1 for e in entries if e['weight'] >= 5)
    return f'{span}共 {len(entries)} 条记忆' + (f'，其中 {core_n} 条是核心级。' if core_n else '。')


def build_memory_library(conn, limit=500):
    placeholders = ','.join('?' * len(LIBRARY_TYPES))
    rows = conn.execute(
        f"""SELECT id, type, content, author, created_at, pinned, tags, layer, importance
            FROM posts
            WHERE type IN ({placeholders}) AND COALESCE(resolved, 0) = 0
            ORDER BY created_at DESC, id DESC
            LIMIT ?""",
        (*LIBRARY_TYPES, limit),
    ).fetchall()

    entries = []
    topic_labels = {}
    assoc_by_id = {}

    for row in rows:
        tags, assocs = _parse_tags(row['tags'])
        created = (row['created_at'] or '').strip()
        date_part, _, time_part = created.partition(' ')
        topic_key = _topic_key_for_row(row, tags)
        if tags:
            topic_labels.setdefault(topic_key, tags[0])
        elif row['type']:
            topic_labels.setdefault(topic_key, row['type'])

        entry = {
            'id': int(row['id']),
            'date': date_part or created[:10],
            'time': (time_part or '00:00')[:5],
            'weight': _post_weight(row),
            'title': _title_from_content(row['content']),
            'who': _author_who(row['author']),
            'topics': [topic_key],
            'tags': tags[:8],
            'content': (row['content'] or '').strip(),
            'links': [],
        }
        entries.append(entry)
        assoc_by_id[entry['id']] = set(assocs)

    # Semantic links via shared assoc words
    for i, a in enumerate(entries):
        shared = []
        a_assocs = assoc_by_id.get(a['id'], set())
        if not a_assocs:
            continue
        for b in entries:
            if b['id'] == a['id']:
                continue
            overlap = a_assocs & assoc_by_id.get(b['id'], set())
            if overlap:
                shared.append((b['id'], len(overlap)))
        shared.sort(key=lambda x: x[1], reverse=True)
        a['links'] = [sid for sid, _ in shared[:4]]

    # Topics aggregate
    by_topic = defaultdict(list)
    for e in entries:
        by_topic[e['topics'][0]].append(e)

    topics = []
    for key, group in sorted(by_topic.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        meta = _topic_meta(key, topic_labels.get(key))
        others = sorted(
            ((k, len(set(g['id'] for g in by_topic[k]) & set(x['id'] for x in group))) for k in by_topic if k != key),
            key=lambda x: x[1],
            reverse=True,
        )
        related = [{'key': k, 'pct': round(min(0.95, 0.55 + 0.08 * n), 2)} for k, n in others[:3] if n > 0]
        topics.append({
            'key': meta['key'],
            'emoji': meta['emoji'],
            'name': meta['name'],
            'desc': meta['desc'],
            'ai': _ai_blurb(meta['name'], group),
            'related': related,
        })

    return {'topics': topics, 'entries': entries}
