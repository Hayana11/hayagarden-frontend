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
- Claude 5 小时 block：`npx -y ccusage@latest blocks --active --json --offline --recent`（仅在下面的官方额度接口不可用时才会用到）
- Claude 官方账号用量：`~/.claude/.credentials.json` 里的 `accessToken`，或环境变量 `CLAUDE_CODE_OAUTH_TOKEN`（优先）——后者是 `claude setup-token` 生成的长效 token，见第 4.1 节
- Codex 官方账号用量：`~/.codex/auth.json` 里的 `access_token` / `account_id`

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

## 3. 官方账号用量接口（真实百分比）

Claude/Codex 客户端自己登录后，会用同一个已登录的 OAuth token 去查真实的账号剩余额度百分比。采集器默认也会尝试读取这两个接口：

- Claude：`GET https://api.anthropic.com/api/oauth/usage`
- Codex：`GET https://chatgpt.com/backend-api/wham/usage`

拿到的 `five_hour` / `seven_day` 百分比就是官方客户端里显示的真实数字，比本地文件估算准确。采集器只**读取**本机已有的登录凭据文件（`~/.claude/.credentials.json`、`~/.codex/auth.json`），从不写回、从不刷新 token——token 过期由各自 CLI 自己处理，采集器这一轮读不到有效 token 时会直接标记 `unavailable` 或退回本地估算，不会尝试自己去刷新登录状态。

这两个接口**不是官方公开、承诺稳定的 API**，是社区从客户端自身请求里摘出来复用的。可能出现的情况：

- 接口随时可能被下线、改字段、加限制，没有事先通知。
- 只在本机已经登录过对应 CLI、且凭据文件存在时才能读到；没有登录 = 自动退回旧的本地估算逻辑，行为和以前一样。
- 请求只发生在采集器进程内，token 不会被写进日志、不会随快照一起上报给我们自己的后端——后端收到的永远只是百分比数字。

如果不想让采集器碰这两个接口，加 `--no-official-usage`，或设置环境变量 `CONTEXT_USAGE_OFFICIAL=0`，会完全跳过，只用原来的本地文件估算路径。

## 4. 手机不碰电脑时的运行方式

方案 | 做法 | 优点 | 限制
--- | --- | --- | ---
家里电脑常开 | Windows 任务计划程序 / PM2 / NSSM 常驻采集器 | 最容易读到本机 Codex 和 Claude Code 文件，也是官方用量接口需要的登录态所在 | 电脑睡眠就没有新数据
VPS 自建登录 | 直接在 VPS 上装并登录 Claude Code / Codex CLI，采集器也跑在这台 VPS 上 | 不依赖家里电脑开关机，手机随时能看最新数据 | 需要单独处理 VPS 上的登录续期（见下）；账号多了一个常驻登录设备
远程桌面机器 | 把 Codex/Claude Code 都跑在一台云桌面或小主机上 | 最稳定，采集器和 CLI、登录态都在同一机器 | 需要长期运行环境

推荐：如果目标是"手机随时看"，采集器必须和 Codex/Claude Code 的本地数据（以及登录态）在同一台机器上运行——这台机器不一定是你家里的电脑，VPS/云主机自己装一份 CLI 也可以，App 后端只接收脱敏快照。

### 4.1 VPS 自建登录时的 token 续期

`claude` CLI 的正常终端会话里，token 会话大约 8 小时一次自动续期。但采集器不是正常终端会话——如果 VPS 上只靠 cron 定时跑非交互的 `claude -p ...`，续期不会触发，跑几个小时后 token 就会过期，`claude_oauth_usage` 又会退回 `unavailable`。两种解法都支持：

**方案一（推荐）：`claude setup-token` 换长效 token。**

```bash
claude setup-token
```

交互授权一次，拿到一个约一年有效期的长效 token，之后不再依赖自动续期。把它导出为环境变量：

```bash
export CLAUDE_CODE_OAUTH_TOKEN=<setup-token 给出的值>
```

采集器读取账号 token 时会**优先**用这个环境变量，完全不去碰 `~/.claude/.credentials.json`，也就不受"非交互调用不续期"这个限制影响。写进采集器的启动环境（systemd unit 的 `Environment=`、PM2 的 `env` 字段，或者 shell profile）即可长期生效。

**方案二：VPS 上保留一个常驻交互终端。**

用 `tmux`/`screen` 开一个窗口跑着 `claude`（哪怕不主动对话），它的正常会话续期逻辑会持续回写 `~/.claude/.credentials.json`；采集器的 `-p` 管道每次读到的都是这个终端刚续过的有效 token。这个方案不需要额外的环境变量配置，但要保证那个终端会话本身不会被意外关掉。

两种方案都行，方案一更省心（一次授权、一年不用管），方案二贴近"模拟一台一直有人用的电脑"。Codex 目前没有对应的长效 token 导出方式，仍然依赖 `~/.codex/auth.json` 的正常续期，需要类似方案二的常驻会话来保活。

## 5. 验收

采集电脑上报后，在任意已能访问 App 的设备执行：

```bash
curl -s https://love-style.xyz/api/context-usage
```

应看到 `agents` 中分别存在 `claude` 与 `codex`，并带各自的 `quota_source`。前端 `/dash/usage` 每 15 秒刷新一次。

常见来源：

- `claude_oauth_usage` / `codex_oauth_usage`：官方账号用量接口，百分比真实可信。
- `ccusage_blocks`：Claude Code 活跃 5 小时 block（官方接口不可用时的兜底，只有剩余时间，没有百分比）。
- `claude_project_jsonl`：只读到 Claude 限额事件，没读到 active block，也没有官方用量数据。
- `codex_session_jsonl`：Codex 最近 session 的 `token_count.rate_limits`（官方接口不可用时的兜底）。
- `unavailable`：对应来源暂时没有可读数据。

`ccusage_blocks` 的 `remainingMinutes` 表示距离 block 重置还有多久，不代表订阅额度剩余多少。采集器会保留 token、倒计时与重置时间，但不会据此伪造 Claude 的"已用百分比"；没有真实限额来源时（既没有官方接口数据，也没有 ccusage block），前端百分比显示为 `—`。

## 6. 安全边界

- 不要把 `CONTEXT_USAGE_REPORT_TOKEN` 提交到 Git、截图或聊天记录。
- 上报 token 只允许写额度快照，不能调用 Claude/Codex。
- 后端拒绝超过 64 KiB 的上报体，并丢弃白名单外字段。
- 新快照按 agent 独立合并；较旧的上报不会覆盖较新的数据。
- 官方用量接口是非官方逆向出来的：token 只在采集器进程内存里用一次即弃，不落盘、不上报、不写回凭据文件。不放心的话用 `--no-official-usage` / `CONTEXT_USAGE_OFFICIAL=0` 关掉，只保留本地估算路径。
