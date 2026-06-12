#!/usr/bin/env python3.11
"""
Memory cleaner: dedup / auto-tag / importance / resolve / Ombre Brain sync
Runs nightly at 03:00
"""
import sqlite3, json, re, datetime, os, sys, time
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
# Ombre Brain sync — batch hold via server.hold()
# ─────────────────────────────────────────────────────────────
import asyncio as _asyncio

async def _batch_hold(sync_items):
    """
    Call server.hold() for each item in ONE shared event loop.
    Importing inside the coroutine so module-level server init
    (BucketManager / Dehydrator / DecayEngine) runs inside the loop.
    """
    sys.path.insert(0, '/opt/ombre-brain')
    from server import hold

    results = []
    for item in sync_items:
        try:
            r = await hold(
                content    = item['content'],
                tags       = item['tags_str'],
                importance = item['importance'],
                pinned     = item['pinned'],
            )
            results.append((item['id'], r, None))
        except Exception as e:
            results.append((item['id'], None, str(e)))
    return results


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
                    newer_id = b['id']
                    older_id = a['id']
                    merged_c = str(result['merged']).strip()
                    conn.execute("UPDATE posts SET content=? WHERE id=?", (merged_c, newer_id))
                    conn.execute("DELETE FROM posts WHERE id=?", (older_id,))
                    conn.commit()
                    log(f'  Merged id={older_id} into id={newer_id}')
                    merged += 1
                    rows[i] = None
                    break
            except Exception as e:
                log(f'  Error comparing id={a["id"]} & id={b["id"]}: {e}')
        rows = [r for r in rows if r is not None]

    conn.close()
    log(f'  Done: checked {checked} pairs, merged {merged}')


# ─────────────────────────────────────────────────────────────
# Task 2 · Auto-tag  (skips already-processed records)
# ─────────────────────────────────────────────────────────────
def auto_tag():
    log('=== Task 2: Auto-tag ===')
    conn = get_db()
    rows = conn.execute(
        "SELECT id, content FROM posts WHERE (tags IS NULL OR tags='') AND processed=0"
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
# Task 3 · Importance scoring  (skips already-processed records)
# ─────────────────────────────────────────────────────────────
def score_importance():
    log('=== Task 3: Importance scoring ===')
    conn = get_db()
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN importance INTEGER DEFAULT 0")
        conn.commit()
        log('  Added importance column')
    except Exception:
        pass

    rows = conn.execute(
        "SELECT id, content FROM posts "
        "WHERE type='MEMORY' AND (importance IS NULL OR importance=0) AND processed=0"
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
# Task 5 · Process new memories & sync to Ombre Brain
# ─────────────────────────────────────────────────────────────
def process_and_sync():
    log('=== Task 5: Process new & sync to Ombre Brain ===')
    conn = get_db()

    rows = conn.execute(
        "SELECT id, content, type FROM posts "
        "WHERE processed=0 AND type IN ('MEMORY','DIARY','MISS')"
    ).fetchall()
    log(f'  {len(rows)} unprocessed records')

    total = len(rows)
    promoted_core = promoted_long = 0
    sync_queue = []   # collect items for batch Ombre sync

    # ── Phase 1: DeepSeek analysis + DB updates ──────────────
    for r in rows:
        try:
            prompt = (
                '分析这条记忆，返回 JSON：\n'
                '{\n'
                '  "importance": 1到10的整数,\n'
                '  "valence": -1到1的小数（情绪愉悦度，负=不愉快，正=愉快）,\n'
                '  "arousal": 0到1的小数（情绪激活度，0=平静，1=激动）,\n'
                '  "tags": "逗号分隔的标签（从：日常/技术/色色/情绪/未完成 中选1-2个）"\n'
                '}\n'
                '只返回JSON，不要其他内容。\n'
                f'记忆内容：{r["content"]}'
            )
            result = ask(prompt, expect_json=True)
            if not result:
                log(f'  id={r["id"]} API empty, skipping')
                continue

            importance = max(1, min(10, int(result.get('importance', 5))))
            valence    = max(-1.0, min(1.0, float(result.get('valence', 0.0))))
            arousal    = max(0.0,  min(1.0, float(result.get('arousal', 0.3))))
            tags_raw   = result.get('tags', '')
            valid_tags = {'日常', '技术', '色色', '情绪', '未完成'}
            tags_list  = [t.strip() for t in str(tags_raw).split(',') if t.strip() in valid_tags]
            tags_str   = ','.join(tags_list) if tags_list else '日常'

            if importance >= 8:
                layer = 'core';    promoted_core += 1
            elif importance >= 5:
                layer = 'long-term'; promoted_long += 1
            else:
                layer = 'recent'

            conn.execute(
                "UPDATE posts SET importance=?, valence=?, arousal=?, tags=?, layer=?, processed=1 "
                "WHERE id=?",
                (importance, valence, arousal, tags_str, layer, r['id'])
            )
            conn.commit()
            log(f'  id={r["id"]} → imp={importance} layer={layer} tags={tags_str}')

            sync_queue.append({
                'id':         r['id'],
                'content':    r['content'],
                'tags_str':   tags_str,
                'importance': importance,
                'pinned':     (importance >= 8),
            })
        except Exception as e:
            log(f'  id={r["id"]} DeepSeek error: {e}')

    conn.close()

    # ── Phase 2: Ombre Brain — one asyncio.run() for all records ──
    synced = 0
    if sync_queue:
        log(f'  Syncing {len(sync_queue)} records to Ombre Brain (hold)…')
        try:
            hold_results = _asyncio.run(_batch_hold(sync_queue))
            for rid, bucket_result, err in hold_results:
                if err:
                    log(f'  id={rid} Ombre hold failed: {err}')
                else:
                    log(f'  id={rid} → Ombre: {str(bucket_result)[:60]}')
                    synced += 1
        except Exception as e:
            log(f'  Ombre Brain batch error: {e}')

    log(f'  Done: processed={total}, →core={promoted_core}, →long-term={promoted_long}, ' +
        f'ombre_synced={synced}')


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
    try:
        process_and_sync()
    except Exception as e:
        log(f'Task 5 failed: {e}')
    log('====== Memory Cleaner finished ======')
