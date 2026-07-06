"""Codebase 工具客户端：直接调用 codebase/server.py 里的实现（不经 MCP HTTP）。"""
import json
import os
import sys

_ROOT = os.environ.get('FRONTEND_ROOT', '/opt/frontend')
if not os.path.isdir(_ROOT):
    _ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CODEBASE_TOOLS = [
    {
        'name': 'codebase_describe_project',
        'description': '读取项目架构自述 ARCHITECTURE.md（服务拓扑、请求管线、家规）。排查系统问题前先调这个建立全局认识。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'codebase_read_file',
        'description': '按行范围读项目文件（相对路径基于 /opt/frontend，也可 /etc/nginx）。end=0 表示从 start 起读 200 行，单次最多 400 行。',
        'input_schema': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': '如 gateway.py、static/chat.html'},
            'start': {'type': 'integer', 'description': '起始行，默认 1'},
            'end': {'type': 'integer', 'description': '结束行，0 表示自动'},
        }, 'required': ['path']},
    },
    {
        'name': 'codebase_list_directory',
        'description': '列目录（相对路径基于 /opt/frontend）。',
        'input_schema': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': '目录路径，默认 .'},
        }},
    },
    {
        'name': 'codebase_search_code',
        'description': '全项目正则搜索（ripgrep）。glob 如 "*.py"、"static/*.html"。',
        'input_schema': {'type': 'object', 'properties': {
            'pattern': {'type': 'string', 'description': '正则或关键词'},
            'glob': {'type': 'string', 'description': '可选文件过滤'},
        }, 'required': ['pattern']},
    },
    {
        'name': 'codebase_find_references',
        'description': '找符号的定义和所有引用位置。',
        'input_schema': {'type': 'object', 'properties': {
            'symbol': {'type': 'string'},
        }, 'required': ['symbol']},
    },
    {
        'name': 'codebase_patch',
        'description': 'str_replace 式安全补丁：old_string 必须唯一，语法校验不过自动回滚。禁改 .env / memories.db。',
        'input_schema': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'old_string': {'type': 'string'},
            'new_string': {'type': 'string'},
        }, 'required': ['path', 'old_string', 'new_string']},
    },
    {
        'name': 'codebase_create_file',
        'description': '新建文件（已存在则拒绝）。语法校验不过不写入。',
        'input_schema': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'content': {'type': 'string'},
            'mkdirs': {'type': 'boolean', 'description': '父目录不存在时自动创建'},
        }, 'required': ['path', 'content']},
    },
    {
        'name': 'codebase_git_view',
        'description': 'git 只读：status / log / diff。不提供 push。',
        'input_schema': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['status', 'log', 'diff']},
            'target': {'type': 'string', 'description': 'diff/log 可选文件路径'},
        }},
    },
    {
        'name': 'codebase_explain_history',
        'description': '代码考古：聚合 git 历史、留言板、教训库，解释某段代码为什么长这样。',
        'input_schema': {'type': 'object', 'properties': {
            'keyword': {'type': 'string', 'description': '函数名/配置键/文件名'},
            'question': {'type': 'string', 'description': '可选补充问题'},
        }, 'required': ['keyword']},
    },
]

CODEBASE_READ_TOOLS = [
    t for t in CODEBASE_TOOLS
    if t['name'] in (
        'codebase_describe_project', 'codebase_read_file', 'codebase_list_directory',
        'codebase_search_code', 'codebase_find_references', 'codebase_git_view',
        'codebase_explain_history',
    )
]


def _server():
    from codebase import server as s
    return s


def run_codebase_tool(name, args):
    args = args or {}
    s = _server()
    try:
        if name == 'codebase_describe_project':
            return s.describe_project()
        if name == 'codebase_read_file':
            return s.read_file(
                args['path'], int(args.get('start', 1)), int(args.get('end', 0)))
        if name == 'codebase_list_directory':
            return s.list_directory(args.get('path', '.'))
        if name == 'codebase_search_code':
            return s.search_code(args['pattern'], args.get('glob', ''))
        if name == 'codebase_find_references':
            return s.find_references(args['symbol'])
        if name == 'codebase_patch':
            return s.patch(args['path'], args['old_string'], args['new_string'])
        if name == 'codebase_create_file':
            return s.create_file(args['path'], args['content'], bool(args.get('mkdirs', False)))
        if name == 'codebase_git_view':
            return s.git_view(args.get('action', 'status'), args.get('target', ''))
        if name == 'codebase_explain_history':
            return s.explain_history(args['keyword'], args.get('question', ''))
        return f'未知 codebase 工具: {name}'
    except Exception as e:
        return f'codebase 工具失败: {type(e).__name__}: {e}'


def openai_tool_specs(tools=None):
    src = tools or CODEBASE_TOOLS
    return [{
        'type': 'function',
        'function': {
            'name': t['name'],
            'description': t['description'],
            'parameters': t['input_schema'],
        },
    } for t in src]
