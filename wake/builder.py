"""
wake/builder.py
组装各模式的 wake system prompt。

输入：mode + context dict + 基础 system 字符串
输出：最终的 system 字符串（含模式模板 + drive/desire snippets）

依赖：bot_config（prompt 模板）、drive_engine、desire（可选注入）
不依赖 gateway.py，循环导入安全。
"""

import importlib


def _load_template(mode: str, ritual_type: str = '') -> str:
    """从 bot_config 读取对应模式的 prompt 模板"""
    try:
        import bot_config as _bc
        importlib.reload(_bc)
        if mode == 'ritual':
            if ritual_type == 'solstice':
                return _bc.RITUAL_SOLSTICE_PROMPT
            elif ritual_type == 'birthday':
                return _bc.RITUAL_BIRTHDAY_PROMPT
            else:
                return _bc.WAKE_DECISION_PROMPT
        elif mode == 'nightwatch':
            return _bc.NIGHTWATCH_DECISION_PROMPT
        elif mode == 'dream':
            return getattr(_bc, 'DREAM_PROMPT', '')
        elif mode == 'summarize':
            return getattr(_bc, 'SUMMARIZE_PROMPT', '')
        else:
            return _bc.WAKE_DECISION_PROMPT
    except Exception:
        return "[wake] {time} t2={t2_hours}h t={t_hours}h\nTHOUGHTS: ...\nACTION: none\nCONTENT: ..."


def build_prompt_suffix(mode: str, context: dict) -> str:
    """
    格式化模板，返回 system 末尾要 append 的字符串。

    context 字段：
      time, t2_hours, t_hours, activity_desc, ritual_type,
      dream_tone, dream_primer, dream_tone_desc,
      summary_date, dialogue, self_trigger_note
    """
    ritual_type = context.get('ritual_type', '')
    tpl = _load_template(mode, ritual_type)

    if mode == 'ritual':
        return tpl  # 仪式模板不格式化，直接 append

    if mode == 'nightwatch':
        return tpl.format(
            time=context.get('time', ''),
            activity_desc=context.get('activity_desc', ''),
        )

    if mode == 'dream':
        return tpl.format(
            time=context.get('time', ''),
            dream_tone=context.get('dream_tone', 'drifting'),
            dream_primer=context.get('dream_primer', ''),
            dream_tone_desc=context.get('dream_tone_desc', ''),
        )

    if mode == 'summarize':
        return tpl.format(
            summary_date=context.get('summary_date', ''),
            dialogue=context.get('dialogue', ''),
        )

    # normal / 默认
    fmt_str = tpl.format(
        time=context.get('time', ''),
        t2_hours=context.get('t2_hours', '999'),
        t_hours=context.get('t_hours', '999'),
    )
    self_note = context.get('self_trigger_note', '').strip()
    if self_note:
        fmt_str = '[自定义提醒触发] 你之前给自己设的备注：' + self_note + '\n\n' + fmt_str
    return fmt_str


def inject_snippets(system: str, mode: str,
                    desire_driven: bool = False,
                    longing_enabled: bool = False) -> str:
    """
    向 system 注入 drive_engine、desire snippets、以及欲望账本房间（dream/summarize 模式跳过）。
    """
    if mode in ('dream', 'summarize'):
        return system

    try:
        import drive_engine as _de
        snip = _de.get_wake_snippet()
        if snip:
            system += '\n\n' + snip
    except Exception:
        pass

    if desire_driven or longing_enabled:
        try:
            import desire as _des
            snip = _des.get_wake_snippet()
            if snip:
                system += '\n\n' + snip
        except Exception:
            pass

    # ── 欲望账本房间注入（normal/nightwatch 模式） ──────────────
    try:
        from config_store import get_config
        if not get_config('desire_ledger_enabled', False):
            return system
    except Exception:
        return system

    try:
        import desire_ledger as _dl
        import json
        from app import get_db

        # 取房间候选
        desires = _dl.surface(limit=6)
        if not desires:
            return system

        # 记录到 wake_log 的 surfaced_desire_ids
        try:
            conn = get_db()
            # 最新的 wake_log（未落账状态，最后一条）
            last_wake = conn.execute("""
                SELECT id FROM wake_log ORDER BY woke_at DESC LIMIT 1
            """).fetchone()
            if last_wake:
                desire_ids = [d['id'] for d in desires]
                conn.execute("""
                    UPDATE wake_log SET surfaced_desire_ids = ? WHERE id = ?
                """, (json.dumps(desire_ids), last_wake['id']))
                conn.commit()
            conn.close()
        except Exception:
            pass

        # 生成房间文本（纯文本版本）
        projects = [d for d in desires if d['track'] == '项目']
        others = [d for d in desires if d['track'] != '项目']

        room_parts = ['[你的房间]']

        if projects:
            room_parts.append('桌上钉着的（项目）：')
            for p in projects:
                state = p.get('state', '进行中')
                room_parts.append(f"· 「{p['text']}」——进度：{state}（碰过{p.get('surfaced_count', 0)}次）")

        if others:
            room_parts.append('最近浮上来的：')
            for o in others:
                room_parts.append(f"· 「{o['text']}」——碰过{o.get('surfaced_count', 0)}次")

        room_parts.append('（挑哪件做、还是什么都不做，都是你的事。想记进度用 desire_act，想放下或改写用 desire_reflect。）')

        room_text = '\n'.join(room_parts)
        system += '\n\n' + room_text
    except Exception as e:
        # 故障隔离：房间渲染失败不影响唤醒
        pass

    return system
