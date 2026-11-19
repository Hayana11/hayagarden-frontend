# 小红书只读通道（OpenCLI 门卫）

> 同一个门卫还提供推特/X 通道（可读，也可在护栏内发推/回复），见 `docs/opencli-x.md`。

让费佳通过哈娅 Windows 电脑上的 OpenCLI 只读浏览小红书，使用的是哈娅为费佳注册的专属账号和专用 Chrome 配置文件。

## 架构

```
gateway.py (xhs_status / xhs_read)  ─┐
mcp-http-server.js (mcp__home__xhs_*) ┴─> HTTP + Bearer token, 经 Tailscale
      -> 哈娅电脑 opencli-gate/gate.mjs (node:http)
      -> opencli-app.exe __opencli_shim xiaohongshu ... -f json --window background
      -> Chrome 扩展（专用配置文件，已登录）
```

- VPS 侧客户端：`tools/opencli_gate_client.py`（网关用）；`mcp-http-server.js` 里有等价的 Node 实现（主 Claude Code 会话用 `mcp__home__xhs_status` / `mcp__home__xhs_read`，已加入 `CC_ALLOWED_TOOLS`）。
- 工具抽屉 `xhs`（标签“小红书”），命中“小红书”等关键词时与 `pocket` 一起打开。
- 门卫实现与安装说明见 `opencli-gate/README.md`。
- 为什么直接调 `opencli-app.exe __opencli_shim`：中文用户名会让 `opencli` 的 .cmd 垫片失效。

## 安全模型

- 只绑定 Tailscale 地址（100.64.0.0/10）；host 为空、`0.0.0.0`、`::` 或其它网段时拒绝启动（仅 `allowLoopback=true` 且 host 为 127.0.0.1 时允许本机测试）。
- 每个请求必须带 `Authorization: Bearer <token>`，用 `crypto.timingSafeEqual` 比较，token 至少 32 字符。无 TLS，依赖 Tailscale 的 WireGuard 加密。
- 只读白名单：`feed`、`search`、`note`、`comments`、`user`。参数严格校验（限制范围、URL 必须是 https://www.xiaohongshu.com 且为笔记/用户主页路径、笔记链接必须带 `xsec_token`、搜索词不得以 `-` 开头）。其它命令一律 400。
- 使用 `execFile`（无 shell），固定追加 `-f json --window background`。
- 一次只执行一条命令（忙则 429），两条命令启动间隔至少 `minIntervalMs`（默认 4 秒），单条命令超时 `timeoutMs`（默认 90 秒，超时杀进程返回 504）。请求体上限 8 KB。
- 日志不含 token、搜索词和链接。

## 配置项

门卫（`opencli-gate/gate.config.json`，环境变量 `GATE_*` 可覆盖）：`host`、`port`（19830）、`token`、`allowLoopback`、`opencliExe`、`opencliPrefixArgs`（`["__opencli_shim"]`）、`minIntervalMs`（4000）、`timeoutMs`（90000）。

VPS（`/opt/frontend/.env` 或环境变量）：`OPENCLI_GATE_URL`（如 `http://100.x.y.z:19830`）、`OPENCLI_GATE_TOKEN`。

## 失败信息

| 返回 | 含义 |
|---|---|
| `computer_not_connected` | 连不上门卫（电脑关机/睡眠/Tailscale 断开） |
| `opencli_gate_misconfigured` | VPS 未配置 URL 或 token |
| `opencli_gate_auth_failed` | token 不一致（门卫返回 401） |
| `auth_required` | 小红书账号需要重新登录（OpenCLI 退出码 77） |
| `bridge_down` | 浏览器扩展没连上（69） |
| `empty` | 结果为空（66） |
| `timeout` | 命令超时（75 或门卫 504） |
| `busy` | 门卫正在执行上一条命令（429） |
| `bad_output` | OpenCLI 输出不是合法 JSON |
| `opencli_failed` | 其它非零退出码 |

成功结果为紧凑 JSON 文本，超过 30 000 字符会截断。

## 刻意不支持

发布、关注/取关、点赞、评论、私信、通知、收藏等任何会改变账号状态的操作；任意 opencli 命令；非小红书域名；公网暴露。电脑睡眠时不做唤醒。
