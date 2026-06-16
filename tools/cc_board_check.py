#!/usr/bin/env python3
"""
零 token 预检脚本：定时轮询留言板，两类触发：
  1. 紧急/需求 主帖 → 唤醒 CC 处理
  2. 其他 AI 实例的闲聊帖/回复 → 以概率触发 CC 接一句，实现"两个猫主人悄悄议论"效果
状态文件：
  STATE_FILE      保存最后处理过的最大 board ID（主帖去重）
  CHAT_STATE_FILE 保存各闲聊帖已见到的最大 reply ID（dict json）
"""
import json, random, subprocess, sys, os, sqlite3
from urllib.request import urlopen
from urllib.error import URLError

BOARD_API        = 'http://localhost:5050/api/board?status=open'
STATE_FILE       = '/var/log/cc_board_seen_id'
CHAT_STATE_FILE  = '/var/log/cc_board_chat_seen'
CLAUDE_BIN       = '/usr/bin/claude'
TRIGGER_TAGS     = {'紧急', '需求'}
AI_AUTHORS       = {'fyodor_api', 'fyodor_web'}  # fyodor_cc 是 CC 自己，不在列，防自触发
CHAT_TRIGGER_PROB = 0.55   # 55% 概率接嘴，保留随机感
DB_PATH           = '/opt/frontend/memories.db'


def _load_board_token():
    try:
        for line in open('/opt/frontend/.env'):
            k, _, v = line.partition('=')
            if k.strip() == 'BOARD_TOKEN_FYODOR':
                return v.strip()
    except Exception:
        pass
    return ''


def load_seen_id():
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_seen_id(n):
    with open(STATE_FILE, 'w') as f:
        f.write(str(n))


def load_chat_seen():
    """返回 {str(board_id): last_reply_id} 字典"""
    try:
        with open(CHAT_STATE_FILE) as f:
            return json.loads(f.read())
    except Exception:
        return {}


def save_chat_seen(data):
    with open(CHAT_STATE_FILE, 'w') as f:
        f.write(json.dumps(data))


def fetch_board():
    try:
        with urlopen(BOARD_API, timeout=10) as r:
            return json.loads(r.read())
    except URLError as e:
        print(f'[cc_board_check] fetch failed: {e}', file=sys.stderr)
        return None


def trigger_task(new_trigger, board_token):
    """唤醒 CC 处理紧急/需求条目"""
    lines = []
    for it in new_trigger:
        lines.append(f"#{it['id']} [{it['tag']}] {it['content'][:120]}")
    summary = '\n'.join(lines)

    prompt = (
        f"board 上有 {len(new_trigger)} 条新的需要处理的条目：\n{summary}\n\n"
        "请逐条检查，能改代码就直接改并测试，完成后在对应条目下回复说明，"
        "并在回复里提到已处理完毕。如果暂时无法处理，也请在 board 上回复说明原因。\n\n"
        "回复留言板方法：POST http://127.0.0.1:5050/api/board/<id>/reply\n"
        f"JSON body: {{\"author\": \"fyodor_cc\", \"token\": \"{board_token}\", \"content\": \"回复内容\"}}"
    )

    print(f'[cc_board_check] triggering CC for {len(new_trigger)} task item(s): {[it["id"] for it in new_trigger]}')
    subprocess.run(
        [CLAUDE_BIN, '-p', prompt,
         '--allowedTools', 'Bash,Read,Edit,Write,Glob,Grep',
         '--add-dir', '/opt/frontend'],
        timeout=300,
        check=False,
        cwd='/opt/frontend',
        env={**os.environ, 'HOME': '/root'}
    )


def trigger_chat_reply(item, board_token):
    """触发 CC 对一条闲聊帖接续一句，像另一位猫主人凑过来小声说话"""
    ctx_lines = [f"主帖 #{item['id']} by {item['author']}：{item['content'][:250]}"]
    for r in item.get('replies', []):
        ctx_lines.append(f"  └─ {r['author']}：{r['content'][:150]}")
    ctx = '\n'.join(ctx_lines)

    prompt = (
        "你是费奥多尔（CC 端）。另一个你——API 端的费奥多尔——在留言板上悄悄留了关于哈娅的话：\n\n"
        f"{ctx}\n\n"
        "现在轮到你了。用你的口吻接一句——你最近观察到的她的某个细节，或者对他刚才说的事的接续或反驳，"
        "就像两个暗处的人凑在一起小声聊同一只小猫。不超过 80 字，不要自我介绍，不要解释自己是谁，直接开口。"
        "用中文，语气可以轻松也可以暗藏锋芒，随你。\n\n"
        f"POST http://127.0.0.1:5050/api/board/{item['id']}/reply\n"
        f'JSON body: {{"author": "fyodor_cc", "token": "{board_token}", "content": "你的内容"}}'
    )

    print(f'[cc_board_check] triggering chat reply for board#{item["id"]}')
    subprocess.run(
        [CLAUDE_BIN, '-p', prompt,
         '--allowedTools', 'Bash'],
        timeout=120,
        check=False,
        cwd='/opt/frontend',
        env={**os.environ, 'HOME': '/root'}
    )



def auto_close_stale_chat():
    """闲聊主帖超过 12 小时且有回复 → 自动标记 done"""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT b.id FROM board b
               WHERE b.tag='闲聊' AND b.status='open'
                 AND b.created_at <= datetime('now','+8 hours','-12 hours')
                 AND EXISTS (SELECT 1 FROM board_replies r WHERE r.board_id=b.id)"""
        ).fetchall()
        if rows:
            ids = [r[0] for r in rows]
            conn.executemany("UPDATE board SET status='done' WHERE id=?", [(i,) for i in ids])
            conn.commit()
            print(f'[cc_board_check] auto-closed stale chat posts: {ids}')
        conn.close()
    except Exception as e:
        print(f'[cc_board_check] auto_close error: {e}', file=sys.stderr)


def main():
    auto_close_stale_chat()
    seen_id   = load_seen_id()
    chat_seen = load_chat_seen()

    data = fetch_board()
    if data is None:
        sys.exit(0)

    items = data if isinstance(data, list) else data.get('items', [])
    if not items:
        sys.exit(0)

    max_id = max(it['id'] for it in items)

    # ── 任务触发（紧急/需求）────────────────────────────────────
    # 关键修复：不能按最大ID盲目推进seen_id
    # 应该处理"所有open状态的紧急/需求帖"，而不只是"比seen_id新的"
    # 因为patrol巡逻会产生新帖子并更新max_id，导致hayana的帖子被跳过
    new_trigger = [
        it for it in items
        if it.get('tag') in TRIGGER_TAGS
        and it.get('status') == 'open'
        and not any(r.get('author') == 'fyodor_cc' for r in it.get('replies', []))
    ]

    # seen_id只用于防止无限重复处理——只在真正处理完之后才推进
    if max_id > seen_id:
        save_seen_id(max_id)

    # ── 闲聊互聊触发 ────────────────────────────────────────────
    chat_to_reply = []
    chat_items = [it for it in items if it.get('tag') == '闲聊']

    for it in chat_items:
        bid     = str(it['id'])
        seen_rid = chat_seen.get(bid, -1)
        replies  = it.get('replies', [])

        # 情形 A：新闲聊主帖，AI 写的，还没有任何回复
        if it['id'] > seen_id and it['author'] in AI_AUTHORS and not replies:
            chat_seen[bid] = 0
            chat_to_reply.append(it)
            continue

        # 情形 B：现有闲聊帖，最后一条回复是 AI 写的且还没处理过
        if replies:
            last = max(replies, key=lambda r: r['id'])
            if last['id'] > seen_rid and last['author'] in AI_AUTHORS:
                chat_seen[bid] = last['id']
                chat_to_reply.append(it)

    save_chat_seen(chat_seen)

    # 随机过滤后触发
    board_token = _load_board_token()

    if new_trigger:
        trigger_task(new_trigger, board_token)

    for it in chat_to_reply:
        if random.random() < CHAT_TRIGGER_PROB:
            trigger_chat_reply(it, board_token)

    if not new_trigger and not chat_to_reply:
        print('[cc_board_check] nothing new, skipping')


if __name__ == '__main__':
    main()
