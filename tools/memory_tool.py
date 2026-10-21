"""记忆系统唯一写入口（M1 统一协议）。
所有往 posts 表写记忆的代码——gateway 工具、[[SAVE:]]、日记、总结、梦、思绪、手动 API——
一律经过 save_memory()，不许裸 INSERT。

B-3：save_memory 是记忆类型（MEMORY / FACT / MISS）的唯一写入路由。
runtime_config memory.kernel_write 打开时，记忆类型写进 Memory Kernel（再投影到 Ombre）；
非记忆内容（DIARY / DREAM / THOUGHT / DAILY_SUMMARY / WEEKLY_SUMMARY / PHOTO …）仍由 posts 管理。
schema 约定：
  type   : MEMORY / DIARY / DREAM / THOUGHT / DAILY_SUMMARY / WEEKLY_SUMMARY / MISS ...
  layer  : core（永久核心）/ long-term（长期）/ recent（近期，生命周期 cron 会代谢它）
  resolved: 0=活跃 1=已退役（自动注入和召回不再看它，主动搜索仍可见）
  recall_count / last_recalled_at: 召回加热（被真正注入 prompt 才算召回）
"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tools import summary_title

DB_PATH = '/opt/frontend/memories.db'

VALID_LAYERS = ('core', 'long-term', 'recent')
# 与 B-1 导入范围一致；只有这些类型会被路由进 Memory Kernel。
MEMORY_TYPES = ('MEMORY', 'FACT', 'MISS')
_BEIJING = timezone(timedelta(hours=8))
_LOG = logging.getLogger('memory_tool')


@dataclass(frozen=True)
class KernelWrite:
    """记忆写进了 Memory Kernel（并投影到 Ombre）；它没有 posts.id。"""
    status: str          # CREATED / DEDUPLICATED
    state_id: str
    lineage_id: str
    bucket_id: str
    ombre_status: str


def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def save_memory(content, type='MEMORY', author='fyodor', layer='recent',
                tags='', importance=0, pinned=0, created_at=None,
                valence=None, arousal=None, *, source='save_memory', provenance=None):
    """统一写入口。created_at 传 None 用当前东八时间。

    返回值契约：
      int          写进了 posts，值是 posts.id —— 非记忆类型；或开关关闭；
                   或 Kernel 失败后回退到 posts（同时登记待补，sync() 会补回 Kernel）
      KernelWrite  开关打开时的记忆类型，写进了 Kernel，没有 posts.id
      None         正文为空，什么都没写
    """
    content = (content or '').strip()
    if not content:
        return None
    if layer not in VALID_LAYERS:
        layer = 'recent'
    fields = dict(type=type, author=author, layer=layer, tags=tags, importance=importance,
                  pinned=pinned, created_at=created_at, valence=valence, arousal=arousal)
    if type in MEMORY_TYPES:
        from tools.memory_kernel_switch import write_enabled

        if write_enabled(DB_PATH):
            try:
                return _kernel_save(content, source=source, provenance=provenance, **fields)
            except Exception as exc:
                _LOG.warning('memory_kernel_save_failed_fallback_posts error_type=%s', exc.__class__.__name__)
                # 帖子和待补登记同一事务提交：不会出现写进旧库却没人来补的记忆。
                return _legacy_save(content, backfill_reason=exc.__class__.__name__, **fields)
    return _legacy_save(content, **fields)


def find_kernel_memory(content):
    """开关打开时，这段记忆是否已经在 Kernel 里（规范化后同文）；返回 KernelWrite 或 None。

    给回执恢复用：Kernel 已写成功、回执还没收尾时进程退出，重试要能认出它。
    开关关闭时不看 Kernel（B-1 导入的旧内容不能被当成这次写入的结果）。
    """
    from tools.memory_kernel_switch import write_enabled

    if not (content or '').strip() or not write_enabled(DB_PATH):
        return None
    from tools import memory_kernel_store

    found = memory_kernel_store.find_head(content)
    if found is None:
        return None
    return KernelWrite(status='DEDUPLICATED', state_id=found['state_id'], lineage_id=found['lineage_id'],
                       bucket_id=found['bucket_id'], ombre_status=found['ombre_status'])


def _kernel_save(content, *, source, provenance, type, author, layer, tags, importance,
                 pinned, created_at, valence, arousal):
    from tools import memory_kernel_store

    occurred_at = None
    if created_at:
        parsed = datetime.fromisoformat(str(created_at).replace(' ', 'T', 1))
        occurred_at = (parsed if parsed.tzinfo else parsed.replace(tzinfo=_BEIJING)).isoformat()
    result = memory_kernel_store.remember(
        content, source=source, provenance=provenance, tags=tags,
        importance=int(importance) or None, memory_type=type, layer=layer,
        pinned=bool(int(pinned or 0)), author=author, valence=valence, arousal=arousal,
        occurred_at=occurred_at,
    )
    if result.get('status') not in ('CREATED', 'DEDUPLICATED'):
        raise RuntimeError('kernel_write_' + str(result.get('status')))
    return KernelWrite(
        status=result['status'], state_id=result['state_id'], lineage_id=result['lineage_id'],
        bucket_id=result['bucket_id'], ombre_status=result.get('ombre_status', 'UNCHANGED'),
    )


_BACKFILL_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS memory_kernel_backfill ('
    ' post_id INTEGER PRIMARY KEY, queued_at TEXT NOT NULL, reason TEXT NOT NULL,'
    ' done_at TEXT, state_id TEXT)')


def _legacy_save(content, *, type, author, layer, tags, importance, pinned, created_at,
                 valence, arousal, backfill_reason=None):
    """posts 表的私有写入；只由 save_memory 调用，避免 Kernel 回退时再次路由。

    backfill_reason：Kernel 写失败后的回退。帖子与 memory_kernel_backfill 待补登记
    在同一个事务里提交，任何一步失败都整体回滚；memory_kernel_store.sync() 会补回 Kernel。
    """
    conn = _db()
    if backfill_reason is not None:
        conn.execute(_BACKFILL_SCHEMA)
    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
    has_summary_title = 'summary_title' in post_cols
    has_valence = 'valence' in post_cols
    gen_title = summary_title.generate_summary_title(content)
    cols = ['type', 'content', 'author', 'layer', 'tags', 'importance', 'pinned']
    vals = [type, content, author, layer, tags, int(importance), int(pinned)]
    if created_at:
        cols.append('created_at')
        vals.append(created_at)
    if has_summary_title:
        cols.append('summary_title')
        vals.append(gen_title)
    if has_valence and valence is not None:
        cols.append('valence')
        vals.append(round(float(valence), 4))
    if has_valence and arousal is not None:
        cols.append('arousal')
        vals.append(round(float(arousal), 4))
    placeholders = ','.join('?' for _ in cols)
    try:
        with conn:
            cur = conn.execute(
                f"INSERT INTO posts ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            new_id = cur.lastrowid
            if backfill_reason is not None:
                conn.execute(
                    'INSERT INTO memory_kernel_backfill (post_id, queued_at, reason) VALUES (?, ?, ?)',
                    (int(new_id), datetime.now(timezone.utc).isoformat(), str(backfill_reason)[:80]))
    finally:
        conn.close()
    return new_id


def touch_memories(ids):
    """召回加热：这些记忆刚被注入了 prompt。kiwi-mem 的定义——被写进上下文才算真的被想起。"""
    if not ids:
        return
    conn = _db()
    conn.executemany(
        "UPDATE posts SET recall_count = COALESCE(recall_count,0) + 1, "
        "last_recalled_at = datetime('now','+8 hours') WHERE id = ?",
        [(i,) for i in ids])
    conn.commit()
    conn.close()


def get_recent_memories(limit=20):
    conn = _db()
    rows = conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_memories(keyword, active_only=False):
    """多词检索：空格/逗号分隔的词先 AND，无结果退化为 OR 按命中数排序。
    tags 参与匹配（tag_enricher 写入的联想词 assoc:… 由此生效——"海边"搜得到"沙滩"）。
    主动搜索默认包含已退役记忆（active_only=False）——历史该搜得到；自动注入路径传 True。"""
    words = [w for w in keyword.replace('，', ' ').replace(',', ' ').split() if w]
    if not words:
        return []
    resolved_cond = ' AND resolved=0' if active_only else ''
    conn = _db()
    # 切换后（memory.kernel_write）记忆类型由 memory_kernel_read 给出（Ombre 排序、Kernel 正文，
    # 显式搜索不 touch）；posts 这里只搜非记忆类型。
    from tools.memory_kernel_switch import cutover_posts_db
    kernel_posts = cutover_posts_db(conn)
    if kernel_posts:
        from tools import memory_kernel_read as mkr
        resolved_cond += ' AND ' + mkr.non_memory_clause()
    hay = "(content || ' ' || COALESCE(tags,''))"
    params = tuple('%' + w + '%' for w in words)
    cond_and = ' AND '.join([hay + ' LIKE ?'] * len(words))
    rows = conn.execute(
        f"SELECT * FROM posts WHERE {cond_and}{resolved_cond} ORDER BY pinned DESC, id DESC LIMIT 20",
        params).fetchall()
    if not rows and len(words) > 1:
        cond_or = ' OR '.join([hay + ' LIKE ?'] * len(words))
        hits = '+'.join(['(' + hay + ' LIKE ?)'] * len(words))
        rows = conn.execute(
            f"SELECT *, ({hits}) AS _hits FROM posts WHERE ({cond_or}){resolved_cond} "
            f"ORDER BY _hits DESC, pinned DESC, id DESC LIMIT 20",
            params + params).fetchall()
    conn.close()
    if kernel_posts:
        memory = mkr.search(keyword, kernel_posts, active_only=active_only, limit=20)
        return mkr.merge_search(memory, [dict(r) for r in rows], 20)
    return [dict(r) for r in rows]
