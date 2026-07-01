"""
chat/response_parser.py — Response Parser（Request Pipeline 最后一环）

统一"从 Anthropic API 响应里提取内容"的逻辑。之前 gateway.py 里有十几处
各自手写 `''.join(b.get('text','') for b in blocks if b.get('type')=='text')`
这类代码，改一次要改十几个地方还容易漏。

每个函数都接受完整的 response dict（{'content': [...], ...}）或者已经
取出来的 content blocks 列表，两种传法都行。
"""


def _get_blocks(response_or_blocks):
    if isinstance(response_or_blocks, dict):
        return response_or_blocks.get('content', []) or []
    return response_or_blocks or []


def extract_text(response_or_blocks):
    """拼接所有 text 类型 block 的内容"""
    blocks = _get_blocks(response_or_blocks)
    return ''.join(
        b.get('text', '') for b in blocks
        if isinstance(b, dict) and b.get('type') == 'text'
    ).strip()


def extract_thinking(response_or_blocks):
    """拼接所有 thinking 类型 block 的内容"""
    blocks = _get_blocks(response_or_blocks)
    return ''.join(
        b.get('thinking', '') for b in blocks
        if isinstance(b, dict) and b.get('type') == 'thinking'
    ).strip()


def extract_tool_uses(response_or_blocks):
    """取出所有 tool_use 类型的 block（原始 dict，含 name/input/id）"""
    blocks = _get_blocks(response_or_blocks)
    return [
        b for b in blocks
        if isinstance(b, dict) and b.get('type') == 'tool_use'
    ]
