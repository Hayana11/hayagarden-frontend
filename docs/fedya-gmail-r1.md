# 费奥多尔 Gmail R1

邮箱能力由上游 [`jiangxi1129/ai-penpal-mail`](https://github.com/jiangxi1129/ai-penpal-mail) 提供。Hayagarden 只接入其 Streamable HTTP MCP、把新邮件编号排进现有自定义提醒队列，并复用 External MCP、Claude Code 工具面和 Unified Wake。

## 运行

- 上游源码独立放在 `/opt/ai-penpal-mail/source`，服务读取 `/etc/ai-penpal-mail/.env`，数据和通知游标放在 `/var/lib/ai-penpal-mail/`。
- `.env` 只由 `ai-penpal-mail` 服务用户读取，不进入仓库、`memories.db` 或日志。
- 上游服务使用 `node mail-mcp.mjs --sse`，监听 `127.0.0.1:3457`。
- 在现有 External MCP 管理入口登记 Streamable HTTP 地址 `http://127.0.0.1:3457/mcp`，无需给这段本机连接再配置认证；运行 discovery 后只启用上游的 `list_recent`、`get_message`、`send_email`。
- 安装本目录的 `ai-penpal-mail.service`、`fedya-gmail-wake-poll.service`、`fedya-gmail-wake-poll.timer` 后，启用邮箱服务与两分钟轮询 timer。`.env` 与上游源码必须先就位。
- 轮询第一次运行只记录当时最近邮件编号，之后的新编号各排入一次现有提醒；邮件正文不写入游标或新数据库。现有 `dream_wake.py selftrig` 处理提醒并走 Unified Wake。

## 验收边界

真实 Gmail 自寄、列表、全文读取和 Gmail App「已发送」可作独立验收。新来信 Wake 验收需在服务和 External MCP 登记就位后，从另一个邮箱发送测试邮件；Wake 不会自动回复外部地址。

Google OAuth 项目目前处于 Testing。Google 对申请 Gmail 权限的测试应用签发的刷新令牌有效期为 7 天；长期运行前需要完成发布/验证，或在令牌过期时按上游 `get-token.mjs` 重新授权。详见 [Google OAuth refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)。
