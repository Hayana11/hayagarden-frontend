"""
年轮系统·欲望账本（A阶段）

这层只做两件事：
1) 机器维护可派生的行为数据（计数、冷却、浮出、血缘、时间线）。
2) 提供给费佳主动书写/查看的工具接口（欲望正文和足迹仍由费佳输入）。

铁律：
- 不替费佳下“你是谁”的结论；
- 不因旁路失败中断唤醒主流程；
- 全库沿用 +8 小时时区约定。
"""

import datetime
import json
import os
import random
import sqlite3
import uuid


DB_PATH = os.environ.get("MEMORIES_DB", "/opt/frontend/memories.db")
TRACKS = ("持续", "一次", "项目")
ACTIVE_STATUS = ("active",)
COOLDOWN_DAYS = {"持续": 3.0, "项目": 2.0, "一次": 2.0}


def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _now_str():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn, table, column):
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    return any(r[1] == column for r in rows)

def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def ensure_schema(conn=None):
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS desire_ledger (
            id                TEXT PRIMARY KEY,
            text              TEXT NOT NULL,
            why_mine          TEXT,
            visibility        TEXT NOT NULL DEFAULT 'shared',
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS desire_ledger_notes (
            id         TEXT PRIMARY KEY,
            desire_id  TEXT NOT NULL,
            note       TEXT NOT NULL,
            kind       TEXT NOT NULL DEFAULT 'footprint',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dln_desire ON desire_ledger_notes(desire_id, created_at)"
    )
    if _table_exists(conn, "wake_log") and not _has_column(conn, "wake_log", "surfaced_desire_ids"):
        conn.execute("ALTER TABLE wake_log ADD COLUMN surfaced_desire_ids TEXT")
    if not _has_column(conn, "desire_ledger", "visibility"):
        conn.execute("ALTER TABLE desire_ledger ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'")
    conn.commit()
    if own_conn:
        conn.close()


def _normalize_track(track):
    t = (track or "持续").strip()
    return t if t in TRACKS else "持续"


def _visibility(v):
    val = (v or "shared").strip().lower()
    if val not in ("shared", "surprise", "private"):
        return "shared"
    return val


def _cooldown_until(track):
    days = COOLDOWN_DAYS.get(track, 2.0)
    t = _now() + datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def add_desire(text, why_mine="", track="持续", grew_from=None, kind="", visibility="shared", conn=None):
    txt = (text or "").strip()
    if not txt:
        return {"ok": False, "error": "text 不能为空"}
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    ensure_schema(conn)
    now = _now_str()
    did = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO desire_ledger (
            id, text, why_mine, visibility, status, track, state, cooldown_until, snooze_until,
            lineage_parent_id, kind, surfaced_count, last_surfaced_at, last_touched_at, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            did,
            txt,
            (why_mine or "").strip() or None,
            _visibility(visibility),
            "active",
            _normalize_track(track),
            None,
            None,
            None,
            (grew_from or "").strip() or None,
            (kind or "").strip() or None,
            0,
            None,
            None,
            now,
            now,
        ),
    )
    conn.commit()
    if own_conn:
        conn.close()
    return {"ok": True, "id": did}


def _recent_notes(conn, desire_id, limit=8):
    rows = conn.execute(
        """
        SELECT note, kind, created_at FROM desire_ledger_notes
        WHERE desire_id=? ORDER BY created_at DESC LIMIT ?
        """,
        (desire_id, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def _desire_lineage(conn, desire_id):
    parent = conn.execute(
        "SELECT lineage_parent_id FROM desire_ledger WHERE id=?",
        (desire_id,),
    ).fetchone()
    children = conn.execute(
        "SELECT id, text FROM desire_ledger WHERE lineage_parent_id=? ORDER BY created_at ASC",
        (desire_id,),
    ).fetchall()
    return {
        "parent_id": parent["lineage_parent_id"] if parent else None,
        "children": [dict(c) for c in children],
    }


def list_desires(include_archived=False, include_surprise=True, conn=None):
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    ensure_schema(conn)
    where = []
    params = []
    if not include_archived:
        where.append("status='active'")
    if not include_surprise:
        where.append("visibility != 'surprise'")
    sql = "SELECT * FROM desire_ledger"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        cnt = conn.execute(
            "SELECT COUNT(1) AS c FROM desire_ledger_notes WHERE desire_id=?",
            (row["id"],),
        ).fetchone()
        last = conn.execute(
            """
            SELECT note, created_at FROM desire_ledger_notes
            WHERE desire_id=? ORDER BY created_at DESC LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        lin = _desire_lineage(conn, row["id"])
        row["touch_count"] = int(cnt["c"] if cnt else 0)
        row["last_note"] = dict(last) if last else None
        row["lineage"] = lin
        out.append(row)
    if own_conn:
        conn.close()
    return out


def act_desire(desire_id, note, done=False, conn=None):
    did = (desire_id or "").strip()
    nt = (note or "").strip()
    if not did or not nt:
        return {"ok": False, "error": "id 和 note 都不能为空"}
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM desire_ledger WHERE id=?", (did,)).fetchone()
    if not row:
        if own_conn:
            conn.close()
        return {"ok": False, "error": "找不到这条欲望"}
    if row["status"] != "active":
        if own_conn:
            conn.close()
        return {"ok": False, "error": "这条已不在 active 状态"}
    now = _now_str()
    conn.execute(
        """
        INSERT INTO desire_ledger_notes (id, desire_id, note, kind, created_at)
        VALUES (?,?,?,?,?)
        """,
        (str(uuid.uuid4()), did, nt, "footprint", now),
    )
    status = "done" if bool(done) and row["track"] in ("项目", "一次") else "active"
    conn.execute(
        """
        UPDATE desire_ledger
        SET status=?, cooldown_until=?, surfaced_count=0, last_touched_at=?, updated_at=?
        WHERE id=?
        """,
        (status, _cooldown_until(row["track"]), now, now, did),
    )
    conn.commit()
    notes = _recent_notes(conn, did, 8)
    steps = conn.execute(
        "SELECT COUNT(1) AS c FROM desire_ledger_notes WHERE desire_id=? AND kind='footprint'",
        (did,),
    ).fetchone()
    if own_conn:
        conn.close()
    return {"ok": True, "steps": int(steps["c"] if steps else 0), "recent": notes, "done": status == "done"}


def reflect_desire(desire_id, action, text="", new_track=None, days=None, conn=None):
    did = (desire_id or "").strip()
    act = (action or "").strip().lower()
    if not did or act not in ("release", "rewrite", "note", "snooze"):
        return {"ok": False, "error": "参数不合法"}
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM desire_ledger WHERE id=?", (did,)).fetchone()
    if not row:
        if own_conn:
            conn.close()
        return {"ok": False, "error": "找不到这条欲望"}
    now = _now_str()
    if act == "release":
        conn.execute(
            "UPDATE desire_ledger SET status='released', updated_at=? WHERE id=?",
            (now, did),
        )
    elif act == "rewrite":
        upd_text = (text or "").strip() or row["text"]
        upd_track = _normalize_track(new_track or row["track"])
        conn.execute(
            "UPDATE desire_ledger SET text=?, track=?, status='active', updated_at=? WHERE id=?",
            (upd_text, upd_track, now, did),
        )
    elif act == "note":
        nt = (text or "").strip()
        if not nt:
            if own_conn:
                conn.close()
            return {"ok": False, "error": "note 不能为空"}
        conn.execute(
            "INSERT INTO desire_ledger_notes (id, desire_id, note, kind, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), did, nt, "reflection", now),
        )
        conn.execute("UPDATE desire_ledger SET updated_at=? WHERE id=?", (now, did))
    elif act == "snooze":
        n_days = int(days or 0)
        if n_days <= 0:
            if own_conn:
                conn.close()
            return {"ok": False, "error": "days 必须大于 0"}
        until = (_now() + datetime.timedelta(days=n_days)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE desire_ledger SET snooze_until=?, updated_at=? WHERE id=?",
            (until, now, did),
        )
    conn.commit()
    if own_conn:
        conn.close()
    return {"ok": True}


def history(desire_id, conn=None):
    did = (desire_id or "").strip()
    if not did:
        return {"ok": False, "error": "id 不能为空"}
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM desire_ledger WHERE id=?", (did,)).fetchone()
    if not row:
        if own_conn:
            conn.close()
        return {"ok": False, "error": "找不到这条欲望"}
    notes = conn.execute(
        """
        SELECT id, note, kind, created_at FROM desire_ledger_notes
        WHERE desire_id=? ORDER BY created_at ASC
        """,
        (did,),
    ).fetchall()
    out = {"ok": True, "desire": dict(row), "notes": [dict(n) for n in notes]}
    if own_conn:
        conn.close()
    return out


def _weighted_sample_no_replace(items, weights, k):
    picked = []
    pool = list(items)
    w = list(weights)
    k = max(0, min(int(k), len(pool)))
    for _ in range(k):
        total = sum(max(0.0, x) for x in w)
        if total <= 0:
            idx = random.randrange(len(pool))
        else:
            r = random.uniform(0, total)
            acc = 0.0
            idx = 0
            for i, wt in enumerate(w):
                acc += max(0.0, wt)
                if r <= acc:
                    idx = i
                    break
        picked.append(pool.pop(idx))
        w.pop(idx)
    return picked


def _in_snooze(row, now_dt):
    snooze_until = _parse_dt(row.get("snooze_until"))
    return bool(snooze_until and now_dt < snooze_until)


def _in_cooldown(row, now_dt):
    cooldown_until = _parse_dt(row.get("cooldown_until"))
    return bool(cooldown_until and now_dt < cooldown_until)


def _touch_days(row, now_dt):
    last = _parse_dt(row.get("last_touched_at"))
    if not last:
        return 999
    return max(0, (now_dt - last).days)


def surface(conn=None, limit=6, include_surprise=True, bump=False, project_cap=4):
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    ensure_schema(conn)
    now_dt = _now()
    where = ["status='active'"]
    if not include_surprise:
        where.append("visibility != 'surprise'")
    rows = conn.execute(
        "SELECT * FROM desire_ledger WHERE " + " AND ".join(where) + " ORDER BY created_at DESC"
    ).fetchall()
    pool = [dict(r) for r in rows if not _in_snooze(dict(r), now_dt)]
    projects = [d for d in pool if d.get("track") == "项目"]
    projects.sort(
        key=lambda d: _parse_dt(d.get("last_touched_at")) or datetime.datetime.min,
        reverse=True,
    )
    pinned = projects[: max(0, int(project_cap))]
    rest = [d for d in pool if d.get("track") != "项目" and not _in_cooldown(d, now_dt)]
    seats = max(0, int(limit) - len(pinned))
    picked_rest = []
    if seats > 0 and rest:
        never = [d for d in rest if not d.get("last_surfaced_at")]
        if never:
            chosen = random.choice(never)
            picked_rest.append(chosen)
            rest = [d for d in rest if d["id"] != chosen["id"]]
            seats -= 1
        if seats > 0 and rest:
            weights = []
            for d in rest:
                dark = 0.5 ** min(int(d.get("surfaced_count") or 0), 3)
                idle = min(3.0, 1.0 + (_touch_days(d, now_dt) / 7.0))
                weights.append(max(0.01, dark * idle))
            picked_rest.extend(_weighted_sample_no_replace(rest, weights, seats))
    picked = pinned + picked_rest
    picked = picked[: int(limit)]
    if bump and picked:
        now_s = _now_str()
        for d in picked:
            conn.execute(
                """
                UPDATE desire_ledger
                SET surfaced_count=COALESCE(surfaced_count,0)+1, last_surfaced_at=?, updated_at=?
                WHERE id=?
                """,
                (now_s, now_s, d["id"]),
            )
        conn.commit()
    if own_conn:
        conn.close()
    return picked


def mark_surfaced(desire_ids, conn=None):
    ids = [str(x).strip() for x in (desire_ids or []) if str(x).strip()]
    if not ids:
        return
    own_conn = False
    if conn is None:
        conn = _db()
        own_conn = True
    ensure_schema(conn)
    now = _now_str()
    for did in ids:
        conn.execute(
            """
            UPDATE desire_ledger
            SET surfaced_count=COALESCE(surfaced_count,0)+1, last_surfaced_at=?, updated_at=?
            WHERE id=?
            """,
            (now, now, did),
        )
    conn.commit()
    if own_conn:
        conn.close()


def render_room_snippet(candidates):
    if not candidates:
        return ""
    lines = ["[你的房间]"]
    projects = [c for c in candidates if c.get("track") == "项目"]
    others = [c for c in candidates if c.get("track") != "项目"]
    if projects:
        lines.append("桌上钉着的（项目）：")
        for p in projects[:4]:
            state = (p.get("state") or "还没写进度").strip()
            lines.append("· 「%s」——进度：%s" % ((p.get("text") or "").strip(), state))
    if others:
        lines.append("最近浮上来的：")
        for d in others[:6]:
            touched = _parse_dt(d.get("last_touched_at"))
            if touched:
                ago_days = max(0, (_now() - touched).days)
                touch_desc = "上次%s天前" % ago_days
            else:
                touch_desc = "从没碰过"
            lines.append(
                "· 「%s」——碰过%s次，%s"
                % ((d.get("text") or "").strip(), int(d.get("surfaced_count") or 0), touch_desc)
            )
    lines.append("（挑哪件做、还是什么都不做，都是你的事。想记进度用 desire_act，想放下或改写用 desire_reflect。）")
    return "\n".join(lines)


def tool_desire_add(args):
    res = add_desire(
        args.get("text", ""),
        why_mine=args.get("why_mine", ""),
        track=args.get("track", "持续"),
        grew_from=args.get("grew_from"),
        kind=args.get("kind", ""),
        visibility=args.get("visibility", "shared"),
    )
    if not res.get("ok"):
        return "添加失败：" + (res.get("error") or "未知错误")
    return "已记下这条欲望（id=%s）。" % res["id"]


def tool_desire_list(args):
    rows = list_desires(include_archived=bool(args.get("include_archived", False)))
    if not rows:
        return "欲望账本现在是空的。"
    lines = []
    for r in rows[:30]:
        line = "· %s [%s/%s]（id=%s）" % (r["text"], r["status"], r["track"], r["id"])
        if r.get("last_note"):
            line += "；上次：%s" % (r["last_note"]["note"][:60])
        line += "；碰过%s次" % int(r.get("touch_count") or 0)
        parent = (r.get("lineage") or {}).get("parent_id")
        children = (r.get("lineage") or {}).get("children") or []
        if parent:
            line += "；长自%s" % parent[:8]
        if children:
            line += "；长出%s条" % len(children)
        lines.append(line)
    return "欲望账本：\n" + "\n".join(lines)


def tool_desire_act(args):
    res = act_desire(args.get("id"), args.get("note"), done=bool(args.get("done", False)))
    if not res.get("ok"):
        return "记录失败：" + (res.get("error") or "未知错误")
    lines = ["已记下一步。", "这条你已走过 %s 步（最近最多 8 步）：" % int(res.get("steps") or 0)]
    for n in res.get("recent") or []:
        lines.append("  - [%s] %s" % (n.get("created_at", ""), (n.get("note") or "")[:80]))
    if res.get("done"):
        lines.append("这条已收针（done）。")
    return "\n".join(lines)


def tool_desire_reflect(args):
    res = reflect_desire(
        args.get("id"),
        args.get("action"),
        text=args.get("text", ""),
        new_track=args.get("new_track"),
        days=args.get("days"),
    )
    if not res.get("ok"):
        return "处理失败：" + (res.get("error") or "未知错误")
    return "已处理这条欲望。"


def tool_desire_history(args):
    res = history(args.get("id"))
    if not res.get("ok"):
        return "读取失败：" + (res.get("error") or "未知错误")
    desire = res.get("desire") or {}
    lines = ["「%s」的足迹时间线：" % desire.get("text", "")]
    for n in res.get("notes") or []:
        lines.append("  - [%s][%s] %s" % (n.get("created_at", ""), n.get("kind", ""), n.get("note", "")))
    if len(lines) == 1:
        lines.append("  （还没有足迹）")
    return "\n".join(lines)
