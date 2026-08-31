# Claude Code / Codex 手机额度后端

这套链路只上传脱敏数字快照：本地采集器读取 CLI 文件，VPS 接收并保存最近快照，手机前端只读取 VPS。

Claude statusLine 的新优先级、白名单 snapshot、freshness 与尚未激活的边界见文末 R1 章节；其有效时会跳过下述 Claude OAuth 路径。

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

拿到的 `five_hour` / `seven_day` 百分比就是官方客户端里显示的真实数字，比本地文件估算准确。字段约定与 [双子续杯](https://github.com/wgjuan2314/shuangzi-xubei) 一致：

- Claude：`five_hour.utilization` / `seven_day.utilization`（已用%），剩余 = 100 − utilization
- Codex：`rate_limit.primary_window` / `secondary_window` 的 `used_percent` + `limit_window_seconds`（18000≈5h，604800≈7d）。**周额度计划可能只有 primary，且秒数是 604800**——采集器按秒数归类，不会盲信 primary=5h。

采集器只**读取**本机已有的登录凭据文件（`~/.claude/.credentials.json`、`~/.codex/auth.json`），从不写回、从不刷新 token——token 过期由各自 CLI 自己处理，采集器这一轮读不到有效 token 时会直接标记 `unavailable` 或退回本地估算，不会尝试自己去刷新登录状态。

这两个接口**不是官方公开、承诺稳定的 API**，是社区从客户端自身请求里摘出来复用的。可能出现的情况：

- 接口随时可能被下线、改字段、加限制，没有事先通知。
- **Claude `oauth/usage` 很容易 429**（双子续杯建议 10–15 分钟刷新一次）。采集器默认最少间隔 12 分钟才打官方接口（`CONTEXT_USAGE_OFFICIAL_MIN_INTERVAL`，秒），失败时复用上次成功的百分比，**不会**退回 JSONL 误报的「额度已耗尽」。
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

- `claude_statusline`：Claude Code 提供的订阅百分比快照，通过 bridge freshness/reset 检查后消费。
- `claude_oauth_usage` / `codex_oauth_usage`：客户端内部账号用量接口，百分比来自账号数据；不是稳定公共 API。
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

## Claude statusLine quota bridge（R1，尚未激活）

Claude 的四条信号各有独立含义：

| 来源 | 数据含义 | 使用方式 |
| --- | --- | --- |
| Claude Code statusLine `rate_limits` | Claude Code 自己提供的真实 Claude.ai 订阅已用百分比 | 新鲜 snapshot 优先；来源 `claude_statusline`，页面显示“Claude Code 额度快照” |
| OAuth `/api/oauth/usage` | undocumented/internal 账号用量 fallback；当前 parser 读取两个窗口的 `utilization` | statusLine 不可用时保持原 cache/fetch 和 429 cooldown |
| ccusage active block | block reset timing，含 `remaining_minutes` | 不能转换为订阅百分比 |
| project JSONL | limit/reset event | 独立 `effective_limit`，可与任意百分比来源共存；不是统一百分比 |

优先级为：**fresh trusted statusLine → 原 OAuth cache/fetch → ccusage active block → JSONL limit-only → unavailable**。只要 statusLine 至少一个窗口有效，本次 Claude collect 不读取凭据、不调用 OAuth、不读写 `official.json`；不能清除或延长 OAuth cooldown，也不能伪造成功状态。statusLine 缺失、无效、过期或两个窗口都已 reset 时恢复原 OAuth/backoff 路径。Codex 行为不变。

### Bridge 输入与落盘

`scripts/claude_statusline_quota.py` 是仅依赖 Python 标准库的 statusLine command。从 stdin 读取机器 JSON，stdout 输出单行无 ANSI 的文字，例如 **fixture** `5h 44% · 7d 27%`。44/27 只是测试示例；实际数值来自输入。缺一个窗口时只显示另一个，没有有效窗口时不显示假 0%，不覆盖 last-good snapshot。

输入读取上限 1 MiB 字符。百分比必须是有限 JSON number 且在 0–100，拒绝 True/False、字符串、NaN/正负 Infinity、越界。reset 必须是可表示日期的有限 Unix **秒**，拒绝字符串/bool，绝不猜单位或把毫秒转换成秒。额外输入字段不落盘；只有 transcript_path 用于下述瞬时只读检查。

默认文件：

`/var/lib/haya-context-usage/claude-statusline.json`

bridge 与 collector 均可通过 `HAYA_CLAUDE_STATUSLINE_SNAPSHOT` 指向同一自定义文件。建议使用仅 producer/collector 所属用户可访问的目录；目录需要可写，其他用户不应能替换 snapshot。

唯一可持久化 schema：

```json
{
  "schema_version": 1,
  "source": "claude_statusline",
  "observed_at": "<UTC ISO8601>",
  "five_hour": {"used_percentage": 44, "resets_at": 1788195600},
  "seven_day": {"used_percentage": 27, "resets_at": 1788782400}
}
```

上述数字仍为 fixture；缺失窗口可以省略。绝不保存完整 statusLine JSON，尤其不保存 session_id、prompt_id、message UUID、transcript_path、cwd、workspace、repo、model、accountUuid、prompt、cost、正文、token、headers 或 environment。使用同目录临时文件、完整 JSON、flush/fsync、`os.replace` 原子替换；POSIX 文件权限为 `0600`。写失败仍 exit 0，保留终端输出与已有 snapshot，stderr 只显示固定短错误，不含输入或路径。

### REWORK-A：source time 与严格更新

**observed_at = 最近可信 assistant transcript event 的 timestamp**，绝不是 statusLine command execution time。pinned 2.1.220 的 response headers → UDt → L7r() → cxS() → rate_limits 没有 source-age fence；permission/vim/model 状态变化、refreshInterval 等重绘也会再次执行 command，所以禁止用本地调用时间替旧 quota 续命。

bridge 仅瞬时读取 stdin.transcript_path 指向的普通文件，从尾部最多读取 **256 KiB bytes**，再按行逆序寻找可信 event；不全文件加载、不越过上限继续扫描。截断的首行不参与解析。VPS 只读结构抽查确认真实记录具有 type=assistant，message 内 role=assistant、type=message、msg_ 开头的 id、usage object 与 content list。桥接器验证这些 envelope 条件，并排除 isApiErrorMessage / synthetic model 记录；不提取内容或标识值用于输出/持久化。

source timestamp 必须是 timezone-aware ISO，归一到 UTC，不早于 2000-01-01；producer 同样执行默认 3600 秒最大年龄、300 秒未来容差。`CLAUDE_STATUSLINE_MAX_AGE_SEC` 与 collector 共用配置语义。最新真实 assistant 的时间缺失、非法或不新鲜时拒绝写入，不再以更旧的 event 作为替代。路径缺失、不存在、无权限、非普通文件，或有界尾部没有可信 event 时也不写；终端仍可显示当前输入的 5h/7d 百分比，exit 0，不输出路径/正文。

写入前读取并校验现有 snapshot 白名单；只有 candidate event time **严格大于**已有 observed_at 才替换。相等或更旧时不 rewrite、不 restamp，字节与 mtime 均保持。无法安全读取现有文件时不覆盖。Linux 写入端对现有目录 inode 取非阻塞 advisory lock，把比较与 atomic replace 串行化；竞争时 fail-soft，不增加 lock 文件或持久字段。非 POSIX 环境仍执行顺序的严格时间比较，不提供此 Linux 并发锁保证。

示例：assistant 在 10:00，command 在 10:01 执行，observed_at 只能是 10:00。10:30、10:59、11:30 无新 assistant 的重绘不能刷新；到 11:30 collector 自然判 stale。若 10:40 出现新 assistant，即使额度仍为 44/27，也必须更新到 10:40；若值变成 45/28，同样正常更新。首次 activation 遇到几小时前的 assistant 时，producer 直接拒绝生成 authoritative snapshot。

### Collector freshness 与窗口 reset

reader 只读，最多读取 16 KiB 字符，严格检查 schema_version/source/允许字段和时区明确的 observed_at：

- `CLAUDE_STATUSLINE_MAX_AGE_SEC` 默认 3600 秒；非正数或非有限/无效配置恢复默认值。
- 超过最大年龄拒绝；允许最多 300 秒未来 clock skew，超过则拒绝。
- 每个窗口必须满足 `resets_at > now`。过期窗口独立移除，不能作为当前百分比使用；另一个有效窗口仍可消费。
- 输出沿用现有 quota contract，reset 转为 UTC ISO8601；`remaining_percentage = 100 - used_percentage`，仅基于真实订阅百分比。
- `updated_at` 使用 assistant event 对应的 observed_at，不伪称 bridge/collector 此刻刚从服务端获取。

observed_at 是当前 transcript 最近可信 assistant/API response event 的时间。statusLine 执行时间仅用于检查年龄，不能写成 source sample time；固定的 assistant event 不能被重绘续命。snapshot schema 不增加任何字段。不要跨账户复用同一个 snapshot；未来 activation/账户切换须另外处理 producer 与 snapshot 归属，不在本 R1 自动启用。

fresh JSONL 信号仍附加到 quota：generic 429 为 `exhausted=false`，明确 usage/weekly/Opus exhaustion 为 true；只被严格更新的 normal usage/session 清除，保留跨文件事件时间比较。statusLine 百分比不压制此信号。

### Pinned 2.1.220 静态路径与未来 activation

核查对象是 `/opt/frontend/.claude-runtime/node_modules/@anthropic-ai/claude-code/bin/claude.exe`，版本 2.1.220，SHA256：

`674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863`

二进制内嵌 JS 的十进制字节偏移证据：

- `252904465`：机器字段文档明确 0–100 订阅百分比与 Unix 秒；首次 API response 前字段可缺失。
- `264011804`（cxS）：把 L7r() 的 five_hour/seven_day utilization × 100 放入 rate_limits，reset 保留秒。
- `264013642`（uxS）/ `264349682`（interactive footer）：交互 hook 调用 configured statusLine；需满足配置、trust/policy 等已有条件。
- `259237008`（V8s）→ `259196912`（q2o）→ `259202633`：序列化机器 JSON 后写入 command stdin。
- `267502556`（GlE）识别 -p/--print → `267432230` prepared-headless → `267963401` 分派 runHeadless；不挂载 interactive footer，不调用 statusLine command。

**PRINT_MODE_STATUSLINE_EXECUTION = NO（静态证据）。** 当前聊天仍是 `-p --input-format stream-json --output-format stream-json`，不会自动生产此 snapshot。本 R1 不改聊天 transport、不迁移 tmux、不启动 tmux、不改 runtime pin，也不修改生产 settings/systemd/timer。没有 producer 时 collector 正常走原 fallback；本 PR 不意味着生产已经获得新百分比。

以后 interactive/tmux terminal 可复用同一个 bridge，在显示 statusLine 的同时产生脱敏 snapshot。本轮没有任何 tmux 实现。未来单独 activation 才可考虑以下 **文档示例，当前禁止 apply**：

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /opt/frontend/scripts/claude_statusline_quota.py"
  }
}
```

`--no-official-usage` / `CONTEXT_USAGE_OFFICIAL=0` 只禁用 OAuth endpoint 路径，不禁用安全的本地 statusLine snapshot。

验证命令 `python3 -B -m unittest tests.test_context_usage -v` 覆盖 bridge、reader、JSONL、429 cooldown、Codex 及 store/API 合同，全部使用临时 fixture；不运行真实 Claude prompt、/usage 或 Anthropic 请求。REWORK-A 保留原 87 个测试，bridge 正常写入场景补充真实 envelope fixture 与冻结时钟，并新增 source time、严格更新、同值新 response、过期 bootstrap、有界读取、内容隐私及失败行为测试。Frontend runtime harness 原 14 场景和 statusLine 44/27 fixture 不变，collector/UI 没有本轮 diff。修复继续提交原 Draft PR #378；没有 activation、merge 或 deploy。
