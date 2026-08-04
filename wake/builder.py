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
                    t_hours_override=None,
                    planner_state_view=None):
    """
    向 system 注入 drive_engine 和 desire snippets（dream/summarize 模式跳过）。

    t_hours_override: DecisionClock user_idle hours for Longing fact.
    When ``planner_state_view`` is set, legacy Decision consumes ``V.drives``
    and Longing uses DecisionClock from V (B1-1A) — no second get_drive().

    Returns ``(system, provenance)`` where provenance is the Decision-time
    freeze from ``drive_engine.decide()`` (or None when unavailable / skipped).
    Prompt text and Settlement share that same snapshot — no Action→Drive guess.
    """
    provenance = None
    if mode in ('dream', 'summarize'):
        return system, provenance

    view = planner_state_view
    if view is not None and t_hours_override is None:
        try:
            t_hours_override = float(view.user_idle_hours)
        except Exception:
            t_hours_override = 0.0

    try:
        import drive_engine as _de
        decision = None
        if hasattr(_de, 'decide') and hasattr(_de, 'freeze_decision_provenance'):
            if view is not None:
                decision = _de.decide(drive=view.drives_for_engine())
            else:
                decision = _de.decide()
            provenance = _de.freeze_decision_provenance(decision)
            snip = _de.get_wake_snippet(decision=decision)
        else:
            snip = _de.get_wake_snippet()
        if snip:
            system = append_system_text(system, snip)
    except Exception:
        provenance = None

    # Stage D final Blocker 3: do NOT inject desire.get_wake_snippet().
    # That path re-reads drives and emits a second Drive→Action decision.
    # Longing-only fact (Stage B) may still be appended when enabled.
    if desire_driven or longing_enabled:
        try:
            import desire as _des
            if hasattr(_des, 'get_longing_wake_fact'):
                snip = _des.get_longing_wake_fact(
                    t_hours_override=t_hours_override,
                )
            else:
                snip = ''
            if snip:
                system = append_system_text(system, snip)
        except Exception:
            pass

    return system, provenance
