#!/usr/bin/env python3
"""Codebase MCP — 项目意识：检索/补丁/git只读/架构自述，改动自动过教训库。
设计要点：无索引（rg 现查现算，15k 行毫秒级）；patch 用 str_replace 语义而非 diff；
语法校验不过自动回滚；白名单继承 workspace（/opt/frontend + /etc/nginx）。"""
import ast, difflib, json, os, pathlib, re, sqlite3, subprocess, shutil
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("codebase", host="127.0.0.1", port=5056)

ROOTS = ['/opt/frontend', '/etc/nginx']
PROJECT = '/opt/frontend'
DB = '/opt/frontend/memories.db'
# 只读黑名单：patch/create 永远拒绝（家规#1/#2）
FORBIDDEN = {'.env', 'memories.db'}
RG = shutil.which('rg')


def _resolve(path: str) -> pathlib.Path:
    p = pathlib.Path(path)
    if not p.is_absolute():
        p = pathlib.Path(PROJECT) / p
    p = p.resolve()
    if not any(str(p) == r or str(p).startswith(r + '/') for r in ROOTS):
        raise PermissionError(f'路径越界：{p}（白名单 {ROOTS}）')
    return p


def _writable(p: pathlib.Path):
    if p.name in FORBIDDEN or p.suffix == '.db':
        raise PermissionError(f'{p.name} 是禁改文件（家规：运行时配置走 runtime_config，密钥人工管理）')


def _syntax_check(p: pathlib.Path, content: str) -> str:
    """返回错误信息，'' 表示通过。html 会抽出内联 <script> 交给 node。"""
    try:
        if p.suffix == '.py':
            ast.parse(content)
        elif p.suffix in ('.js', '.mjs', '.html'):
            js = content
            if p.suffix == '.html':
                blocks = re.findall(r'<script>(.*?)</script>', content, re.DOTALL)
                if not blocks:
                    return ''
                js = '\n;\n'.join(blocks)
            r = subprocess.run(['node', '--check', '/dev/stdin'], input=js.encode(),
                               capture_output=True, timeout=15)
            if r.returncode != 0:
                return r.stderr.decode()[:500]
        elif p.suffix == '.json':
            json.loads(content)
    except Exception as e:
        return str(e)[:500]
    return ''


def _lessons_hint(description: str) -> str:
    """quick_match 教训库（直接读表，不经 5055 进程）。命中返回提示，不阻塞。"""
    try:
        conn = sqlite3.connect(DB, timeout=3)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT title,description,tags FROM lessons WHERE status='active'").fetchall()
        conn.close()
        words = set(description.lower().replace(',', ' ').split())
        hits = []
        for l in rows:
            tags = set(t.strip().lower() for t in (l['tags'] or '').split(',') if t.strip())
            if tags & words or any(t in description.lower() for t in tags):
                hits.append(f"「{l['title']}」{l['description']}")
        return ('\n⚠️ 命中教训库：\n' + '\n'.join('- ' + h for h in hits[:3])) if hits else ''
    except Exception:
        return ''


def _grep(pattern: str, glob: str = '', max_results: int = 50) -> str:
    if RG:
        cmd = [RG, '-n', '--no-heading', '-m', '5', pattern, PROJECT,
               '-g', '!*.bak*', '-g', '!node_modules', '-g', '!*.min.js', '-g', '!venv']
        if glob:
            cmd += ['-g', glob]
    else:
        cmd = ['grep', '-rn', '--include=' + (glob or '*'), pattern, PROJECT]
    r = subprocess.run(cmd, capture_output=True, timeout=20)
    lines = r.stdout.decode('utf-8', 'ignore').splitlines()
    out = [l.replace(PROJECT + '/', '') for l in lines[:max_results]]
    more = f'\n…共 {len(lines)} 处，已截断' if len(lines) > max_results else ''
    return ('\n'.join(out) + more) if out else f'未找到：{pattern}'


@mcp.tool()
def read_file(path: str, start: int = 1, end: int = 0) -> str:
    """按行范围读文件（相对路径基于 /opt/frontend）。end=0 表示 start 起 200 行。单次最多 400 行。"""
    p = _resolve(path)
    lines = p.read_text(errors='replace').splitlines()
    total = len(lines)
    s = max(1, start)
    e = min(total, end if end > 0 else s + 199, s + 399)
    body = '\n'.join(f'{i}\t{lines[i-1]}' for i in range(s, e + 1))
    return f'{path} [{s}-{e} / 共{total}行]\n{body}'


@mcp.tool()
def list_directory(path: str = '.') -> str:
    """列目录（相对路径基于 /opt/frontend）。目录带/，文件带大小。"""
    p = _resolve(path)
    if not p.is_dir():
        return f'{path} 不是目录'
    items = []
    for c in sorted(p.iterdir()):
        if c.name in ('node_modules', 'venv', '.git', '__pycache__'):
            items.append(c.name + '/ (略)')
        elif c.is_dir():
            items.append(c.name + '/')
        else:
            items.append(f'{c.name}  {c.stat().st_size}B')
    return '\n'.join(items) or '(空目录)'


@mcp.tool()
def search_code(pattern: str, glob: str = '') -> str:
    """全项目正则搜索（ripgrep）。glob 如 '*.py'、'static/*.html'。"""
    return _grep(pattern, glob)


@mcp.tool()
def find_references(symbol: str) -> str:
    """找一个符号的定义和所有引用位置，按文件聚合。"""
    defs = _grep(rf'^\s*(def|class)\s+{re.escape(symbol)}\b|^{re.escape(symbol)}\s*=', max_results=10)
    refs_raw = _grep(rf'\b{re.escape(symbol)}\b', max_results=200)
    counts = {}
    for line in refs_raw.splitlines():
        f = line.split(':', 1)[0]
        if ':' in line:
            counts[f] = counts.get(f, 0) + 1
    agg = '\n'.join(f'{f}  ×{n}' for f, n in sorted(counts.items(), key=lambda x: -x[1])[:20])
    return f'【定义】\n{defs}\n\n【引用分布】\n{agg or "无"}'


@mcp.tool()
def patch(path: str, old_string: str, new_string: str) -> str:
    """str_replace 式安全补丁：old_string 必须在文件中唯一。语法校验不过自动回滚。
    返回 diff + 教训库命中提示。"""
    p = _resolve(path)
    _writable(p)
    src = p.read_text(errors='replace')
    n = src.count(old_string)
    if n == 0:
        return '✗ old_string 在文件中找不到（注意空格和缩进要完全一致）'
    if n > 1:
        return f'✗ old_string 出现 {n} 次，不唯一——扩大上下文让它唯一'
    new_src = src.replace(old_string, new_string, 1)
    err = _syntax_check(p, new_src)
    if err:
        return f'✗ 语法校验失败，未写入：\n{err}'
    p.write_text(new_src)
    diff = ''.join(difflib.unified_diff(
        src.splitlines(keepends=True), new_src.splitlines(keepends=True),
        fromfile=f'a/{path}', tofile=f'b/{path}', n=2))[:3000]
    hint = _lessons_hint(f'{path} {new_string[:200]}')
    return f'✓ 已写入 {path}\n{diff}{hint}'


@mcp.tool()
def create_file(path: str, content: str, mkdirs: bool = False) -> str:
    """新建文件（已存在则拒绝，防覆盖）。语法校验不过不写入。"""
    p = _resolve(path)
    _writable(p)
    if p.exists():
        return f'✗ {path} 已存在——改文件用 patch'
    err = _syntax_check(p, content)
    if err:
        return f'✗ 语法校验失败，未创建：\n{err}'
    if mkdirs:
        p.parent.mkdir(parents=True, exist_ok=True)
    if not p.parent.is_dir():
        return f'✗ 父目录不存在（传 mkdirs=true 自动创建）'
    p.write_text(content)
    return f'✓ 已创建 {path}（{len(content)}字符）' + _lessons_hint(f'{path} {content[:200]}')


@mcp.tool()
def git_view(action: str = 'status', target: str = '') -> str:
    """git 只读三件套：status / log / diff。log 默认最近20条；diff 可带文件路径。不提供 push。"""
    if action not in ('status', 'log', 'diff'):
        return "action 只能是 status / log / diff"
    cmd = ['git', '-C', PROJECT]
    if action == 'status':
        cmd += ['status', '--short']
    elif action == 'log':
        cmd += ['log', '--oneline', '-20'] + ([target] if target else [])
    else:
        cmd += ['diff'] + ([target] if target else [])
    r = subprocess.run(cmd, capture_output=True, timeout=15)
    out = r.stdout.decode('utf-8', 'ignore')[:4000]
    return out or '(无输出——工作区干净或无差异)'


@mcp.tool()
def describe_project() -> str:
    """项目架构自述：返回 ARCHITECTURE.md（含服务拓扑、请求管线、配置真源、家规）。"""
    p = pathlib.Path(PROJECT) / 'ARCHITECTURE.md'
    if p.exists():
        return p.read_text(errors='replace')
    return '(ARCHITECTURE.md 不存在——用 list_directory 和 search_code 自行探索)'


if __name__ == '__main__':
    mcp.run(transport='streamable-http')
