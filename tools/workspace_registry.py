"""
workspace_registry.py — custom workspace tools + mcp_search/load/call envelope (PR 3).

Registry: /opt/workspace/tools/registry.json + /opt/workspace/tools/<name>.sh
Management tools (register/list/delete) are NOT in the static TOOLS array; only
mcp_search / mcp_load / mcp_call are always visible. resident=true custom tools
merge into get_workspace_tool_defs() at request time.
"""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import workspace_executor

logger = logging.getLogger(__name__)

_WORKSPACE_ROOT = Path(workspace_executor.EXEC_CWD).resolve()
_TOOLS_DIR = _WORKSPACE_ROOT / "tools"
_REGISTRY_FILE = _TOOLS_DIR / "registry.json"
_REGISTRY_LOCK_FILE = _TOOLS_DIR / ".registry.lock"
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,48}$")

RESERVED_TOOL_NAMES = {
    "mcp_search",
    "mcp_load",
    "mcp_call",
    "shell_exec",
    "register_workspace_tool",
    "list_workspace_tools",
    "delete_workspace_tool",
    "workspace_app",
    "schedule_reminder",
    "ws_read",
    "ws_write",
    "ws_edit",
    "ws_job",
    "ws_ls",
    "ws_patch",
    "ws_diff",
}

MGMT_TOOL_NAMES = frozenset({
    "register_workspace_tool",
    "list_workspace_tools",
    "delete_workspace_tool",
})

META_TOOL_NAMES = frozenset({"mcp_search", "mcp_load", "mcp_call"})

TOOLS_NOTE = (
    "<tools_note>\n"
    "当前可见的工具列表不是全部能力。/opt/workspace 沙箱里的文件操作、shell、后台任务，\n"
    "以及此前会话注册的自定义工具，都可通过 mcp_search('workspace') → mcp_load → mcp_call 发现。\n"
    "对话涉及这些能力时先 mcp_search，别在未搜索前说做不到。\n"
    "注册/列出/删除自定义工具：mcp_load server=workspace 看 schema，再用 mcp_call 调用。\n"
    "新工具默认 resident=false（不进工具列表，保持 prompt cache 稳定）；仅高频工具设 resident=true。\n"
    "</tools_note>"
)

META_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "mcp_search",
        "description": (
            "按关键词搜索可用的工具服务。返回服务名和描述。"
            "需要超出当前可见列表的能力时先用它（含 workspace 自定义工具）。"
            "你不会记得以前注册过什么工具，所以先搜再断言没有某能力。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，例如 workspace、memory、shell",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "mcp_load",
        "description": (
            "加载某个 MCP 服务的完整工具 schema。mcp_search 之后调用，"
            "必须先 load 再 mcp_call。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "mcp_search 返回的服务名"},
            },
            "required": ["server"],
        },
    },
    {
        "name": "mcp_call",
        "description": (
            "调用 MCP 服务上的工具。须先 mcp_load 该服务。"
            "input 为 JSON 对象，匹配已加载的 schema。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "服务名"},
                "tool": {"type": "string", "description": "工具名"},
                "input": {"type": "object", "description": "工具参数"},
            },
            "required": ["server", "tool"],
        },
    },
]

WORKSPACE_MGMT_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_workspace_tools",
        "description": "列出 /opt/workspace/tools 下已注册的自定义工具。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "register_workspace_tool",
        "description": (
            "创建或替换可复用的沙箱自定义工具。脚本保存到 /opt/workspace/tools/<name>.sh，"
            "在 /opt/workspace 下以 wsandbox 用户运行，参数经环境变量 TOOL_INPUT_JSON 传入。"
            "默认 resident=false：通过 mcp_search workspace → mcp_load → mcp_call 发现，"
            "不进可见工具列表（保持 prompt cache）。resident=true 仅用于每天多次调用的高频工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "2-49 字符，字母开头，字母数字下划线"},
                "description": {"type": "string", "description": "工具用途说明"},
                "parameters": {"type": "object", "description": "参数的 JSON Schema"},
                "script": {"type": "string", "description": "Bash 脚本，读 $TOOL_INPUT_JSON"},
                "resident": {
                    "type": "boolean",
                    "description": "true=加入可见工具列表；默认 false",
                },
            },
            "required": ["name", "description", "script"],
        },
    },
    {
        "name": "delete_workspace_tool",
        "description": "删除已注册的自定义工具及其脚本（默认删 .sh）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工具名"},
                "delete_script": {
                    "type": "boolean",
                    "description": "是否删除脚本文件，默认 true",
                },
            },
            "required": ["name"],
        },
    },
]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _coerce_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    if schema.get("type") != "object":
        return {"type": "object", "properties": {}, "additionalProperties": True}
    schema.setdefault("properties", {})
    return schema


def _harden_path(path: Path, *, is_dir: bool | None = None) -> None:
    import stat
    if is_dir is None:
        is_dir = path.is_dir() or not path.exists()
    mode = 0o2770 if is_dir else 0o660
    try:
        if is_dir:
            path.mkdir(parents=True, exist_ok=True)
        if path.exists():
            os.chmod(path, mode)
            shutil.chown(
                path,
                user=workspace_executor.EXEC_USER,
                group=workspace_executor.EXEC_GROUP,
            )
    except Exception:
        logger.warning("failed to harden sandbox path %s", path, exc_info=True)


def _ensure_tools_dir() -> None:
    old_umask = os.umask(0o007)
    try:
        _TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    _harden_path(_TOOLS_DIR, is_dir=True)


@contextmanager
def _with_registry_lock():
    """Cross-worker file lock for registry.json read-modify-write."""
    _ensure_tools_dir()
    old_umask = os.umask(0o007)
    try:
        lock_fh = open(_REGISTRY_LOCK_FILE, "w", encoding="utf-8")
    finally:
        os.umask(old_umask)
    _harden_path(_REGISTRY_LOCK_FILE, is_dir=False)
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fh.close()


def _read_registry_file() -> dict[str, dict[str, Any]]:
    if not _REGISTRY_FILE.exists():
        return {}
    try:
        raw = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.warning("corrupt workspace registry, resetting", exc_info=True)
        return {}


def _write_registry_file(tools: dict[str, dict[str, Any]]) -> None:
    old_umask = os.umask(0o007)
    try:
        _REGISTRY_FILE.write_text(_json(tools) + "\n", encoding="utf-8")
    finally:
        os.umask(old_umask)
    _harden_path(_REGISTRY_FILE, is_dir=False)


def load_registry() -> dict[str, dict[str, Any]]:
    _ensure_tools_dir()
    with _with_registry_lock():
        return _read_registry_file()


def save_registry(tools: dict[str, dict[str, Any]]) -> None:
    _ensure_tools_dir()
    with _with_registry_lock():
        _write_registry_file(tools)


def _workspace_tool_schema(meta: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(_coerce_schema(meta.get("parameters")))


def _tool_def(name: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": str(meta.get("description") or "Workspace tool."),
        "input_schema": _workspace_tool_schema(meta),
    }


def build_resident_tool_defs() -> list[dict[str, Any]]:
    tools = load_registry()
    return [
        _tool_def(name, meta)
        for name, meta in sorted(tools.items())
        if isinstance(meta, dict) and meta.get("resident")
    ]


def is_registered_tool(name: str) -> bool:
    return name in load_registry()


def is_resident_tool(name: str) -> bool:
    meta = load_registry().get(name)
    return isinstance(meta, dict) and bool(meta.get("resident"))


def list_workspace_tools() -> str:
    tools = load_registry()
    return _json({
        "tools": [
            {
                "name": name,
                "description": meta.get("description", ""),
                "resident": bool(meta.get("resident")),
                "created_at": meta.get("created_at"),
                "script_path": meta.get("script_path"),
            }
            for name, meta in sorted(tools.items())
            if isinstance(meta, dict)
        ],
        "usage_note": (
            "resident 工具在可见工具列表里可直接调用；非 resident 工具通过 "
            "mcp_call server=workspace 调用（先 mcp_load server=workspace 看 schema）。"
        ),
    })


def register_workspace_tool(arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name") or "").strip()
    description = str(arguments.get("description") or "").strip()
    script = str(arguments.get("script") or "").strip()
    parameters = _coerce_schema(arguments.get("parameters"))

    if not _NAME_RE.match(name):
        return _json({
            "error": "invalid_name",
            "detail": "Use 2-49 chars: letters, numbers, underscore; start with a letter.",
        })
    if name in RESERVED_TOOL_NAMES:
        return _json({"error": "reserved_name", "name": name})
    if not description:
        return _json({"error": "description_required"})
    if not script:
        return _json({"error": "script_required"})
    if len(script) > 20000:
        return _json({"error": "script_too_large", "max_chars": 20000})

    _ensure_tools_dir()
    script_path = _TOOLS_DIR / f"{name}.sh"
    body = script
    if not body.startswith("#!"):
        body = "#!/bin/bash\nset -euo pipefail\n" + body + "\n"
    old_umask = os.umask(0o007)
    try:
        script_path.write_text(body, encoding="utf-8")
    finally:
        os.umask(old_umask)
    try:
        os.chmod(script_path, 0o750)
        shutil.chown(
            script_path,
            user=workspace_executor.EXEC_USER,
            group=workspace_executor.EXEC_GROUP,
        )
    except Exception:
        logger.warning("failed to chown workspace tool %s", script_path, exc_info=True)

    resident = bool(arguments.get("resident", False))
    with _with_registry_lock():
        tools = _read_registry_file()
        tools[name] = {
            "description": description,
            "parameters": parameters,
            "script_path": str(script_path),
            "resident": resident,
            "created_at": tools.get(name, {}).get("created_at") or _iso(_now()),
            "updated_at": _iso(_now()),
        }
        _write_registry_file(tools)
    if resident:
        return _json({
            "ok": True,
            "name": name,
            "resident": True,
            "script_path": str(script_path),
            "available_next_turn": True,
        })
    return _json({
        "ok": True,
        "name": name,
        "resident": False,
        "script_path": str(script_path),
        "discover_via": "mcp_search 'workspace' -> mcp_load server=workspace -> mcp_call",
        "note": "not in the tool list (saves prompt cache); call via mcp_call",
    })


def delete_workspace_tool(arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name") or "").strip()
    delete_script = bool(arguments.get("delete_script", True))
    if not _NAME_RE.match(name):
        return _json({"error": "invalid_name"})
    if name in RESERVED_TOOL_NAMES:
        return _json({"error": "reserved_name", "name": name})

    with _with_registry_lock():
        tools = _read_registry_file()
        meta = tools.pop(name, None)
        if meta is None:
            _write_registry_file(tools)
            return _json({"ok": True, "deleted": False, "name": name, "detail": "tool was not registered"})

        script_deleted = False
        script_path = Path(str(meta.get("script_path") or ""))
        if delete_script and str(script_path):
            try:
                script_path.resolve().relative_to(_TOOLS_DIR.resolve())
                if script_path.exists():
                    script_path.unlink()
                    script_deleted = True
            except Exception as exc:
                tools[name] = meta
                _write_registry_file(tools)
                return _json({"error": "delete_script_failed", "detail": str(exc)[:300]})

        _write_registry_file(tools)
    return _json({"ok": True, "deleted": True, "name": name, "script_deleted": script_deleted})


def _run_script(script_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run tool script as wsandbox when available; else current user (dev/CI)."""
    kwargs: dict[str, Any] = {
        "args": ["/bin/bash", str(script_path)],
        "cwd": workspace_executor.EXEC_CWD,
        "env": env,
        "text": True,
        "errors": "replace",
        "capture_output": True,
        "timeout": workspace_executor.EXEC_TIMEOUT,
    }
    try:
        import grp
        import pwd
        pwd.getpwnam(workspace_executor.EXEC_USER)
        grp.getgrnam(workspace_executor.EXEC_GROUP)
        kwargs["user"] = workspace_executor.EXEC_USER
        kwargs["group"] = workspace_executor.EXEC_GROUP
        kwargs["extra_groups"] = []
    except (KeyError, LookupError):
        logger.debug("sandbox user/group missing; running tool without privilege drop")
    return subprocess.run(**kwargs)


def execute_workspace_tool(tool_name: str, arguments: dict[str, Any]) -> str | None:
    tools = load_registry()
    meta = tools.get(tool_name)
    if not isinstance(meta, dict):
        return None

    if not workspace_executor.EXEC_ENABLED:
        return _json({
            "error": "exec_disabled",
            "detail": "custom tool execution is not enabled (set EXEC_ENABLED=1)",
        })

    script_path = Path(str(meta.get("script_path") or ""))
    try:
        script_path.resolve().relative_to(_TOOLS_DIR.resolve())
    except ValueError:
        return _json({"error": "invalid_script_path"})
    if not script_path.exists():
        return _json({"error": "script_missing", "script_path": str(script_path)})

    env = dict(workspace_executor.EXEC_ENV)
    env["TOOL_INPUT_JSON"] = _json(arguments if isinstance(arguments, dict) else {})
    try:
        proc = _run_script(script_path, env)
    except subprocess.TimeoutExpired:
        return _json({
            "error": "timeout",
            "detail": f"tool exceeded {workspace_executor.EXEC_TIMEOUT}s",
        })
    except Exception as exc:
        return _json({"error": "tool_failed", "detail": str(exc)[:300]})

    stdout = (proc.stdout or "")[:12000]
    stderr = (proc.stderr or "")[:12000]
    return _json({"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr})


def workspace_server_entry(query: str) -> dict[str, str] | None:
    tools = load_registry()
    nonres = {n: m for n, m in tools.items() if isinstance(m, dict) and not m.get("resident")}
    q = (query or "").lower().strip()
    searchable = (
        "workspace register create delete manage custom tool "
        + " ".join(f"{n} {m.get('description', '')}" for n, m in nonres.items())
    ).lower()
    if q and q not in searchable:
        return None
    names = ", ".join(sorted(nonres)) if nonres else "none yet"
    return {
        "name": "workspace",
        "description": (
            f"Workspace custom tool management (register/list/delete) plus "
            f"registered non-resident tools ({len(nonres)}): {names}. "
            f"mcp_load server=workspace for schemas, then mcp_call."
        ),
    }


def load_workspace_schemas() -> str:
    mgmt = [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t.get("input_schema") or {"type": "object"},
        }
        for t in WORKSPACE_MGMT_TOOL_DEFS
    ]
    tools = load_registry()
    custom = [
        {
            "name": n,
            "description": m.get("description", ""),
            "input_schema": _workspace_tool_schema(m),
        }
        for n, m in sorted(tools.items())
        if isinstance(m, dict) and not m.get("resident")
    ]
    return _json({"server": "workspace", "tools": mgmt + custom})


def mcp_search(arguments: dict[str, Any]) -> str:
    entry = workspace_server_entry(str(arguments.get("query") or ""))
    return _json({"servers": [entry] if entry else []})


def mcp_load(arguments: dict[str, Any]) -> str:
    if str(arguments.get("server") or "") == "workspace":
        return load_workspace_schemas()
    return _json({"error": "unknown_server"})


def mcp_call(
    arguments: dict[str, Any],
    *,
    conversation_id: str = "",
    inner_dispatch: Any = None,
) -> str:
    del conversation_id
    if str(arguments.get("server") or "") != "workspace":
        return _json({"error": "unknown_server"})
    inner_name = str(arguments.get("tool") or "")
    inner_args = arguments.get("input") if isinstance(arguments.get("input"), dict) else {}
    if inner_name in MGMT_TOOL_NAMES:
        if inner_dispatch is None:
            return _json({"error": "dispatch_unavailable"})
        return inner_dispatch(inner_name, inner_args)
    result = execute_workspace_tool(inner_name, inner_args)
    if result is not None:
        return result
    return _json({"error": "unknown_tool", "tool": inner_name})
