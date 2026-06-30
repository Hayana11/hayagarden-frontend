"""
wake/parser.py
解析 AI 输出中的 THOUGHTS / ACTION / CONTENT 三段式结构。
agent loop 多轮拼接时只取最后一组完整输出。
"""
import re as _re

_THOUGHT_PLACEHOLDERS = {
    '你的内心想法（这段不会给哈娅看）',
    '此刻的内心——她还醒着，你在想什么',
    '一句话关于这个梦的内在感受',
    '一句话内心感受',
}

_DIRTY_MARKERS = ('THOUGHTS:', 'ACTION:', 'CONTENT:', '<thinking', '</thinking')

_VALID_ACTIONS = frozenset(('none', 'message', 'diary', 'explore'))


def parse_response(text: str) -> tuple:
    """
    从 AI 输出中提取 THOUGHTS / ACTION / CONTENT。

    返回 (thoughts: str, action: str, content: str)
    action 归一化为 none / message / diary / explore 之一。
    content 含格式污染时 action 降为 none。
    """
    thoughts = ''
    action   = 'none'
    c_text   = ''

    blocks = list(_re.finditer(r'THOUGHTS:', text))
    search_text = text[blocks[-1].start():] if blocks else text

    m = _re.search(r'THOUGHTS:\s*(.+?)(?=\nACTION:|$)', search_text, _re.DOTALL)
    if m:
        thoughts = m.group(1).strip()

    m = _re.search(r'ACTION:\s*(\S+)', search_text)
    if m:
        action = m.group(1).strip().lower()
        if action == 'send':
            action = 'message'
        elif action not in _VALID_ACTIONS:
            action = 'none'

    m = _re.search(r'CONTENT:\s*(.+)', search_text, _re.DOTALL)
    if m:
        c_text = m.group(1).strip()

    if thoughts in _THOUGHT_PLACEHOLDERS:
        thoughts = ''

    if any(mk in c_text for mk in _DIRTY_MARKERS):
        action = 'none'

    return thoughts, action, c_text
