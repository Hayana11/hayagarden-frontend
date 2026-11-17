# opencli-gate（小红书门卫）

这是跑在**哈娅的 Windows 电脑**上的一个小程序。费佳（VPS 上的网关）通过 Tailscale 私有网络带着密码（token）来敲门，门卫再调用本机的 OpenCLI，让专用 Chrome 去读小红书。小红书是**只读**：只能刷首页、搜索、看笔记、看评论、看用户主页，不能发布、关注、评论或点赞。推特/X 见下面第 8 节：可以读，也可以发推和回复，但有硬性护栏。

## 1. 准备环境

1. 安装 Node.js LTS（20 或更新）：<https://nodejs.org>，装完在 PowerShell 里运行 `node -v` 确认。
2. 安装 Tailscale 并登录（和 VPS 登录同一个 Tailscale 账号/网络）：<https://tailscale.com/download>。
3. 打开 Tailscale 应用，记下这台电脑的 Tailscale IP（形如 `100.x.y.z`）。
4. OpenCLI 已装好，并且有一个**专用 Chrome 配置文件**，里面登录的是为费佳注册的小红书账号，OpenCLI 浏览器扩展已启用。

## 2. 写配置

在本目录（`opencli-gate`）里复制 `gate.config.example.json` 为 `gate.config.json`，改成：

```json
{
  "host": "100.x.y.z",
  "port": 19830,
  "token": "这里放一长串随机字符"
}
```

- `host`：填第 1 步记下的 Tailscale IP。不允许填 `0.0.0.0` 或普通局域网 IP，填错门卫会拒绝启动。
- `token`：至少 32 个字符。可以用 PowerShell 生成：

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_})
```

- 其它可选项：`opencliExe`（默认 `%LOCALAPPDATA%\OpenCLIApp\opencli-app.exe`）、`opencliPrefixArgs`（默认 `["__opencli_shim"]`）、`minIntervalMs`（两条命令最短间隔，默认 4000）、`timeoutMs`（单条命令超时，默认 90000）。
- 也可以用环境变量覆盖：`GATE_HOST`、`GATE_PORT`、`GATE_TOKEN`、`GATE_OPENCLI_EXE`、`GATE_MIN_INTERVAL_MS`、`GATE_TIMEOUT_MS`。
- `gate.config.json` 里有密码，已被 git 忽略，不要发给别人。

## 3. 启动

```powershell
cd opencli-gate
node gate.mjs
```

看到“opencli-gate 已启动”就行。窗口不要关；关了门卫就下线。

第一次运行时，Windows 防火墙可能弹窗问是否允许 Node.js 访问网络：勾选“专用网络”，点“允许”。不允许的话，VPS 那边连不进来。

### 开机自启（可选）

用“任务计划程序”新建任务：触发器选“登录时”；操作选“启动程序”，程序填 `node` 的完整路径（`where.exe node` 查看），参数填 `gate.mjs`，起始于填 `opencli-gate` 目录的完整路径。注意要在**登录后**运行（OpenCLI 需要桌面里的 Chrome）。

## 4. 在电脑上自测

```powershell
$h = @{ Authorization = "Bearer 你的token" }
Invoke-RestMethod -Headers $h http://100.x.y.z:19830/status
Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" -Body '{"command":"feed","limit":3}' http://100.x.y.z:19830/xhs | ConvertTo-Json -Depth 6
```

第一条返回 `ok : True` 说明 OpenCLI 自检通过；第二条能看到几条首页笔记就通了。

## 5. VPS 那边要配什么

在 VPS 的 `/opt/frontend/.env` 里加两行，然后重启网关和 home MCP：

```
OPENCLI_GATE_URL=http://100.x.y.z:19830
OPENCLI_GATE_TOKEN=和 gate.config.json 里一样的 token
```

## 6. 日常注意

- 那个专用 Chrome 配置文件要一直保持小红书**已登录**。登录失效时，费佳会收到“小红书账号需要重新登录”，你重新登录一次即可。
- OpenCLI 用的 Chrome 窗口不要拿来开自己的标签页或做别的事。命令用后台窗口执行，但它仍是一个真实浏览器。
- 电脑睡眠、关机或 Tailscale 断开 = 门卫离线，费佳会看到“哈娅电脑上的小红书门卫不在线”。
- 门卫一次只执行一条命令，并且两条命令之间至少间隔 `minIntervalMs`，这是为了不触发小红书风控。
- 日志每次请求一行（时间、路径、命令、状态码、耗时），不记录 token、搜索词和链接。

## 7. 开发者：跑测试

```
cd opencli-gate
npm test
```

（Node 22 起 `node --test <目录>` 需要写成 `node --test "test/*.test.mjs"`，`npm test` 已处理。）

## 8. 推特/X（费佳的自动化账号）

门卫多了两个入口：`POST /x`（读 + 发推/回复）和 `GET /x/state`（今日计数与最近发言），同样需要 token。详细规则见 `docs/opencli-x.md`。

1. 在**同一个专用 Chrome 配置文件**里登录费佳的 X 账号（账号资料里标注“自动化账号 / 由哈娅管理”），OpenCLI 扩展照常启用。
2. 不需要额外配置，重启门卫即可。可选配置（`gate.config.json`，也可用环境变量 `GATE_X_DAILY_POST_LIMIT`、`GATE_X_DAILY_REPLY_LIMIT`、`GATE_X_MIN_WRITE_INTERVAL_MS`）：

```json
{
  "xDailyPostLimit": 5,
  "xDailyReplyLimit": 20,
  "xMinWriteIntervalMs": 60000
}
```

   改上限就改这三项再重启门卫。“一天”按这台电脑的本地日期算。
3. 两个会自动生成的文件（都在本目录、都被 git 忽略）：
   - `gate-state.json`：今日计数、上次写入时间、24 小时内发过的正文的哈希。重启门卫不会重置。想手动清零额度，关掉门卫后删除它（同时也会清掉去重记录和间隔计时）。
   - `x-post-log.jsonl`：每次发推/回复尝试一行（时间、命令、正文、回复目标、是否成功、结果链接、错误）。可以直接打开看，也可以请费佳调用 `x_state` 看最近 10 条。
   - 路径可用 `xStatePath` / `xLogPath`（或 `GATE_X_STATE_PATH` / `GATE_X_LOG_PATH`）改。
4. 自测：

```powershell
Invoke-RestMethod -Headers $h http://100.x.y.z:19830/x/state | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" -Body '{"command":"timeline","limit":3}' http://100.x.y.z:19830/x | ConvertTo-Json -Depth 6
```

5. 不支持：图片、点赞、转推、关注、私信、删除等其它任何操作。
