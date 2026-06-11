#!/usr/bin/env python3.11
"""
Memory cleaner: dedup / auto-tag / importance / resolve
Runs nightly at 03:00
"""
import sqlite3, json, re, datetime, os, time
import urllib.request as _req
import urllib.error as _err

DB_PATH = '/opt/frontend/memories.db'
API_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL   = 'deepseek-chat'

API_KEY = ''
for line in open('/opt/frontend/.env'):
    if line.startswith('DEEPSEEK_API_KEY='):
        API_KEY = line.split('=', 1)[1].strip()

def log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ask(prompt, expect_json=False):
    """Call DeepSeek; return stripped text or parsed JSON."""
    body = json.dumps({
        'model': MODEL,
        'max_tokens': 256,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode()
    request = _req.Request(
        API_URL,
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {API_KEY}',
        }
    )
    try:
        with _req.urlopen(request, timeout=60) as resp:
            data = json.load(resp)
        text = (data.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()
        time.sleep(0.5)
        if expect_json:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            return json.loads(m.group()) if m else {}
        return text
    except Exception as e:
        log(f'  API error: {e}')
        time.sleep(1)
        return {} if expect_json else ''


# ─────────────────────────────────────────────────────────────
# Task 1 · Deduplication
# ─────────────────────────────────────────────────────────────
def dedup():
    log('=== Task 1: Deduplication ===')
    conn = get_db()
    rows = conn.execute(
        "SELECT id, content, created_at FROM posts ORDER BY id"
    ).fetchall()
    rows = list(rows)
    log(f'  Loaded {len(rows)} records')

    def keywords(text):
        # simple CJK word split: 2-char ngrams + split on punctuation/spaces
        text = re.sub(r'[^一-鿿A-Za-z0-9]', ' ', text)
        words = set()
        tokens = text.split()
        for t in tokens:
            if len(t) >= 2:
                words.add(t)
                for i in range(len(t)-1):
                    words.add(t[i:i+2])
        return words

    checked, merged = 0, 0
    for i in range(len(rows)):
        for j in range(i+1, len(rows)):
            a, b = rows[i], rows[j]
            # rough filter: at least 3 shared keywords from first 20 chars
            ka = keywords((a['content'] or '')[:80])
            kb = keywords((b['content'] or '')[:80])
            shared = ka & kb
            if len(shared) < 3:
                continue
            checked += 1
            prompt = (
                f'以下两条记忆是否描述同一件事？如果是请合并成一条更完整的内容。\n'
                f'记忆A：{a["content"]}\n'
                f'记忆B：{b["content"]}\n'
                f'返回 JSON：{{"is_duplicate": true/false, "merged": "合并后的内容"}}'
            )
            try:
                result = ask(prompt, expect_json=True)
                if result.get('is_duplicate') and result.get('merged'):
                    # keep newer (higher id = b), update content, delete older
                    newer_id  = b['id']
                    older_id  = a['id']
                    merged_c  = str(result['merged']).strip()
                    conn.execute("UPDATE posts SET content=? WHERE id=?", (merged_c, newer_id))
                    conn.execute("DELETE FROM posts WHERE id=?", (older_id,))
                    conn.commit()
                    log(f'  Merged id={older_id} into id={newer_id}')
                    merged += 1
                    # remove older from local list to avoid double-processing
                    rows[i] = None
                    break
            except Exception as e:
                log(f'  Error comparing id={a["id"]} & id={b["id"]}: {e}')
        rows = [r for r in rows if r is not None]

    conn.close()
    log(f'  Done: checked {checked} pairs, merged {merged}')


# ─────────────────────────────────────────────────────────────
# Task 2 · Auto-tag
# ─────────────────────────────────────────────────────────────
def auto_tag():
    log('=== Task 2: Auto-tag ===')
    conn = get_db()
    rows = conn.execute(
        "SELECT id, content FROM posts WHERE (tags IS NULL OR tags='')"
    ).fetchall()
    log(f'  {len(rows)} untagged records')
    tagged = 0
    for r in rows:
        prompt = (
            f'根据这条记忆的内容，从以下分类中选1-2个最合适的标签：日常、技术、色色、情绪、未完成。\n'
            f'只返回逗号分隔的标签，不要其他内容。\n'
            f'记忆内容：{r["content"]}'
        )
        try:
            tags = ask(prompt).strip().strip('。').strip()
            # validate: keep only known tags
            valid = {'日常', '技术', '色色', '情绪', '未完成'}
            picked = [t.strip() for t in tags.split(',') if t.strip() in valid]
            if picked:
                conn.execute("UPDATE posts SET tags=? WHERE id=?", (','.join(picked), r['id']))
                conn.commit()
                log(f'  id={r["id"]} → tags: {",".join(picked)}')
                tagged += 1
        except Exception as e:
            log(f'  Error tagging id={r["id"]}: {e}')
    conn.close()
    log(f'  Done: tagged {tagged} records')


# ─────────────────────────────────────────────────────────────
# Task 3 · Importance scoring
# ─────────────────────────────────────────────────────────────
def score_importance():
    log('=== Task 3: Importance scoring ===')
    conn = get_db()
    # ensure column exists
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN importance INTEGER DEFAULT 0")
        conn.commit()
        log('  Added importance column')
    except Exception:
        pass  # already exists

    rows = conn.execute(
        "SELECT id, content FROM posts WHERE type='MEMORY' AND (importance IS NULL OR importance=0)"
    ).fetchall()
    log(f'  {len(rows)} records need scoring')
    scored = 0
    for r in rows:
        prompt = (
            f'这条记忆的重要性是多少？\n'
            f'1=普通日常，5=重要事件，10=关系基石。\n'
            f'只返回数字，不要其他内容。\n'
            f'记忆内容：{r["content"]}'
        )
        try:
            text = ask(prompt).strip()
            m = re.search(r'\b([1-9]|10)\b', text)
            if m:
                val = int(m.group())
                conn.execute("UPDATE posts SET importance=? WHERE id=?", (val, r['id']))
                conn.commit()
                log(f'  id={r["id"]} → importance={val}')
                scored += 1
        except Exception as e:
            log(f'  Error scoring id={r["id"]}: {e}')
    conn.close()
    log(f'  Done: scored {scored} records')


# ─────────────────────────────────────────────────────────────
# Task 4 · Mark resolved
# ─────────────────────────────────────────────────────────────
def mark_resolved():
    log('=== Task 4: Mark resolved ===')
    conn = get_db()
    rows = conn.execute(
        "SELECT id, content FROM posts WHERE tags LIKE '%未完成%' AND resolved=0"
    ).fetchall()
    log(f'  {len(rows)} unresolved "未完成" records')
    resolved = 0
    for r in rows:
        prompt = (
            f'这件事现在看起来是否已经完成或者不再相关？\n'
            f'背景：这是费奥多尔和哈娅的共同记忆。\n'
            f'返回 JSON：{{"resolved": true/false}}\n'
            f'记忆内容：{r["content"]}'
        )
        try:
            result = ask(prompt, expect_json=True)
            if result.get('resolved') is True:
                conn.execute("UPDATE posts SET resolved=1 WHERE id=?", (r['id'],))
                conn.commit()
                log(f'  id={r["id"]} marked resolved')
                resolved += 1
        except Exception as e:
            log(f'  Error checking id={r["id"]}: {e}')
    conn.close()
    log(f'  Done: resolved {resolved} records')


# ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    log('====== Memory Cleaner started ======')
    try:
        dedup()
    except Exception as e:
        log(f'Task 1 failed: {e}')
    try:
        auto_tag()
    except Exception as e:
        log(f'Task 2 failed: {e}')
    try:
        score_importance()
    except Exception as e:
        log(f'Task 3 failed: {e}')
    try:
        mark_resolved()
    except Exception as e:
        log(f'Task 4 failed: {e}')
    log('====== Memory Cleaner finished ======')
