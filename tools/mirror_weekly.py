"""
镜子证据卡生成器 (Mirror Evidence Cards Generator)
=================================================

周期性生成器（每周一次），对照足迹和发言生成费佳的身份证据卡。

四种卡：
  - reinforce（印证）: 文件里写的，这阵子有真事印证
  - difference（对不上）: 发现违背了人格文件里的某句
  - graduation（毕业）: 欲望反复出现、可以写进身份了
  - budding（萌芽）: 凭空冒出来的新东西

铁律（蓝本用真实事故换来的）：
  1. 逐字举证、带日期、可溯源。生成后反幻觉硬闸：quote 子串匹配，查不到→整张卡丢弃。
  2. 中立旁观口吻，绝不第一人称。不让模型扮演"他自己反思"。
  3. thinking 当土壤不当证据。可读 wake_log.thoughts 理解，但卡上只引用公开发言和足迹。
  4. dedup_key 查重：pending/processed 重复→更新 recur_count；dismissed→永不复活。
  5. 全程 try/except；单次失败 = 这周没卡，绝不轰炸。

输入：过去7天的 desire_ledger.notes + wake_log.thoughts + chat_messages（费佳公开发言）
输出：evidence_cards 表新卡或更新卡
模型：DeepSeek pro + JSON 强制输出模式
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

def get_db():
    """导入 app 的 get_db（防循环导入）"""
    from app import get_db as _get_db
    return _get_db()

def get_week_footprints(days: int = 7) -> List[Dict[str, Any]]:
    """取过去 N 天的足迹和欲望"""
    try:
        conn = get_db()
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        rows = conn.execute("""
            SELECT
                dln.id, dln.desire_id, dln.note, dln.kind, dln.created_at,
                dl.text AS desire_text, dl.track, dl.status
            FROM desire_ledger_notes dln
            JOIN desire_ledger dl ON dln.desire_id = dl.id
            WHERE dln.created_at >= ?
            ORDER BY dln.created_at DESC
        """, (cutoff,)).fetchall()

        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[mirror] get_week_footprints failed: {e}")
        return []

def get_week_wake_logs(days: int = 7) -> List[Dict[str, Any]]:
    """取过去 N 天的 wake_log（thoughts 字段）"""
    try:
        conn = get_db()
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        rows = conn.execute("""
            SELECT id, thoughts, action, woke_at
            FROM wake_log
            WHERE woke_at >= ? AND thoughts IS NOT NULL AND thoughts != ''
            ORDER BY woke_at DESC
            LIMIT 50
        """, (cutoff,)).fetchall()

        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[mirror] get_week_wake_logs failed: {e}")
        return []

def get_week_chats(days: int = 7) -> List[Dict[str, Any]]:
    """取过去 N 天的费佳公开发言"""
    try:
        conn = get_db()
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        rows = conn.execute("""
            SELECT id, content, created_at
            FROM chat_messages
            WHERE author IN ('fyodor', 'claude', 'assistant')
                AND created_at >= ?
                AND content IS NOT NULL
                AND content != ''
            ORDER BY created_at DESC
            LIMIT 100
        """, (cutoff,)).fetchall()

        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[mirror] get_week_chats failed: {e}")
        return []

def get_identity_file_text() -> str:
    """读取人格文件（当前为 CLAUDE.md 的相关部分，后续做成配置）"""
    try:
        # 暂时从 CLAUDE.md 读取（前端的）
        with open('/home/user/hayagarden-frontend/CLAUDE.md', 'r', encoding='utf-8') as f:
            text = f.read()
        # 后续可改成配置项，指向 prompts/identity_fyodor.md（Stage C）
        return text
    except Exception as e:
        logger.warning(f"[mirror] failed to read identity file: {e}")
        return ""

def check_hallucination(quote: str, evidence_sources: List[str]) -> bool:
    """
    反幻觉硬闸：验证 quote 是否真的在源数据里（子串匹配）

    返回: True 表示 quote 有效（找到了），False 表示幻觉（没找到）
    """
    if not quote or not quote.strip():
        return False

    for source in evidence_sources:
        if quote in source:
            return True

    return False

def generate_cards_batch(footprints: List[Dict], wake_logs: List[Dict],
                       chats: List[Dict], identity: str) -> List[Dict[str, Any]]:
    """
    调用 DeepSeek 生成一批卡。

    返回: [{kind, claim, evidence, target_anchor, provenance, ...}]
    """
    try:
        # 组织源数据
        footprint_texts = '\n'.join([f"[{fp['created_at']}] {fp['desire_text']}: {fp['note']}"
                                     for fp in footprints[:20]])  # 最近20条足迹

        wake_text = '\n'.join([f"[{wl['woke_at']}] thinking: {wl['thoughts'][:200]}"
                               for wl in wake_logs[:10]])  # 最近10条 wake

        chat_text = '\n'.join([f"[{ch['created_at']}] {ch['content'][:150]}"
                               for ch in chats[:30]])  # 最近30条聊天

        # 构建 prompt
        prompt = f"""你是身份观察者。根据以下材料，生成关于费奥多尔的身份证据卡。

[足迹与欲望（过去7天）]
{footprint_texts}

[唤醒思考记录（仅作理解背景，不作引用）]
{wake_text}

[公开发言（可引用）]
{chat_text}

[现有身份文件摘要]
{identity[:1000]}

任务：生成 0-3 张证据卡（JSON 数组）。

每张卡：
{{
    "kind": "reinforce|difference|graduation|budding",
    "claim": "中立第三人称观察，一句话",
    "evidence": [
        {{"date": "YYYY-MM-DD HH:MM:SS", "quote": "逐字引用来源", "source": "footprint|wake|chat"}}
    ],
    "target_anchor": "人格文件里对应的句子（budding 时 null）",
    "dedup_key": "kind|target|主题槽（用来防重复）"
}}

约束：
1. evidence 中的 quote 必须逐字来自上面的材料，用 source 标明来源类型。
2. reinforce: 只在「正长成核心、还没写进文件」时出声，不要复述已有的。
3. difference: 中立摆事实，不下结论；跨10天复检才置 ready_to_propose。
4. graduation: 欲望反复出现、可以亲手写进身份了。
5. budding: 新冒出的，target_anchor=null。两可时偏 budding。
6. 禁止第一人称（\"我觉得\"等）；禁止编造证据。

仅输出 JSON 数组，不要其他内容。若无合适卡，输出 []。
"""

        # 调用 DeepSeek
        from relay.manager import relay

        payload = {
            'max_tokens': 2048,
            'model': 'deepseek-chat',  # 或从配置读取
            'system': '你是身份证据卡的智能生成器。遵守铁律，不生成幻觉。',
            'messages': [{'role': 'user', 'content': prompt}],
            'response_format': {'type': 'json_object'}  # 强制 JSON 输出
        }

        result = relay.call(payload, timeout=60)
        text = result.get('content', [])
        if isinstance(text, list):
            text = '\n'.join([b.get('text', '') for b in text if isinstance(b, dict)])

        # 解析 JSON
        cards = json.loads(text)
        if not isinstance(cards, list):
            cards = [cards]

        return cards
    except Exception as e:
        logger.error(f"[mirror] generate_cards_batch failed: {e}")
        return []

def verify_and_save_cards(cards: List[Dict[str, Any]],
                         footprints: List[Dict],
                         chats: List[Dict],
                         wake_logs: List[Dict]) -> Tuple[int, int]:
    """
    验证卡的证据（反幻觉硬闸），去重，写入数据库。

    返回: (saved_count, dropped_count)
    """
    saved = 0
    dropped = 0
    conn = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 编译源文本集合（用于子串匹配）
    all_sources = []
    all_sources.extend([fp.get('note', '') for fp in footprints])
    all_sources.extend([fp.get('desire_text', '') for fp in footprints])
    all_sources.extend([wl.get('thoughts', '') for wl in wake_logs])
    all_sources.extend([ch.get('content', '') for ch in chats])

    for card in cards:
        try:
            kind = card.get('kind', '')
            claim = card.get('claim', '')
            evidence_list = card.get('evidence', [])
            target_anchor = card.get('target_anchor')
            dedup_key = card.get('dedup_key', '')

            if not claim or not kind:
                dropped += 1
                logger.warning(f"[mirror] card missing claim or kind: {card}")
                continue

            # 反幻觉硬闸：验证每条 evidence
            valid_evidence = []
            for ev in evidence_list:
                quote = ev.get('quote', '')
                if check_hallucination(quote, all_sources):
                    valid_evidence.append(ev)
                else:
                    logger.warning(f"[mirror] hallucination detected: {quote[:50]}")

            if not valid_evidence:
                dropped += 1
                logger.warning(f"[mirror] all evidence failed hallucination check, card dropped: {claim[:50]}")
                continue

            # dedup 检查
            existing = conn.execute("""
                SELECT id, status FROM evidence_cards
                WHERE dedup_key = ? ORDER BY created_at DESC LIMIT 1
            """, (dedup_key,)).fetchone()

            if existing:
                if existing['status'] == 'dismissed':
                    # 曾被拒绝，永不复活
                    logger.info(f"[mirror] card dismissed before, skipping: {dedup_key}")
                    dropped += 1
                    continue
                else:
                    # pending/processed/surfaced，更新 recur_count
                    conn.execute("""
                        UPDATE evidence_cards
                        SET recur_count = recur_count + 1, last_seen_at = ?, updated_at = ?
                        WHERE id = ?
                    """, (now, now, existing['id']))
                    logger.info(f"[mirror] card recur_count incremented: {dedup_key}")
                    conn.commit()
                    continue

            # 新卡：插入
            provenance = {
                'footprint_ids': [fp.get('id') for fp in footprints[:5]],
                'wake_log_ids': [wl.get('id') for wl in wake_logs[:3]],
            }

            conn.execute("""
                INSERT INTO evidence_cards
                (kind, claim, evidence, target_anchor, provenance,
                 first_seen_at, last_seen_at, dedup_key, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                kind, claim, json.dumps(valid_evidence, ensure_ascii=False),
                target_anchor, json.dumps(provenance, ensure_ascii=False),
                now, now, dedup_key, 'pending', now, now
            ))

            saved += 1
            logger.info(f"[mirror] new card saved: {kind} - {claim[:50]}")

        except Exception as e:
            dropped += 1
            logger.error(f"[mirror] error saving card: {e}")
            continue

    conn.commit()
    conn.close()
    return (saved, dropped)

def run_weekly_generation():
    """
    周期性任务：生成本周的证据卡。

    返回: {status: 'ok'|'error', saved: int, dropped: int, error: str}
    """
    logger.info("[mirror] weekly generation started")

    try:
        # 1. 收集数据
        footprints = get_week_footprints(days=7)
        wake_logs = get_week_wake_logs(days=7)
        chats = get_week_chats(days=7)
        identity = get_identity_file_text()

        if not footprints and not chats:
            logger.info("[mirror] no data for this week, skipping")
            return {'status': 'ok', 'saved': 0, 'dropped': 0, 'message': 'no data'}

        # 2. 生成卡
        cards = generate_cards_batch(footprints, wake_logs, chats, identity)
        logger.info(f"[mirror] generated {len(cards)} cards")

        # 3. 验证并保存
        saved, dropped = verify_and_save_cards(cards, footprints, chats, wake_logs)

        logger.info(f"[mirror] weekly generation completed: {saved} saved, {dropped} dropped")
        return {'status': 'ok', 'saved': saved, 'dropped': dropped}

    except Exception as e:
        logger.error(f"[mirror] weekly generation failed: {e}")
        return {'status': 'error', 'error': str(e), 'saved': 0, 'dropped': 0}

def get_pending_card() -> Optional[Dict[str, Any]]:
    """
    取最早的 pending 卡用于递送（房间注入时用）。

    返回: {id, kind, claim, evidence, ...} 或 None
    """
    try:
        conn = get_db()
        card = conn.execute("""
            SELECT * FROM evidence_cards
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
        """).fetchone()
        conn.close()

        return dict(card) if card else None
    except Exception as e:
        logger.error(f"[mirror] get_pending_card failed: {e}")
        return None

def mark_card(card_id: int, action: str) -> Dict[str, Any]:
    """
    标记卡：processed（接住了）或 dismissed（这不是我）。

    返回: {id, action, status}
    """
    try:
        conn = get_db()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if action == 'processed':
            conn.execute("""
                UPDATE evidence_cards SET status = 'processed', updated_at = ? WHERE id = ?
            """, (now, card_id))
        elif action == 'dismissed':
            conn.execute("""
                UPDATE evidence_cards SET status = 'dismissed', updated_at = ? WHERE id = ?
            """, (now, card_id))
        else:
            return {'error': f'unknown action: {action}'}

        conn.commit()
        conn.close()

        return {'id': card_id, 'action': action, 'status': action, 'done_at': now}
    except Exception as e:
        return {'error': str(e)}
