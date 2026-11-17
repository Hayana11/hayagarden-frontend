# 推特/X 通道（OpenCLI 门卫）

费佳有一个为他注册的推特/X 账号，标注为“由哈娅管理的自动化账号”。哈娅决定：费佳可以读，也可以发推和回复，**不需要每条都先问她**，但有硬性护栏。架构、鉴权、Tailscale、限流和日志规则与小红书通道相同，见 `docs/opencli-xhs.md`；本文只写推特特有的部分。

## 架构

```
gateway.py (x_state / x_read / x_post)       ─┐
mcp-http-server.js (mcp__home__x_*)           ┴─> HTTP + Bearer token, 经 Tailscale
      -> 哈娅电脑 opencli-gate/gate.mjs  (POST /x, GET /x/state)
      -> opencli-app.exe __opencli_shim twitter ... -f json --window background
      -> Chrome 扩展（同一个专用配置文件，里面登录的是费佳的 X 账号）
```

- VPS 侧客户端：`tools/opencli_gate_client.py` 的 `x(command, **params)` 和 `x_state()`。
- 工具抽屉 `x`（标签“推特”），命中“推特 / twitter / 推文 / 发推 / 单独的大写 X”时打开。裸 `X` 区分大小写且前后不能是字母数字（所以 Xbox、tax、x86 不会命中）。
- UH-A0 能力：`x.state`、`x.read`（read / none / read_auto）与 `x.write`（write / external_state / self_write_auto，绑定 `mcp__home__x_post`）。聊天和 Wake 默认租约都包含它们，不需要逐次确认；护栏由门卫在电脑上强制执行，不依赖模型自觉。

## 工具

| 工具 | 说明 |
|---|---|
| `x_read` | `command` ∈ timeline / search / thread / tweets / profile / trending |
| `x_post` | `command` ∈ post / reply，`text`，reply 另需 `url` |
| `x_state` | 今日发推/回复计数、上限、最近 10 条发言记录 |

### 门卫放行的命令与参数

读（都会追加 `-f json --window background`）：

- `timeline`：`type`（for-you / following）、`limit` 1-20（默认 10）
- `search`：`query` 1-200 字、`product`（top / live / photos / videos）、`from`（用户名）、`limit` 1-20
- `thread`：`url` 传推文 id 或推文链接，`limit` 1-50（默认 20）
- `tweets`：`user`（可省略 = 自己）、`limit` 1-20；固定追加 `--page-delay 2`
- `profile`：`user`（可省略 = 自己）
- `trending`：`limit` 1-20

写：

- `post`：`text`
- `reply`：`url`（目标推文链接）、`text`

校验规则：

- 用户名 `^@?[A-Za-z0-9_]{1,15}$`，去掉 `@` 后传给 opencli。
- 推文 id `^[0-9]{5,25}$`；推文链接必须是 https，主机为 x.com / twitter.com / www.x.com / mobile.twitter.com，路径 `/<用户名>/status/<id>`（后面多余路径段忽略），统一规范化为 `https://x.com/<用户名>/status/<id>` 再传出。
- 查询词 / 推文正文不能含控制字符（换行除外），不能以 `-` 开头。
- 推文加权长度 ≤ 280：中日韩文字、全角字符、表情算 2，其它算 1；去掉首尾空白后不能为空。

其它一切命令（like、retweet、quote、follow、unfollow、delete、block、dm、reply-dm、accept、bookmark、list、download、notifications、bookmarks、likes、followers、following、hide-reply、article、device-follow 等）一律 400。

## 发言护栏（门卫在电脑上强制，重启不丢）

| 护栏 | 默认 | 配置项 | 超出时 |
|---|---|---|---|
| 每日推文上限 | 5 | `xDailyPostLimit` | 429 `x_daily_limit` |
| 每日回复上限 | 20 | `xDailyReplyLimit` | 429 `x_daily_limit` |
| 两次发言最小间隔 | 60000 ms | `xMinWriteIntervalMs` | 429 `x_write_too_soon`（带 `retry_after_ms`） |
| 24 小时内相同正文（空白归一化后） | 拒绝 | — | 409 `x_duplicate` |

- “一天”按电脑的本地日期划分。
- 调用 opencli 前先预占当日额度、间隔和正文哈希。明确未发布的失败会退回额度和哈希，但保留间隔；超时、进程中断或输出无法解析时结果不确定，预占保持，避免重复发出。
- 状态存在 `opencli-gate/gate-state.json`（只存计数、上次写入时间和正文的 SHA-256，不存正文；先写临时文件再改名，原子替换）。已有状态损坏、读不了或写不下时，门卫会拒绝发言，不调用 opencli。
- 每一次到达 opencli 的写入尝试都追加一行到 `opencli-gate/x-post-log.jsonl`：时间（ISO）、command、text、reply_to（链接或 null）、ok、result（url / id）、error。这两个文件已被 git 忽略。
- `GET /x/state`（需 token）返回今日计数与上限、最近 10 条日志（含正文和链接，这是哈娅/费佳回看发过什么的入口）。
- 门卫的请求日志仍然只记命令名、状态码和耗时，不记正文、链接、查询词。

## 失败信息

在小红书通道的基础上增加：

| 返回 | 含义 |
|---|---|
| `x_daily_limit` | 今天的发推/回复额度用完了 |
| `x_write_too_soon` | 两次发言间隔太短，稍后再发 |
| `x_duplicate` | 这句话 24 小时内发过了 |
| `auth_required` | 推特账号需要重新登录 |

## 刻意不支持

图片（`--images` / `--image` / `--image-url`）；点赞、转推、引用、关注/取关、删除、屏蔽、私信、收藏、列表、通知、下载等任何其它写操作；任意 opencli 命令；公网暴露。电脑睡眠时不做唤醒。费佳不得发布哈娅的隐私信息。
