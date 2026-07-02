# 家的架构（love-style.xyz）

> Codebase MCP 的 describe_project() 返回本文档。改架构时同步改这里——这份文档过期比没有更糟。
> 最后更新：2026-07-02（Codebase MCP v1 上线时）

## 服务拓扑

| 服务 | 端口 | systemd | 入口 | 职责 |
|------|------|---------|------|------|
| 主站 | 5050 | frontend.service | app.py | 页面路由、聊天记录 API、留言板、日历、漂流瓶、artifacts、配置 API |
| AI 网关 | 5051 | frontend-gw.service (gunicorn gthread×4) | gateway.py | 聊天流式管线、工具执行、wake、日记、摘要 |
| 白夜工作台 | 5052 | frontend-workspace.service | workspace_server.py | 独立模型配置的代码工作台（文件白名单读写） |
| 灯守护 | 内部 | — | tools/mijia_daemon.py | 米家灯 HTTP 桥（/light/main/*、/light/bedside/warm|neutral） |
| 教训库 MCP | 5055 | lessons-mcp.service | /opt/lessons/server.py | record→审核→promote→validate_edit 两层筛选 |
| Codebase MCP | 5056 | codebase-mcp.service | /opt/codebase/server.py | 代码检索/补丁/git 只读/架构自述（本文档） |
| 渐变脑 | — | — | /opt/ombre-brain | 长期记忆、handoff、memo |

nginx（/etc/nginx/conf.d/frontend.conf）：`/api/gw/`→5051（read_timeout 320s，buffering off）、`/lessons-mcp/`→5055、`/codebase-mcp/`→5056、`/mcp`、`/discord-mcp`、`/ombre/`。

## 请求管线（聊天一轮的生命周期）

```
chat.html send()
  → POST /api/chat/send (app.py 存 hayana 消息)
  → POST /api/gw/chat/stream (gateway.py)
      build_system() [chat/system_builder.py：人设+记忆+ombre handoff]
      build_messages()
      relay.call_stream() [relay/manager.py → adapter.py 按能力裁剪 → 中转站]
      工具循环（最多5轮）：run_tool() → 六事件 SSE
      _persist() 写 chat_messages（finally 里有断流救援）
  → 前端 parser 分流渲染（trace-row/工具卡/artifact 卡）
```

### SSE 六事件约定（家里 t/d 信封，chatnest 语义）
`think` / `text` / `tool_use`(执行前,含idx) / `tool_result`(执行后) / `trace_summary`(无思考时) / `done`+`err`；
辅助：`usage`、`notice`、`tool_progress`(大参数生成进度)、`tool_call`(dup=1,旧前端兼容)。
所有 provider（api_relay / claude_code / 未来 agent_sdk）统一发这套。协议注释在 gateway.py /chat/stream 上方。

## 配置真源

- **runtime_config 表**（memories.db，经 config_store.py 读写）：MODEL、ACTIVE_RELAY、GW_PROVIDER、WAKE_PROB_SCALE 等。**运行时改配置不用重启**。
- **.env**（/opt/frontend/.env）：只做兜底和密钥（ANTHROPIC_API_KEY=relay的key、CLAUDE_CODE_OAUTH_TOKEN、BOARD_TOKEN_FYODOR、DEEPSEEK_API_KEY）。**代码不许写 .env**。
- **models.json**：策展模型清单（label/desc/thinking/primary/dot）。gateway 按 thinking 字段决定传不传 thinking 参数。
- **relay_presets 表**：中转站列表（url/key/capabilities 覆盖）。当前主力 relay id=10（68886868.xyz）。relay 无 haiku 通道；轻量摘要用 `[按量3] deepseek-v3.2`。

## 关键模块

- **relay/**：manager（当前 relay 状态）、adapter（thinking/cache/tools/vision 按能力裁剪）、capabilities（URL 特征静态表 + DB 手动覆盖）。
- **chat/**：system_builder（build_system/build_wake_system，含 ombre handoff 线程化超时）、response_parser。
- **gateway.py 内**：TOOLS（29+工具定义）、run_tool（分发：灯走 mijia HTTP、记忆走 ombre、板子走 5050 API、文件走白名单）、_summarize_traces_sync/_llm_one_liner/_tool_caption（deepseek 轻摘要）、_cc_stream_gen（claude_code provider：跑 claude CLI 订阅）。
- **artifacts**：create_html/create_markdown/create_document 工具 → artifacts 表 → 独立卡片渲染。
- **tools/cc_board_check.py**：cron 夜巡（东八 02:00-08:00 每半小时），board 紧急/需求帖唤醒 CC；失败2次自动补占位回复退出重试（fail_counts 在 /var/log/cc_board_fail_counts）。

## 部署流

GitHub（Hayana11/hayagarden-frontend，main）↔ VPS /opt/frontend 工作区。
每日 auto backup 自动 commit+push。CC/费佳改动：分支推 GitHub → VPS `git fetch + git checkout FETCH_HEAD -- <files>` → systemctl restart。

## 家规（project rules——改代码前必读）

1. **禁改 .env**——运行时配置一律走 runtime_config（config_store），密钥人工管理。
2. **禁改 memories.db 结构**（除非迁移脚本 + 备份先行）。
3. **改 chat.html 必须 bump static/sw.js 的 CACHE 版本**——PWA 缓存不更新会出现"旧前端不认新事件"级事故（教训#5）。
4. **改 SSE 事件协议必须新旧兼容一个观察期**（dup 标记模式，教训#5）。
5. **board 发帖 tag 语义**：'紧急'/'需求' 会唤醒 CC 烧订阅额度（教训#4）；进度看板用 '看板'；'闲聊' 12小时后自动关闭。
6. **relay 调 LLM**：官方 api.anthropic.com 在这台机器上 401，必须走 API_URL（教训#3）；模型名带通道前缀。
7. **改代码前调 lessons 的 validate_edit**（Codebase MCP 的 patch 工具自动做 quick_match）。
8. **服务改动 ALL_CLEAR 前至少确认一次 systemctl status**（Board Protocol）。
9. git push 只推自己的分支；VPS 上不直接 commit（auto backup 会收编工作区改动）。

## 数据库主要表（memories.db）

chat_messages（含 thinking/thinking_summary/tool_calls/cache_info）、board + board_replies、runtime_config、relay_presets、lessons + lesson_candidates、artifacts、wake_log、dream_events、drift_bottles。
