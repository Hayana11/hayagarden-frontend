# 家的架构（love-style.xyz）

> Codebase MCP 的 describe_project() 返回本文档。改架构时同步改这里——这份文档过期比没有更糟。
> 最后更新：2026-07-05（记忆升级：联邦召回/联想标签/长期事实/map-reduce 日摘要/向量脚手架）

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
  - **工具结果回注历史的截断旋钮**：`TOOL_INJECT_MAX`（本地工具，默认2000）、`TOOL_INJECT_MAX_MCP`（外部/大返回工具 web_search/browse_github/read_webpage/get_activity_summary，默认8000）。build_messages() 把 assistant 的 tool_calls 列压成“[上一轮我调用的工具与结果]”注回对话历史，否则模型下轮会失忆（工具结果不进后续上下文的旧 bug）。
  - **滚动摘要旋钮**：`ROLLING_LIVE_N`（实时窗口条数，默认60）、`ROLLING_HORIZON_DAYS`（回溯天数，默认3）、`ROLLING_MAX_CHARS`（摘要上限，默认700）。
  - **文件清理旋钮**：`FILE_CLEAN_CAP_MB`（文件目录总占用上限 MB，默认200）、`FILE_CLEAN_ORPHAN_GRACE_MIN`（孤儿文件宽限分钟，默认60）。
- **.env**（/opt/frontend/.env）：只做兜底和密钥（ANTHROPIC_API_KEY=relay的key、CLAUDE_CODE_OAUTH_TOKEN、BOARD_TOKEN_FYODOR、DEEPSEEK_API_KEY）。**代码不许写 .env**。
- **models.json**：策展模型清单（label/desc/thinking/primary/dot）。gateway 按 thinking 字段决定传不传 thinking 参数。
- **relay_presets 表**：中转站列表（url/key/capabilities 覆盖）。当前主力 relay id=10（68886868.xyz）。relay 无 haiku 通道；轻量摘要用 `[按量3] deepseek-v3.2`。

## 关键模块

- **relay/**：manager（当前 relay 状态）、adapter（thinking/cache/tools/vision 按能力裁剪）、capabilities（URL 特征静态表 + DB 手动覆盖）、**relay_sanitize**（出站 payload 脱敏：API key/Bearer/.env 路径/陌生 IP → 占位符）。
- **chat/**：system_builder（build_system/build_wake_system，含 ombre handoff 线程化超时）、response_parser。
- **gateway.py 内**：TOOLS（29+工具定义）、run_tool（分发：灯走 mijia HTTP、记忆走 ombre、板子走 5050 API、文件走白名单、**沙箱 ws_* 走 /opt/workspace**）、_summarize_traces_sync/_llm_one_liner/_tool_caption（deepseek 轻摘要）、_cc_stream_gen（claude_code provider：跑 claude CLI 订阅）。
- **artifacts**：create_html/create_markdown/create_document 工具 → artifacts 表 → 独立卡片渲染。

## Workspace 沙箱（2026-07-09，PR 1）

主聊天通过 gateway `TOOLS` 暴露 7 个沙箱工具（`shell_exec` + `ws_*`），读写范围严格限制在 `/opt/workspace`：

| 路径 | 用途 |
|------|------|
| `/opt/workspace/projects/` | 源码、脚本、venv |
| `/opt/workspace/artifacts/tool_outputs/` | shell 长输出落盘 |
| `/opt/workspace/apps/` | 预留（PR 4 workspace_app） |
| `/opt/workspace/tools/` | 预留（PR 3 自造工具） |
| `/opt/workspace/.jobs/` | 预留（PR 2 ws_job） |

- **模块**：`tools/workspace_agent.py`（文件工具分发）、`tools/workspace_executor.py`（shell，默认 `EXEC_ENABLED=0`）、`tools/relay_sanitize.py`（relay 出站脱敏，接在 `adapt_request()` 之后）。
- **部署**：`sudo ./scripts/prepare-workspace-sandbox.sh` 创建目录和 `wsandbox` 用户（`workspace` 组，目录 2770 setgid，文件 660）；`systemctl restart frontend-gw`。
- **安全**：`EXEC_ENABLED=0` 时 `shell_exec` 与 `ws_diff` 的 git 模式均禁用；git 模式启用时使用 `--no-ext-diff --no-pager` 且忽略全局 git 配置，防 external diff 旁路。
- **与 workspace_server.py 分离**：白夜工作台（5052/5053）仍是人工运维面板，不承载 agent 主循环；沙箱工具不指向 `/opt/frontend` 生产代码。
- **脱敏范围**：仅覆盖 `relay.manager` 主聊天/wake/workspace_chat 出站；`gateway._llm_one_liner`、DeepSeek fallback、`tools/repair_agent.py` 等直连 LLM 不在 PR 1 范围。
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

chat_messages（含 thinking/thinking_summary/tool_calls/cache_info/**file_url/file_name/choices**）、board + board_replies、runtime_config、relay_presets、lessons + lesson_candidates、artifacts、wake_log、dream_events、drift_bottles、**rolling_summary**（单行滚动摘要，id=1）、**memory_vectors**（post_id→embedding BLOB，向量检索用）。

## 记忆系统（2026-07-05 升级）

- **自动联想召回**（gateway `_recall_memories`，注入最后一条 user 消息前）：三路联邦——
  ① posts 全扫（jieba 词重叠，正文+tags 一起算）；② 渐变脑 `bucket_mgr.search`（跳过
  dehydrate 的 LLM 调用，热路径直取原文）；③ 向量余弦（EMBED_ENABLED 时）。
  旋钮：`RECALL_MAX_ITEMS`（默认3）、`RECALL_OMBRE`（默认true）。
- **联想标签** `tools/tag_enricher.py`（每6小时）：DeepSeek 给记忆生成同义词/关联概念，
  追加进 tags 的 `assoc:词1|词2` 段；`search_memories` 和召回都搜 tags——字面不同也能想起。
- **长期事实** `tools/fact_extractor.py`（每晚05:40）：从前一天对话抽取约定/纪念日/偏好等
  稳定事实，存 `posts(type='FACT', layer='core')`。不参与 memory_cycle 降权（episodic/semantic
  分离），system_builder 注入 BP2「长期事实」段（15条）。
- **向量检索脚手架**（方案二A，配置即用）：`tools/embedding_tool.py`（OpenAI 兼容
  /embeddings，纯 python 余弦）+ `tools/vector_indexer.py`（每小时增量索引）+
  `memory_vectors` 表（`tools/migrate_memory_vectors.py` 建，备份先行）。
  旋钮：`EMBED_ENABLED`/`EMBED_API_URL`/`EMBED_API_KEY`/`EMBED_MODEL`。
  未配置时零开销；Mac mini 到货后指向本地 Ollama/LM Studio 的 embeddings 端点即点亮。
- **共享轻 LLM 助手** `tools/llm_lite.py`：DeepSeek 直连，cron 专用（热路径禁用）。

## 长上下文与清理（cron）

- **日摘要** `tools/summarizer.py`（每天 06:00）：map-reduce 分段——全量对话按 5000 字切块，
  每块 DeepSeek 压要点（map），再合成日摘要（reduce），量大的日子给 200-400 字。
  不再采样丢内容；claude CLI 只做 DeepSeek 不可用时的兜底。
- **滚动对话摘要** `tools/rolling_summary.py`（每 15 分钟）：build_messages 的实时窗口封顶 60 条；同一天聊得很长时，早于窗口又不够格进日摘要（summarizer.py 只管过去的天）的那段会丢。本脚本把“比窗口早、但在 HORIZON_DAYS 内”的历史用 DeepSeek 压成一段，存 rolling_summary；build_messages 在 len(rows)>=60 时注入到开头。生成走后台，不占对话热路径。
- **用户文件清理** `tools/file_cleaner.py`（每晚 03:30）：① 孤儿文件（磁盘上无任何 chat_messages.file_url 引用，且超 GRACE 分钟）直接删；② 总占用超 CAP_MB 时从最老开始删物理文件 + 清空对应消息的 file 字段（消息保留）。图片附件另有 attachment_store 的 30张/7天自清。

## 文件收发 + 选择器（“哪个字段有值就是哪种气泡”）

沿用 image_url 的老规矩，不用 type 字段：
- **用户发文件**：`POST /api/chat/upload_file`（扩展名白名单 + 2MB + 文件名安全化加随机前缀，存 `/static/uploads/files/`）→ `/api/chat/send` 带 file_url/file_name。build_messages() 把最近 6 条文件全文注入上下文（最多 30000 字截断），更早只留 `[文件:x]` 标记（抄图片降级策略）。
- **AI 发文件**：走已有 artifacts 工具 create_html/create_markdown/create_document（成品性内容不要贴气泡）。
- **选择器**：AI 在正文写 `[choices]A|B|C[/choices]`（标签驱动，非 tool call，不打断流式），gateway `_extract_choices` 在**每一条保存路径**（relay 流式/claude_code/deepseek 降级）抽出存 choices 列、正文去标签（空正文给 `[选项: ...]` 免得 Claude API 拒空 content）。前端按钮可点性 = 这组选项之后没有用户消息；点击=把选项文本当普通消息发出。
- **文档库**：`/files`（static/files.html）查 chat_messages 而非扫目录；`/api/files/delete` 删物理文件 + 清空 file 字段（realpath 防穿越）；html 预览用 `sandbox="allow-scripts"` iframe（无 allow-same-origin，碰不到登录态）。
