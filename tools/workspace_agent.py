"""
workspace_agent.py — sandbox file tools for /opt/workspace (PR 1).

Exposes shell_exec + ws_* + ws_job tools to gateway.py.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from tools import workspace_executor
from tools import workspace_jobs
from tools import workspace_registry
from tools import workspace_apps

WORKSPACE_ROOT = Path(
    os.environ.get("WORKSPACE_ROOT", "/opt/workspace")
).resolve()

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", ".jobs",
    ".cache", "dist", "build", ".pytest_cache", "site-packages",
}

_WS_READ_CODE = r'''
import hashlib
import json
import re
from pathlib import Path
args = json.loads(__import__('sys').stdin.read() or '{}')
path = Path(args['path'])
encoding = args.get('encoding') or 'utf-8'
offset = int(args.get('offset') or 1)
limit = int(args.get('limit') or 200)
max_chars = int(args.get('max_chars') or 20000)
pattern = args.get('pattern') or ''
context_lines = int(args.get('context_lines') or 20)
if path.is_dir():
    print(json.dumps({'error': 'is_directory', 'path': str(path)}, ensure_ascii=False))
    raise SystemExit(0)
text = path.read_text(encoding=encoding, errors='replace')
sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
lines = text.splitlines()
total = len(lines)
if pattern:
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits = [i for i, ln in enumerate(lines, 1) if rx.search(ln)]
    shown = hits[:5]
    spans = []
    for h in shown:
        lo, hi = max(1, h - context_lines), min(total, h + context_lines)
        if spans and lo <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    parts = ['\n'.join(f'{i}\t{lines[i-1]}' for i in range(lo, hi + 1)) for lo, hi in spans]
    numbered = '\n... [gap] ...\n'.join(parts)
    truncated_chars = False
    if len(numbered) > max_chars:
        numbered = numbered[:max_chars]
        truncated_chars = True
    print(json.dumps({
        'ok': True, 'path': str(path), 'file_sha256': sha,
        'pattern': pattern, 'match_count': len(hits), 'match_lines': shown,
        'total_lines': total, 'content': numbered,
        'truncated': len(hits) > len(shown) or truncated_chars,
    }, ensure_ascii=False))
    raise SystemExit(0)
start = max(1, offset)
end = min(total, start + max(1, limit) - 1)
selected = lines[start - 1:end] if start <= total else []
numbered = '\n'.join(f'{i}\t{line}' for i, line in enumerate(selected, start))
truncated_chars = False
if len(numbered) > max_chars:
    numbered = numbered[:max_chars]
    truncated_chars = True
print(json.dumps({
    'ok': True,
    'path': str(path),
    'file_sha256': sha,
    'start_line': start,
    'end_line': end if selected else None,
    'total_lines': total,
    'content': numbered,
    'truncated': end < total or truncated_chars,
}, ensure_ascii=False))
'''

_WS_WRITE_CODE = r'''
import json
import os
from pathlib import Path
os.umask(0o007)
args = json.loads(__import__('sys').stdin.read() or '{}')
path = Path(args['path'])
content = args.get('content') or ''
encoding = args.get('encoding') or 'utf-8'
create_dirs = bool(args.get('create_dirs', True))
if path.exists() and path.is_dir():
    print(json.dumps({'error': 'is_directory', 'path': str(path)}, ensure_ascii=False))
    raise SystemExit(0)
if create_dirs:
    path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(content, encoding=encoding)
import hashlib
print(json.dumps({
    'ok': True,
    'path': str(path),
    'bytes': len(content.encode(encoding, errors='replace')),
    'lines': len(content.splitlines()),
    'file_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
}, ensure_ascii=False))
'''

_WS_EDIT_CODE = r'''
import json
import os
from pathlib import Path
os.umask(0o007)
args = json.loads(__import__('sys').stdin.read() or '{}')
path = Path(args['path'])
old = args.get('old')
new = args.get('new')
encoding = args.get('encoding') or 'utf-8'
if old is None or old == '':
    print(json.dumps({'error': 'old_required'}, ensure_ascii=False))
    raise SystemExit(0)
if new is None:
    new = ''
if path.is_dir():
    print(json.dumps({'error': 'is_directory', 'path': str(path)}, ensure_ascii=False))
    raise SystemExit(0)
import hashlib
text = path.read_text(encoding=encoding, errors='replace')
expected = (args.get('expected_sha256') or '').strip().lower()
actual = hashlib.sha256(text.encode('utf-8')).hexdigest()
if expected and expected != actual:
    print(json.dumps({'error': 'file_changed', 'expected_sha256': expected,
                     'actual_sha256': actual,
                     'hint': 're-read the file, it changed since your last read'},
                    ensure_ascii=False))
    raise SystemExit(0)
count = text.count(old)
if count != 1:
    print(json.dumps({'error': 'match_count_not_one', 'occurrences': count}, ensure_ascii=False))
    raise SystemExit(0)
updated = text.replace(old, new, 1)
path.write_text(updated, encoding=encoding)
print(json.dumps({
    'ok': True,
    'path': str(path),
    'replacements': 1,
    'bytes': len(updated.encode(encoding, errors='replace')),
    'lines': len(updated.splitlines()),
    'file_sha256': hashlib.sha256(updated.encode('utf-8')).hexdigest(),
}, ensure_ascii=False))
'''

_WS_LS_CODE = r'''
import json
from pathlib import Path
args = json.loads(__import__('sys').stdin.read() or '{}')
root = Path(args['path'])
max_depth = int(args.get('max_depth') or 3)
max_entries = int(args.get('max_entries') or 300)
skip = {'node_modules', '.git', '__pycache__', 'venv', '.venv', '.jobs',
        '.cache', 'dist', 'build', '.pytest_cache', 'site-packages'}
if not root.exists():
    print(json.dumps({'error': 'not_found', 'path': str(root)}, ensure_ascii=False))
    raise SystemExit(0)
if root.is_file():
    print(json.dumps({'ok': True, 'path': str(root), 'type': 'file',
                      'size': root.stat().st_size}, ensure_ascii=False))
    raise SystemExit(0)
out = []
count = 0
truncated = False
def walk(d, depth, prefix):
    global count, truncated
    try:
        entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as exc:
        out.append(f'{prefix}!! {type(exc).__name__}')
        return
    for e in entries:
        if truncated:
            return
        if count >= max_entries:
            truncated = True
            return
        if e.is_dir():
            count += 1
            if e.name in skip:
                out.append(f'{prefix}{e.name}/ [skipped]')
                continue
            out.append(f'{prefix}{e.name}/')
            if depth < max_depth:
                walk(e, depth + 1, prefix + '  ')
        else:
            count += 1
            try:
                size = e.stat().st_size
            except Exception:
                size = -1
            out.append(f'{prefix}{e.name} ({size}B)')
walk(root, 1, '')
print(json.dumps({'ok': True, 'path': str(root), 'tree': '\n'.join(out),
                  'entries': count, 'truncated': truncated}, ensure_ascii=False))
'''

_WS_PATCH_CODE = r'''
import hashlib
import json
import os
from pathlib import Path
os.umask(0o007)
args = json.loads(__import__('sys').stdin.read() or '{}')
edits = args.get('edits') or []
contents = {}
order = []
results = []
errors = 0
for i, ed in enumerate(edits):
    path = str(ed.get('path') or '')
    old = ed.get('old') or ''
    new = ed.get('new')
    if new is None:
        new = ''
    item = {'index': i, 'path': path}
    if not path or not old:
        item['error'] = 'path_and_old_required'
        errors += 1
        results.append(item)
        continue
    if path not in contents:
        p = Path(path)
        if not p.is_file():
            item['error'] = 'not_found'
            errors += 1
            results.append(item)
            continue
        contents[path] = p.read_text(encoding='utf-8', errors='replace')
        order.append(path)
    cnt = contents[path].count(old)
    if cnt != 1:
        item['error'] = 'match_count_not_one'
        item['occurrences'] = cnt
        errors += 1
        results.append(item)
        continue
    contents[path] = contents[path].replace(old, new, 1)
    item['ok'] = True
    results.append(item)
if errors:
    print(json.dumps({'error': 'patch_rejected',
                      'detail': 'atomic: nothing was written',
                      'failed': errors, 'edits': results}, ensure_ascii=False))
    raise SystemExit(0)
written = []
for path in order:
    Path(path).write_text(contents[path], encoding='utf-8')
    written.append({'path': path,
                    'file_sha256': hashlib.sha256(contents[path].encode('utf-8')).hexdigest()})
print(json.dumps({'ok': True, 'applied': len(edits), 'files': written}, ensure_ascii=False))
'''

_WS_DIFF_CODE = r'''
import difflib
import json
import os
import subprocess
from pathlib import Path
args = json.loads(__import__('sys').stdin.read() or '{}')
max_chars = int(args.get('max_chars') or 20000)
git_diff_enabled = bool(args.get('git_diff_enabled', False))
def cap(text):
    if len(text) > max_chars:
        return text[:max_chars] + '\n... [diff capped, narrow the path]', True
    return text, False
GIT_SAFE_ENV = {
    'HOME': os.environ.get('HOME', '/tmp'),
    'PATH': '/usr/bin:/bin',
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_CONFIG_SYSTEM': '/dev/null',
    'GIT_TERMINAL_PROMPT': '0',
}
GIT_SAFE_PREFIX = [
    'git', '--no-pager',
    '-c', 'diff.external=',
    '-c', 'core.pager=cat',
    '-c', 'pager.diff=false',
    '-c', 'interactive.diffFilter=',
]
def git_run(argv, cwd):
    return subprocess.run(
        GIT_SAFE_PREFIX + argv,
        cwd=cwd,
        env=GIT_SAFE_ENV,
        capture_output=True,
        text=True,
    )
def git_ok(proc, allow_diff=False):
    if allow_diff:
        return proc.returncode in (0, 1)
    return proc.returncode == 0
a = args.get('a')
b = args.get('b')
path = args.get('path')
if a and b:
    pa, pb = Path(a), Path(b)
    for p in (pa, pb):
        if not p.is_file():
            print(json.dumps({'error': 'not_found', 'path': str(p)}, ensure_ascii=False))
            raise SystemExit(0)
    ta = pa.read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)
    tb = pb.read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)
    d = ''.join(difflib.unified_diff(ta, tb, fromfile=str(pa), tofile=str(pb)))
    d, tr = cap(d)
    print(json.dumps({'ok': True, 'mode': 'files', 'diff': d or '(identical)',
                      'truncated': tr}, ensure_ascii=False))
    raise SystemExit(0)
if path:
    if not git_diff_enabled:
        print(json.dumps({'error': 'git_diff_disabled',
                          'detail': 'git diff is disabled while EXEC_ENABLED=0; use a+b two-file compare instead'},
                         ensure_ascii=False))
        raise SystemExit(0)
    p = Path(path)
    base = p if p.is_dir() else p.parent
    top = git_run(['-C', str(base), 'rev-parse', '--show-toplevel'], cwd=str(base))
    if not git_ok(top):
        print(json.dumps({'error': 'not_a_git_repo', 'path': str(base),
                          'detail': (top.stderr or '')[:300]}, ensure_ascii=False))
        raise SystemExit(0)
    stat = git_run(['-C', str(base), 'diff', '--no-ext-diff', '--no-textconv', '--stat', '--', str(p)],
                   cwd=str(base))
    full = git_run(['-C', str(base), 'diff', '--no-ext-diff', '--no-textconv', '--', str(p)],
                   cwd=str(base))
    if not git_ok(stat, allow_diff=True) or not git_ok(full, allow_diff=True):
        err = (stat.stderr or '') + (full.stderr or '')
        print(json.dumps({'error': 'git_diff_failed',
                          'stat_exit': stat.returncode,
                          'full_exit': full.returncode,
                          'detail': err[:500]}, ensure_ascii=False))
        raise SystemExit(0)
    d, tr = cap(full.stdout)
    print(json.dumps({'ok': True, 'mode': 'git', 'repo': top.stdout.strip(),
                      'stat': stat.stdout, 'diff': d or '(clean)',
                      'truncated': tr}, ensure_ascii=False))
    raise SystemExit(0)
print(json.dumps({'error': 'args_required',
                  'detail': 'pass a+b for two-file diff, or path for git diff'},
                 ensure_ascii=False))
'''


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def workspace_path(raw_path: Any, *, for_create: bool = False) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("path_required")
    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else WORKSPACE_ROOT / candidate
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise ValueError("path_outside_workspace") from exc
    if not for_create and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(minimum, min(maximum, number))


def _run_workspace_python(code: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["python3", "-c", code],
            input=_json(payload),
            cwd=str(WORKSPACE_ROOT),
            user=workspace_executor.EXEC_USER,
            group=workspace_executor.EXEC_GROUP,
            extra_groups=[],
            env=workspace_executor.EXEC_ENV,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "detail": f"operation exceeded {timeout}s"}
    except Exception as exc:
        return {"error": "operation_failed", "detail": str(exc)[:300]}

    if proc.returncode != 0:
        return {
            "error": "operation_failed",
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[:4000],
            "stderr": (proc.stderr or "")[:4000],
        }
    try:
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {"error": "invalid_tool_output", "stdout": (proc.stdout or "")[:4000]}


def _ws_read(arguments: dict[str, Any]) -> str:
    try:
        path = workspace_path(arguments.get("path"))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)[:300]})
    payload = {
        "path": str(path),
        "offset": _coerce_int(arguments.get("offset"), 1, 1, 1_000_000),
        "limit": _coerce_int(arguments.get("limit"), 200, 1, 1000),
        "max_chars": _coerce_int(arguments.get("max_chars"), 20000, 1000, 80000),
        "encoding": str(arguments.get("encoding") or "utf-8"),
        "pattern": str(arguments.get("pattern") or ""),
        "context_lines": _coerce_int(arguments.get("context_lines"), 20, 0, 200),
    }
    return _json(_run_workspace_python(_WS_READ_CODE, payload))


def _ws_write(arguments: dict[str, Any]) -> str:
    try:
        path = workspace_path(arguments.get("path"), for_create=True)
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)[:300]})
    payload = {
        "path": str(path),
        "content": str(arguments.get("content") or ""),
        "create_dirs": bool(arguments.get("create_dirs", True)),
        "encoding": str(arguments.get("encoding") or "utf-8"),
    }
    return _json(_run_workspace_python(_WS_WRITE_CODE, payload))


def _ws_edit(arguments: dict[str, Any]) -> str:
    try:
        path = workspace_path(arguments.get("path"))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)[:300]})
    payload = {
        "path": str(path),
        "old": str(arguments.get("old") or ""),
        "new": str(arguments.get("new") or ""),
        "encoding": str(arguments.get("encoding") or "utf-8"),
        "expected_sha256": str(arguments.get("expected_sha256") or ""),
    }
    return _json(_run_workspace_python(_WS_EDIT_CODE, payload))


def _ws_ls(arguments: dict[str, Any]) -> str:
    raw = str(arguments.get("path") or "").strip()
    if raw in ("", "."):
        path = WORKSPACE_ROOT
    else:
        try:
            path = workspace_path(raw)
        except Exception as exc:
            return _json({"error": type(exc).__name__, "detail": str(exc)[:300]})
    payload = {
        "path": str(path),
        "max_depth": _coerce_int(arguments.get("max_depth"), 3, 1, 8),
        "max_entries": _coerce_int(arguments.get("max_entries"), 300, 10, 2000),
    }
    return _json(_run_workspace_python(_WS_LS_CODE, payload))


def _ws_patch(arguments: dict[str, Any]) -> str:
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        return _json({"error": "edits_required"})
    if len(edits) > 20:
        return _json({"error": "too_many_edits", "max": 20})
    resolved: list[dict[str, str]] = []
    for i, ed in enumerate(edits):
        if not isinstance(ed, dict):
            return _json({"error": "invalid_edit", "index": i})
        try:
            p = workspace_path(ed.get("path"))
        except Exception as exc:
            return _json({"error": type(exc).__name__, "index": i, "detail": str(exc)[:300]})
        resolved.append({
            "path": str(p),
            "old": str(ed.get("old") or ""),
            "new": str(ed.get("new") if ed.get("new") is not None else ""),
        })
    return _json(_run_workspace_python(_WS_PATCH_CODE, {"edits": resolved}))


def _ws_diff(arguments: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "max_chars": _coerce_int(arguments.get("max_chars"), 20000, 2000, 60000),
        "git_diff_enabled": workspace_executor.EXEC_ENABLED,
    }
    try:
        if arguments.get("a") or arguments.get("b"):
            payload["a"] = str(workspace_path(arguments.get("a")))
            payload["b"] = str(workspace_path(arguments.get("b")))
        else:
            raw = str(arguments.get("path") or "").strip()
            if raw in ("", "."):
                payload["path"] = str(WORKSPACE_ROOT)
            else:
                payload["path"] = str(workspace_path(raw))
            if not workspace_executor.EXEC_ENABLED:
                return _json({
                    "error": "git_diff_disabled",
                    "detail": "git diff is disabled while EXEC_ENABLED=0; use a+b two-file compare instead",
                })
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)[:300]})
    return _json(_run_workspace_python(_WS_DIFF_CODE, payload))


WORKSPACE_TOOL_DEFS = [
    {
        "name": "shell_exec",
        "description": (
            "在 /opt/workspace 沙箱里执行 shell 命令（默认关闭，返回 exec_disabled）。"
            "用于跑脚本、安装依赖、测试项目。工作目录 /opt/workspace，超时 300 秒。"
            "源码放 projects/，可下载产物放 artifacts/。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的 shell 命令"},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "ws_ls",
        "description": "列出 /opt/workspace 下的目录树（限深度，跳过 node_modules/.git/__pycache__ 等大目录）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认 /opt/workspace"},
                "max_depth": {"type": "integer", "description": "递归深度，默认 3"},
                "max_entries": {"type": "integer", "description": "条目上限，默认 300"},
            },
        },
    },
    {
        "name": "ws_read",
        "description": "读取 /opt/workspace 下的文本文件（带行号）。支持 offset/limit 或 pattern 搜索模式。返回 file_sha256 供 ws_edit 校验。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对 /opt/workspace"},
                "offset": {"type": "integer", "description": "起始行，默认 1"},
                "limit": {"type": "integer", "description": "最多行数，默认 200"},
                "pattern": {"type": "string", "description": "正则搜索，返回匹配行及上下文"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "ws_write",
        "description": "在 /opt/workspace 下写入完整文本文件（创建或覆盖）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标路径"},
                "content": {"type": "string", "description": "完整文件内容"},
                "create_dirs": {"type": "boolean", "description": "自动创建父目录，默认 true"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "ws_edit",
        "description": "精确替换 /opt/workspace 文件中的一处文本（old 必须恰好出现一次）。可先传 expected_sha256 防并发覆盖。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string", "description": "要替换的原文，必须唯一"},
                "new": {"type": "string", "description": "替换后的文本"},
                "expected_sha256": {"type": "string", "description": "可选，ws_read 返回的 file_sha256"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "ws_patch",
        "description": "原子批量替换：多个 old 必须各匹配一次，任一失败则全部不写。",
        "input_schema": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old": {"type": "string"},
                            "new": {"type": "string"},
                        },
                        "required": ["path", "old", "new"],
                    },
                },
            },
            "required": ["edits"],
        },
    },
    {
        "name": "ws_diff",
        "description": "查看 diff：传 a+b 比较两个文件（始终可用）；传 path 看 git 未提交变更（仅 EXEC_ENABLED=1 时可用）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "string", "description": "第一个文件"},
                "b": {"type": "string", "description": "第二个文件"},
                "path": {"type": "string", "description": "git diff 的文件或目录"},
                "max_chars": {"type": "integer", "description": "输出上限，默认 20000"},
            },
        },
    },
    {
        "name": "ws_job",
        "description": (
            "后台任务：action=start 启动长命令并立刻返回 job id；action=status/tail/list/stop 查询或控制。"
            "默认 notify=true，完成后会推送通知到聊天（不占用生成锁）。需要 EXEC_ENABLED=1。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "status", "tail", "list", "stop"],
                    "description": "操作类型，默认 status",
                },
                "cmd": {"type": "string", "description": "action=start 时的 shell 命令"},
                "name": {"type": "string", "description": "可选任务标签"},
                "id": {"type": "string", "description": "job id（status/tail/stop）"},
                "lines": {"type": "integer", "description": "tail 行数，默认 80"},
                "notify": {
                    "type": "boolean",
                    "description": "start 时完成后是否通知，默认 true",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "workspace_app",
        "description": (
            "管理 /opt/workspace/apps/ 下的实时网页应用（manifest.json + 本地 loopback 服务）。"
            "用 ws_write/ws_patch 写好代码和 manifest 后，action=start 启动；"
            "用户通过 proxy_url（/api/gw/workspace/apps/<id>/proxy/）访问。"
            "start/stop/restart 需要 EXEC_ENABLED=1；失败时用 ws_read 看 log_path。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "start", "stop", "restart"],
                    "description": "操作类型，默认 list",
                },
                "id": {"type": "string", "description": "app 目录名（除 list 外必填）"},
                "force": {
                    "type": "boolean",
                    "description": "stop/restart 时 SIGKILL，默认 false",
                },
            },
        },
    },
]

WORKSPACE_TOOL_NAMES = {t["name"] for t in WORKSPACE_TOOL_DEFS}


def get_workspace_tool_defs() -> list[dict[str, Any]]:
    """Static ws_* tools + mcp envelope + resident custom tools (re-read each call)."""
    return (
        list(WORKSPACE_TOOL_DEFS)
        + list(workspace_registry.META_TOOL_DEFS)
        + workspace_registry.build_resident_tool_defs()
    )


def is_workspace_tool(name: str) -> bool:
    if name in WORKSPACE_TOOL_NAMES:
        return True
    if name in workspace_registry.META_TOOL_NAMES:
        return True
    if name in workspace_registry.MGMT_TOOL_NAMES:
        return True
    return workspace_registry.is_registered_tool(name)


def _dispatch_mgmt_or_custom(name: str, args: dict[str, Any]) -> str:
    if name == "register_workspace_tool":
        return workspace_registry.register_workspace_tool(args)
    if name == "list_workspace_tools":
        return workspace_registry.list_workspace_tools()
    if name == "delete_workspace_tool":
        return workspace_registry.delete_workspace_tool(args)
    if workspace_registry.is_registered_tool(name):
        result = workspace_registry.execute_workspace_tool(name, args)
        if result is not None:
            return result
    return _json({"error": "unknown_workspace_tool", "name": name})


def call_tool(name: str, args: dict, caller: str = "fyodor_cc", conversation_id: str = "") -> str:
    del caller
    args = args or {}
    if name == "shell_exec":
        return workspace_executor.run_exec(str(args.get("cmd") or ""), args.get("secrets"))
    if name == "ws_job":
        return workspace_jobs.ws_job(args, conversation_id=conversation_id)
    if name == "workspace_app":
        return workspace_apps.workspace_app(args)
    if name == "mcp_search":
        return workspace_registry.mcp_search(args)
    if name == "mcp_load":
        return workspace_registry.mcp_load(args)
    if name == "mcp_call":
        return workspace_registry.mcp_call(
            args,
            conversation_id=conversation_id,
            inner_dispatch=_dispatch_mgmt_or_custom,
        )
    if name in workspace_registry.MGMT_TOOL_NAMES:
        return _dispatch_mgmt_or_custom(name, args)
    if workspace_registry.is_registered_tool(name):
        return _dispatch_mgmt_or_custom(name, args)
    if name == "ws_ls":
        return _ws_ls(args)
    if name == "ws_read":
        return _ws_read(args)
    if name == "ws_write":
        return _ws_write(args)
    if name == "ws_edit":
        return _ws_edit(args)
    if name == "ws_patch":
        return _ws_patch(args)
    if name == "ws_diff":
        return _ws_diff(args)
    return _json({"error": "unknown_workspace_tool", "name": name})
