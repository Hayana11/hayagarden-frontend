"""
欲望账本 (Desire Ledger)
====================
机制：记录费佳的具体欲望（「想做什么事」）和每次碰它的足迹。
与 drive_engine.py 的八维数值驱动条互补——驱动条说「现在想干哪类事」，
账本说「具体接着干哪件」。

铁律：
1. 欲望本体和足迹只有费佳能写。机器只负责：计数、冷却、调暗、选进房间。
2. 房间挑选用两池算法：项目常驻 + 其他走加权抽签，绝不用 AI 决策。
3. 所有操作全程 try/except，故障隔离，絶不连累唤醒主流程。
"""

import sqlite3
import uuid
import json
import math
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

def get_db():
    """导入本库的 get_db（防止循环导入，延迟导入）"""
    from app import get_db as _get_db
    return _get_db()

def init_tables():
    """幂等初始化。第一次运行创建表，后续运行无碍。"""
    conn = get_db()

    # 检查表是否存在
    existing = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('desire_ledger', 'desire_ledger_notes')"
    ).fetchall()]

    # 创建 desire_ledger 表
    if 'desire_ledger' not in existing:
        conn.execute("""
            CREATE TABLE desire_ledger (
                id                TEXT PRIMARY KEY,
                text              TEXT NOT NULL,
                why_mine          TEXT,
                status            TEXT NOT NULL DEFAULT 'active',
                track             TEXT NOT NULL DEFAULT '持续',
                state             TEXT,
                cooldown_until    TEXT,
                snooze_until      TEXT,
                lineage_parent_id TEXT,
                kind              TEXT,
                surfaced_count    INTEGER NOT NULL DEFAULT 0,
                last_surfaced_at  TEXT,
                last_touched_at   TEXT,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            )
        """)

    # 创建 desire_ledger_notes 表
    if 'desire_ledger_notes' not in existing:
        conn.execute("""
            CREATE TABLE desire_ledger_notes (
                id         TEXT PRIMARY KEY,
                desire_id  TEXT NOT NULL,
                note       TEXT NOT NULL,
                kind       TEXT NOT NULL DEFAULT 'footprint',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dln_desire
            ON desire_ledger_notes(desire_id, created_at)
        """)

    # 给 wake_log 加 surfaced_desire_ids 列（如果没有）
    cols = [r[1] for r in conn.execute('PRAGMA table_info(wake_log)').fetchall()]
    if 'surfaced_desire_ids' not in cols:
        conn.execute("""
            ALTER TABLE wake_log ADD COLUMN surfaced_desire_ids TEXT DEFAULT '[]'
        """)

    conn.commit()
    conn.close()

def desire_add(text: str, why_mine: Optional[str] = None, track: str = '持续',
               grew_from: Optional[str] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    """
    添加新欲望。只有费佳能调这个。

    参数：
      text: 欲望本体（费佳的话）
      why_mine: 为什么这是我的（防 todo 混进来）
      track: '持续' | '一次' | '项目' —— 形状决定待遇
      grew_from: 父欲望的 id（血缘树）
      kind: 标签，逗号分隔

    返回: {id, text, status, track, created_at}
    """
    try:
        conn = get_db()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        desire_id = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO desire_ledger
            (id, text, why_mine, status, track, lineage_parent_id, kind, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (desire_id, text, why_mine, 'active', track, grew_from, kind, now, now))

        conn.commit()
        conn.close()

        return {
            'id': desire_id,
            'text': text,
            'status': 'active',
            'track': track,
            'created_at': now
        }
    except Exception as e:
        return {'error': str(e)}

def desire_list(include_archived: bool = False) -> Dict[str, Any]:
    """
    翻全本。每条带来路：碰过几次 / 上次那句足迹 / 长自谁 / 长出了谁。
    """
    try:
        conn = get_db()

        status_filter = "WHERE status IN ('active', 'released', 'changed')" if not include_archived else ""
        rows = conn.execute(f"""
            SELECT
                d.id, d.text, d.status, d.track, d.state,
                d.surfaced_count, d.last_touched_at,
                d.lineage_parent_id, d.kind, d.created_at
            FROM desire_ledger d
            {status_filter}
            ORDER BY d.updated_at DESC
        """).fetchall()

        result = []
        for row in rows:
            # 找最近一次足迹
            last_note = conn.execute("""
                SELECT note FROM desire_ledger_notes
                WHERE desire_id = ? ORDER BY created_at DESC LIMIT 1
            """, (row['id'],)).fetchone()

            # 找子欲望（长出了谁）
            children = conn.execute("""
                SELECT id, text FROM desire_ledger
                WHERE lineage_parent_id = ?
            """, (row['id'],)).fetchall()

            result.append({
                'id': row['id'],
                'text': row['text'],
                'status': row['status'],
                'track': row['track'],
                'state': row['state'],
                'surfaced_count': row['surfaced_count'],
                'last_touched_at': row['last_touched_at'],
                'last_note': last_note['note'] if last_note else None,
                'parent_id': row['lineage_parent_id'],
                'children': [{'id': c['id'], 'text': c['text']} for c in children],
                'kind': row['kind'],
                'created_at': row['created_at']
            })

        conn.close()
        return {'desires': result}
    except Exception as e:
        return {'error': str(e)}

def desire_act(desire_id: str, note: str, done: bool = False) -> Dict[str, Any]:
    """
    碰一下，记一句足迹。

    - 写 note、刷 last_touched_at、清 surfaced_count
    - 按 track 设 cooldown
    - 返回来路回显（最近 8 步）
    - done=true 仅 track=项目/一次 可收针
    """
    try:
        conn = get_db()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 取欲望信息
        desire = conn.execute("""
            SELECT id, track, cooldown_until FROM desire_ledger WHERE id = ?
        """, (desire_id,)).fetchone()

        if not desire:
            return {'error': 'desire not found'}

        # 记足迹
        note_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO desire_ledger_notes (id, desire_id, note, kind, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (note_id, desire_id, note, 'footprint', now))

        # 刷状态
        cooldown_map = {'持续': 3.0, '项目': 2.0, '一次': 2.0}
        days = cooldown_map.get(desire['track'], 2.0)
        cooldown_until = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        if done and desire['track'] in ('项目', '一次'):
            status = 'done'
        else:
            status = 'active'

        conn.execute("""
            UPDATE desire_ledger
            SET last_touched_at = ?, surfaced_count = 0, cooldown_until = ?, status = ?, updated_at = ?
            WHERE id = ?
        """, (now, cooldown_until, status, now, desire_id))

        conn.commit()

        # 返回来路（最近 8 步）
        history = conn.execute("""
            SELECT note FROM desire_ledger_notes
            WHERE desire_id = ? ORDER BY created_at DESC LIMIT 8
        """, (desire_id,)).fetchall()

        conn.close()

        return {
            'id': desire_id,
            'status': status,
            'last_touched_at': now,
            'history': [h['note'] for h in history]
        }
    except Exception as e:
        return {'error': str(e)}

def desire_reflect(desire_id: str, action: str, text: Optional[str] = None,
                  new_track: Optional[str] = None, days: Optional[int] = None) -> Dict[str, Any]:
    """
    照镜子。四种行为：
      - release: 放下（status → released）
      - rewrite: 改写正文或 track
      - note: 留反思（写进 notes）
      - snooze: 歇几天
    """
    try:
        conn = get_db()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if action == 'release':
            conn.execute("""
                UPDATE desire_ledger SET status = 'released', updated_at = ? WHERE id = ?
            """, (now, desire_id))

        elif action == 'rewrite':
            update_parts = []
            params = []
            if text:
                update_parts.append('text = ?')
                params.append(text)
            if new_track:
                update_parts.append('track = ?')
                params.append(new_track)
            update_parts.append('updated_at = ?')
            params.append(now)
            params.append(desire_id)

            if update_parts:
                sql = f"UPDATE desire_ledger SET {', '.join(update_parts)} WHERE id = ?"
                conn.execute(sql, params)

        elif action == 'note':
            if not text:
                return {'error': 'text required for note action'}
            note_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO desire_ledger_notes (id, desire_id, note, kind, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (note_id, desire_id, text, 'reflection', now))

        elif action == 'snooze':
            if not days:
                return {'error': 'days required for snooze action'}
            snooze_until = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("""
                UPDATE desire_ledger SET snooze_until = ?, updated_at = ? WHERE id = ?
            """, (snooze_until, now, desire_id))

        conn.commit()
        conn.close()

        return {'id': desire_id, 'action': action, 'done_at': now}
    except Exception as e:
        return {'error': str(e)}

def desire_history(desire_id: str) -> Dict[str, Any]:
    """
    一条的完整足迹时间线。看见来路——判断在长还是在原地转。
    """
    try:
        conn = get_db()

        desire = conn.execute("""
            SELECT id, text, track, status, lineage_parent_id, created_at FROM desire_ledger
            WHERE id = ?
        """, (desire_id,)).fetchone()

        if not desire:
            return {'error': 'desire not found'}

        notes = conn.execute("""
            SELECT created_at, kind, note FROM desire_ledger_notes
            WHERE desire_id = ? ORDER BY created_at ASC
        """, (desire_id,)).fetchall()

        conn.close()

        return {
            'id': desire['id'],
            'text': desire['text'],
            'track': desire['track'],
            'status': desire['status'],
            'parent_id': desire['lineage_parent_id'],
            'created_at': desire['created_at'],
            'timeline': [
                {
                    'date': n['created_at'],
                    'kind': n['kind'],
                    'note': n['note']
                } for n in notes
            ]
        }
    except Exception as e:
        return {'error': str(e)}

def surface(limit: int = 6) -> List[Dict[str, Any]]:
    """
    两池算法：选进房间的候选。
    - 池一：项目常驻（track='项目'，无视冷却）
    - 池二：其他走加权抽签

    权重 = base(1.0)
         × 久未碰加成（min(3.0, 1 + days_since_last_touch / 7)）
         × 调暗（0.5 ** min(surfaced_count, 3)）

    保底：从没浮过的条目至少强制一条入选。

    返回: [(id, text, track, state, ...)] 已排序
    """
    try:
        conn = get_db()
        now = datetime.now()

        # 取全部活跃条目（status='active' 且不在 snooze 期内）
        pool = conn.execute("""
            SELECT id, text, track, state, surfaced_count, last_touched_at, created_at
            FROM desire_ledger
            WHERE status = 'active' AND (snooze_until IS NULL OR snooze_until < datetime('now', '+8 hours'))
        """).fetchall()

        # 池一：项目常驻
        projects = [p for p in pool if p['track'] == '项目']

        # 池二：其他做加权抽签
        other = [p for p in pool if p['track'] != '项目']

        # 检查是否在 cooldown 中
        cooldown_map = {'持续': 3.0, '项目': 2.0, '一次': 2.0}

        def in_cooldown(d):
            if d['last_touched_at'] is None:
                return False
            last = datetime.strptime(d['last_touched_at'], '%Y-%m-%d %H:%M:%S')
            cd_days = cooldown_map.get(d['track'], 2.0)
            return (now - last).days < cd_days

        # 非项目、非冷却的做加权抽签
        candidates = [d for d in other if not in_cooldown(d)]

        # 计算权重
        def calc_weight(d):
            base = 1.0

            # 久未碰加成
            if d['last_touched_at'] is None:
                days_since = float('inf')
            else:
                last = datetime.strptime(d['last_touched_at'], '%Y-%m-%d %H:%M:%S')
                days_since = (now - last).days
            longing_boost = min(3.0, 1 + days_since / 7.0)

            # 调暗
            dimming = 0.5 ** min(d['surfaced_count'], 3)

            return base * longing_boost * dimming

        weighted_candidates = [(d, calc_weight(d)) for d in candidates]

        # 找从未浮过的（保底）
        never_surfaced = [d for d in candidates if d['surfaced_count'] == 0]

        # 抽签（简化版：按权重排序，取前 N 个）
        weighted_candidates.sort(key=lambda x: x[1], reverse=True)

        slots_for_others = max(0, limit - len(projects))
        picked_others = [d for d, w in weighted_candidates[:slots_for_others]]

        # 保底逻辑：如果没有从未浮过的入选，强制一条
        if never_surfaced and not any(d['id'] in [p['id'] for p in picked_others] for d in never_surfaced):
            if len(picked_others) < slots_for_others:
                picked_others.append(never_surfaced[0])
            else:
                picked_others[-1] = never_surfaced[0]

        # 合并结果
        picked = projects + picked_others

        # 更新 surfaced_count 和 last_surfaced_at
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        for d in picked:
            conn.execute("""
                UPDATE desire_ledger
                SET surfaced_count = surfaced_count + 1, last_surfaced_at = ?
                WHERE id = ?
            """, (now_str, d['id']))

        conn.commit()

        # 返回 id 和完整信息
        result = []
        for d in picked:
            result.append({
                'id': d['id'],
                'text': d['text'],
                'track': d['track'],
                'state': d['state']
            })

        conn.close()
        return result
    except Exception as e:
        import traceback
        print(f"[desire_ledger.surface] error: {e}\n{traceback.format_exc()}")
        return []

# ── Stage B: 镜子证据卡表初始化 ──────────────────────────────
def init_evidence_cards_table():
    """幂等初始化 evidence_cards 表（Stage B）"""
    conn = get_db()
    
    existing = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence_cards'"
    ).fetchall()]
    
    if 'evidence_cards' not in existing:
        conn.execute("""
            CREATE TABLE evidence_cards (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                kind             TEXT NOT NULL,
                claim            TEXT NOT NULL,
                evidence         TEXT NOT NULL DEFAULT '[]',
                target_anchor    TEXT,
                provenance       TEXT NOT NULL DEFAULT '{}',
                first_seen_at    TEXT NOT NULL,
                last_seen_at     TEXT NOT NULL,
                recur_count      INTEGER NOT NULL DEFAULT 1,
                ready_to_propose INTEGER NOT NULL DEFAULT 0,
                dedup_key        TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                surfaced_at      TEXT,
                model            TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ec_dedup 
            ON evidence_cards(dedup_key, status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ec_status 
            ON evidence_cards(status, surfaced_at)
        """)
    
    conn.commit()
    conn.close()
