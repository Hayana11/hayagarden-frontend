"""Presentation metadata for completed unified main-chat wakes."""
import json


def parse_response(raw):
    visible = []
    title = summary = ''
    for line in raw.splitlines():
        stripped = line.strip()
        if '[[ACTIVITY]]' in stripped:
            prefix, _, record = line.partition('[[ACTIVITY]]')
            if prefix.strip():
                visible.append(prefix)
            name, sep, body = record.strip().partition('｜')
            if sep:
                title, summary = name.strip()[:16], body.strip()[:40]
        else:
            visible.append(line)
    text = '\n'.join(visible).strip()
    silent = '[[SILENT]]' in text
    text = text.replace('[[SILENT]]', '').strip()
    return text, silent and not text, title, summary


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS wake_activity (
        wake_run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, woke_at TEXT NOT NULL,
        spoke INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
        tool_calls_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours'))
    )""")


def save(conn, rid, mode, spoke, title, summary, calls):
    ensure_schema(conn)
    tools = [{k: c.get(k) for k in ('name', 'success', 'time')} for c in calls if c.get('name')]
    conn.execute("""INSERT INTO wake_activity
        (wake_run_id, mode, woke_at, spoke, title, summary, tool_calls_json)
        SELECT ?, ?, woke_at, ?, ?, ?, ? FROM wake_log WHERE wake_run_id=?
        ON CONFLICT(wake_run_id) DO NOTHING""",
        (rid, mode, int(spoke), title, summary, json.dumps(tools, ensure_ascii=False), rid))


def list_activity(conn, since, limit, before='', before_id='', silent_only=False):
    rows = conn.execute("""SELECT wake_run_id,mode,woke_at,spoke,title,summary,tool_calls_json
        FROM wake_activity WHERE woke_at >= ? AND (?=0 OR spoke=0)
        AND (?='' OR (woke_at,wake_run_id)<(?,?))
        ORDER BY woke_at DESC,wake_run_id DESC LIMIT ?""",
        (since, int(silent_only), before, before, before_id, limit))
    return [dict(wake_run_id=r[0], mode=r[1], woke_at=r[2], spoke=bool(r[3]), title=r[4],
                 summary=r[5], tools=[c['name'] for c in json.loads(r[6])]) for r in rows]
