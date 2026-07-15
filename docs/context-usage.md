# Claude Code / Codex 手机额度后端

这套链路只上传脱敏数字快照：本地采集器读取 CLI 文件，VPS 接收并保存最近快照，手机前端只读取 VPS。

## 接口

- `GET /api/context-usage`：返回 Claude Code 与 Codex 最近一次可信快照。
- `POST /api/context-usage/report`：电脑采集器上报快照，必须使用 Bearer token。

后端只落库以下白名单字段：额度百分比、剩余分钟、重置时间、token 数、上下文窗口、模型名、来源与更新时间。原始 JSONL、对话、文件路径、OAuth token、API key 不会保存。

## 1. VPS 配置

生成一个独立上报 token，不要复用 Claude/Codex 凭据：

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

写入 `/opt/frontend/.env`：

```dotenv
CONTEXT_USAGE_REPORT_TOKEN=<上一步生成的随机值>
```

重新部署或重启 `frontend` 服务。没有配置这个变量时，上报接口会返回 `503`；token 错误返回 `401`。

## 2. 采集电脑配置

采集器必须运行在实际使用 Claude Code/Codex 的电脑上。它默认读取：

- Codex：`~/.codex/sessions`
- Claude Code：`~/.claude/projects`
- Claude 5 小时 block：`npx -y ccusage@latest blocks --active --json --offline --recent`

PowerShell 示例：

```powershell
$env:CONTEXT_USAGE_REPORT_URL = 'https://love-style.xyz/api/context-usage/report'
$env:CONTEXT_USAGE_REPORT_TOKEN = '<与 VPS 相同的随机值>'
$env:CODEX_SESSIONS_DIR = "$HOME/.codex/sessions"
$env:CLAUDE_PROJECTS_DIR = "$HOME/.claude/projects"
$env:CONTEXT_USAGE_TIMEZONE = 'Asia/Shanghai'

python tools/context_usage_collector.py --print
python tools/context_usage_collector.py
```

第一条只在终端打印脱敏结果，不上报；确认内容后再运行第二条。

持续运行：

```powershell
python tools/context_usage_collector.py --watch --interval 60
```

也可以让 Windows 任务计划程序每 5 分钟执行一次单次命令。电脑睡眠时不会产生新快照，手机会继续显示最后一次更新时间。

若 `npx` 不在 PATH，可用 `CCUSAGE_COMMAND` 指定完整命令。ccusage 第一次执行可能需要联网下载，之后 `--offline` 使用缓存数据。

## 3. 验收

采集电脑上报后，在任意已能访问 App 的设备执行：

```bash
curl -s https://love-style.xyz/api/context-usage
```

应看到 `agents` 中分别存在 `claude` 与 `codex`，并带各自的 `quota_source`。前端 `/dash/usage` 每 15 秒刷新一次。

常见来源：

- `ccusage_blocks`：Claude Code 活跃 5 小时 block。
- `claude_project_jsonl`：只读到 Claude 限额事件，没读到 active block。
- `codex_session_jsonl`：Codex 最近 session 的 `token_count.rate_limits`。
- `unavailable`：对应来源暂时没有可读数据。

`ccusage_blocks` 的 `remainingMinutes` 表示距离 block 重置还有多久，不代表订阅额度剩余多少。采集器会保留 token、倒计时与重置时间，但不会据此伪造 Claude 的“已用百分比”；没有真实限额来源时，前端百分比显示为 `—`。

## 4. 安全边界

- 不要把 `CONTEXT_USAGE_REPORT_TOKEN` 提交到 Git、截图或聊天记录。
- 上报 token 只允许写额度快照，不能调用 Claude/Codex。
- 后端拒绝超过 64 KiB 的上报体，并丢弃白名单外字段。
- 新快照按 agent 独立合并；较旧的上报不会覆盖较新的数据。
