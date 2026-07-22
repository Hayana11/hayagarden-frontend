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
        elif mode == 'morning':
            return _bc.MORNING_DECISION_PROMPT
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

    if mode == 'morning':
        return tpl.format(time=context.get('time', ''))

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


def append_system_text(system, text: str):
    """Append uncached wake-only text without flattening cached system blocks."""
    text = str(text or '').strip()
    if not text:
        return system
    if isinstance(system, list):
        return list(system) + [{'type': 'text', 'text': text}]
    base = str(system or '').strip()
    return base + ('\n\n' if base else '') + text


def inject_snippets(system, mode: str,
                    desire_driven: bool = False,
                    longing_enabled: bool = False,
                    t_hours_override=None):
    """
    向 system 注入 drive_engine 和 desire snippets（dream/summarize 模式跳过）。

    t_hours_override: Wake 权威空闲小时数。传入后 Longing 不再读可能停摆的
    desire_state.last_hayana_msg_time。
    """
    if mode in ('dream', 'summarize'):
        return system

    try:
        import drive_engine as _de
        snip = _de.get_wake_snippet()
        if snip:
            system = append_system_text(system, snip)
    except Exception:
        pass

    if desire_driven or longing_enabled:
        try:
            import desire as _des
            snip = _des.get_wake_snippet(t_hours_override=t_hours_override)
            if snip:
                system = append_system_text(system, snip)
        except Exception:
            pass

    return system
