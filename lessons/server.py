#!/usr/bin/env python3
import sqlite3, json, os
from mcp.server.fastmcp import FastMCP
import httpx

mcp = FastMCP("learn-from-mistakes", host="127.0.0.1", port=5055)
DB = "/opt/frontend/memories.db"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
LESSON_MODEL = os.environ.get("LESSON_MODEL", "claude-haiku-4-5-20251001")

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS lesson_candidates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_evidence TEXT NOT NULL,
        candidate_title TEXT, candidate_description TEXT, candidate_tags TEXT,
        author TEXT DEFAULT 'unknown',
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now','+8 hours'))
    );
    CREATE TABLE IF NOT EXISTS lessons(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, description TEXT NOT NULL,
        reason TEXT, tags TEXT,
        source_candidate_id INTEGER,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now','+8 hours'))
    );
    """)
    for col, table, defn in [
        ("author", "lesson_candidates", "TEXT DEFAULT 'unknown'"),
        ("reason", "lessons", "TEXT"),
        ("status", "lessons", "TEXT DEFAULT 'active'"),
    ]:
        try: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        except: pass
    c.commit(); c.close()

async def claude(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            API_URL,
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": LESSON_MODEL, "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]}
        )
        return r.json()["content"][0]["text"]

def parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return json.loads(t.strip())

def quick_match(description: str, lessons) -> list:
    words = set(description.lower().replace(',', ' ').split())
    matched = []
    for l in lessons:
        tags = set(t.strip().lower() for t in (l['tags'] or '').split(',') if t.strip())
        if tags & words or any(t in description.lower() for t in tags):
            matched.append(l)
    return matched

@mcp.tool()
async def record_evidence(description: str, context: str = "", author: str = "cc") -> str:
    """记录一次错误，自动提炼候选教训"""
    prompt = f'从错误提炼教训，只回复JSON：{{"title":"标题15字内","description":"说明60字内","tags":"tag1,tag2"}}\n错误：{description}\n上下文：{context}'
    try:
        data = parse_json(await claude(prompt))
    except:
        data = {"title": "待提炼", "description": description[:60], "tags": "未分类"}
    c = db()
    c.execute(
        "INSERT INTO lesson_candidates(raw_evidence,candidate_title,candidate_description,candidate_tags,author) VALUES(?,?,?,?,?)",
        (description, data["title"], data["description"], data.get("tags",""), author)
    )
    c.commit(); c.close()
    return f"✓ 候选已生成：「{data['title']}」— {data['description']}"

@mcp.tool()
def list_candidates() -> str:
    """查看待审核的候选教训"""
    c = db()
    rows = c.execute("SELECT id,candidate_title,candidate_description,candidate_tags,author FROM lesson_candidates WHERE status='pending' ORDER BY id DESC").fetchall()
    c.close()
    return "无待审候选。" if not rows else "\n---\n".join(
        f"[{r['id']}] {r['candidate_title']}\n{r['candidate_description']}\n标签：{r['candidate_tags']} | 来源：{r['author']}" for r in rows
    )

@mcp.tool()
def promote_lesson(candidate_id: int, reason: str = "") -> str:
    """将候选升格为正式教训，reason说明为什么有这条"""
    c = db()
    row = c.execute("SELECT * FROM lesson_candidates WHERE id=? AND status='pending'", (candidate_id,)).fetchone()
    if not row: c.close(); return f"找不到候选 {candidate_id}"
    c.execute("INSERT INTO lessons(title,description,reason,tags,source_candidate_id) VALUES(?,?,?,?,?)",
              (row["candidate_title"], row["candidate_description"], reason, row["candidate_tags"], candidate_id))
    c.execute("UPDATE lesson_candidates SET status='promoted' WHERE id=?", (candidate_id,))
    c.commit(); c.close()
    return f"✓ 「{row['candidate_title']}」已成为正式教训。"

@mcp.tool()
def reject_candidate(candidate_id: int) -> str:
    """拒绝候选教训"""
    c = db()
    r = c.execute("UPDATE lesson_candidates SET status='rejected' WHERE id=? AND status='pending'", (candidate_id,))
    c.commit(); c.close()
    return f"✓ 已拒绝。" if r.rowcount else f"找不到候选 {candidate_id}"

@mcp.tool()
def list_lessons(include_deprecated: bool = False) -> str:
    """查看正式教训，include_deprecated=True时也显示已过时的"""
    c = db()
    q = "SELECT id,title,description,reason,tags,status FROM lessons" + ("" if include_deprecated else " WHERE status='active'") + " ORDER BY id DESC"
    rows = c.execute(q).fetchall()
    c.close()
    return "教训库为空。" if not rows else "\n---\n".join(
        f"[{r['id']}][{r['status']}] {r['title']}\n{r['description']}" +
        (f"\n原因：{r['reason']}" if r['reason'] else "") +
        f"\n标签：{r['tags']}" for r in rows
    )

@mcp.tool()
def search_lesson(keyword: str) -> str:
    """按关键词搜索已有教训"""
    c = db()
    k = f"%{keyword}%"
    rows = c.execute(
        "SELECT id,title,description,reason,tags FROM lessons WHERE status='active' AND (tags LIKE ? OR title LIKE ? OR description LIKE ?) ORDER BY id DESC",
        (k, k, k)
    ).fetchall()
    c.close()
    return f"未找到含「{keyword}」的教训。" if not rows else "\n---\n".join(
        f"[{r['id']}] {r['title']}\n{r['description']}" + (f"\n原因：{r['reason']}" if r['reason'] else "") + f"\n标签：{r['tags']}"
        for r in rows
    )

@mcp.tool()
def deprecate_lesson(lesson_id: int) -> str:
    """将教训标记为已过时，不再参与validate"""
    c = db()
    r = c.execute("UPDATE lessons SET status='deprecated' WHERE id=? AND status='active'", (lesson_id,))
    c.commit(); c.close()
    return f"✓ 教训 {lesson_id} 已标为过时。" if r.rowcount else f"找不到教训 {lesson_id}"

@mcp.tool()
async def validate_edit(description: str) -> str:
    """改代码前检查是否命中已有教训（先关键词筛选，命中才调Claude）"""
    c = db()
    all_lessons = c.execute("SELECT id,title,description,reason,tags FROM lessons WHERE status='active'").fetchall()
    c.close()
    if not all_lessons: return "教训库为空，放心改。"
    candidates = quick_match(description, all_lessons)
    if not candidates: return "✓ 未命中已有教训。"
    ls = "\n".join(f"- {l['title']}：{l['description']}" for l in candidates)
    prompt = f'判断改动是否违反教训，只回复JSON：{{"hits":[],"warning":"无命中则空字符串"}}\n改动：{description}\n教训：\n{ls}'
    try:
        data = parse_json(await claude(prompt))
        return f"⚠️ {data['warning']}\n命中：{', '.join(data['hits'])}" if data.get("warning") else "✓ 未命中。"
    except:
        return "✓ 检查完成。"

if __name__ == "__main__":
    init()
    mcp.run(transport="streamable-http")
