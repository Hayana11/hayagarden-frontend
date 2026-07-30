# NEXUS UI 交接合同

面向明日 `/nexus` UI。后端插头名称已冻结；不得自行改名。

## 条件项裁决表

| 条件项 | 实际选择 | YES/NO | 代码依据 |
|--------|----------|-------:|----------|
| busy 使用仓库既有状态码 | **423 Locked**（与 `window_busy` / `rollover_deferred` 同语义族） | YES | `nexus_routes.py` `start_turn` → `423` + `code: nexus_busy`；仓库先例：`context_window_routes.py` |
| Last-Event-ID 续接可用 | **不支持** | NO | 全仓无 `Last-Event-ID` SSE 头处理；`nexus_routes.py` `turn_events` 固定 `after_sequence=0`；`capabilities.last_event_id=false` |
| context usage 可安全获取 | **可返回数值快照或 `null`** | YES | `app.py` `_nexus_context_usage_snapshot` → `context_usage_store.get_snapshot`；失败或空则 `null` |
| POST 返回 events_url，GET 提供 SSE | `events_url=/api/nexus/turn/<turn_id>/events`；GET 同路径 SSE | YES | `nexus_runtime.start_turn` / `nexus_routes.turn_events` |

## 冻结 Endpoint

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/nexus/status` | runtime / session / git / capabilities |
| POST | `/api/nexus/turn` | 启动 turn |
| GET | `/api/nexus/turn/<turn_id>/events` | SSE |
| POST | `/api/nexus/turn/<turn_id>/interrupt` | 中断 |
| GET | `/api/nexus/git` | git 摘要 |
| GET | `/api/nexus/turns` | 最多 50 条摘要 |

认证：与 Moments/Monopoly 相同的 owner guard（Bearer / owner cookie）。未配置时 503，未认证 401。

## POST /api/nexus/turn

### 请求

```json
{
  "agent": "claude",
  "instruction": "请在 nexus_fixture.txt 写入 hello"
}
```

- 只允许 `agent`、`instruction`
- `agent`: `claude` | `codex`
- `instruction`: 1–8000 Unicode 字符纯文本
- 额外字段（含 `cwd`/`shell`/`env`/`sandbox` 等）→ **400** `unexpected_fields`

### 成功响应（202）

```json
{
  "turn_id": "a1b2c3…",
  "accepted": true,
  "events_url": "/api/nexus/turn/a1b2c3…/events"
}
```

### Busy（423）

```json
{
  "ok": false,
  "error": "runtime is processing another turn",
  "code": "nexus_busy",
  "retryable": true
}
```

UI：收到 423 时应禁用发送并提示“施工台忙碌”，可短退避重试；不要排队连发。

## SSE 接法

1. `POST /api/nexus/turn` 成功后取 `events_url`
2. `EventSource` 或 `fetch` 流式读取该 URL（需带 owner 认证；原生 EventSource 不便带 Authorization 时用 fetch + ReadableStream）
3. 每条 SSE：`event: <name>` + `data: <json>`
4. **不要依赖 Last-Event-ID**（不支持）
5. 客户端断线默认不杀 Agent；需要停止时调用 interrupt
6. 读到唯一终态 `done` 或 `err` 后关闭连接

### 六个公共字段

```json
{
  "event": "text",
  "turn_id": "…",
  "agent": "claude",
  "sequence": 3,
  "timestamp": "2026-07-30T12:00:00.000Z",
  "data": { "text": "…" }
}
```

### 九种事件示例

**meta**
```json
{"event":"meta","turn_id":"…","agent":"claude","sequence":1,"timestamp":"…","data":{"phase":"accepted"}}
```

**think**
```json
{"event":"think","turn_id":"…","agent":"claude","sequence":2,"timestamp":"…","data":{"text":"planning"}}
```

**text**
```json
{"event":"text","turn_id":"…","agent":"claude","sequence":3,"timestamp":"…","data":{"text":"done"}}
```

**tool_use**
```json
{"event":"tool_use","turn_id":"…","agent":"claude","sequence":4,"timestamp":"…","data":{"name":"write","path":"nexus_fixture.txt"}}
```

**tool_result**
```json
{"event":"tool_result","turn_id":"…","agent":"claude","sequence":5,"timestamp":"…","data":{"ok":true}}
```

**status**
```json
{"event":"status","turn_id":"…","agent":"claude","sequence":6,"timestamp":"…","data":{"phase":"interrupted"}}
```

**git**
```json
{"event":"git","turn_id":"…","agent":"claude","sequence":7,"timestamp":"…","data":{"branch":"main","changed_files":["nexus_fixture.txt"],"diff_stat":"…","additions":1,"deletions":0,"clean":false}}
```

**done**
```json
{"event":"done","turn_id":"…","agent":"claude","sequence":8,"timestamp":"…","data":{"ok":true}}
```

**err**
```json
{"event":"err","turn_id":"…","agent":"claude","sequence":8,"timestamp":"…","data":{"code":"interrupted","message":"turn interrupted"}}
```

### 事件顺序与唯一终态

- `sequence` 从 1 单调递增
- 每个 turn **恰好一个**终态：`done` 或 `err`
- 中断：先可选 `status.phase=interrupted`，再以 `err.code=interrupted` 结束
- 未知原始事件归一为 `status`

## Interrupt

`POST /api/nexus/turn/<turn_id>/interrupt`

```json
{"ok":true,"turn_id":"…","interrupted":true,"state":"interrupting","detail":"interrupt_requested"}
```

非活动 turn（幂等）：

```json
{"ok":true,"turn_id":"…","interrupted":false,"state":"done","detail":"not_active"}
```

不接受 PID / 进程名。

## Git 摘要

`GET /api/nexus/git`

```json
{
  "branch": "main",
  "changed_files": ["nexus_fixture.txt"],
  "diff_stat": " nexus_fixture.txt | 1 +\n 1 file changed, 1 insertion(+)",
  "additions": 1,
  "deletions": 0,
  "clean": false
}
```

## Status

`GET /api/nexus/status` 含：

- `runtime_state` / `active_turn_id` / `active_agent`
- `sessions.claude|codex.exists`
- `context_usage`：对象或 **`null`**
- `git` 摘要
- `capabilities` flags

### context_usage 为 null 时的 UI 行为

隐藏用量条或显示“用量暂不可用”，**不要**假装 0%。不要报错阻断接线。

## Turns

`GET /api/nexus/turns` → `{ "turns": [ … ] }`，最多 50 条摘要，无完整正式聊天、无敏感原始事件。

## Capability flags（以 status 为准）

| flag | 值 |
|------|----|
| interrupt | true |
| sse | true |
| last_event_id | false |
| clear | false（Clear = 前端本地清屏） |
| rewind | false（不存在） |
| push / merge / deploy / shell | false |
| busy_http_status | 423 |
| instruction_max_chars | 8000 |
| turns_history_limit | 50 |
| agent_availability.claude.available | **false**（`ENVIRONMENT_BLOCKED`：无硬 workspace 隔离） |
| agent_availability.codex.available | true（`workspace-write`） |

### workspace 缺失时的 status

`GET /api/nexus/status` 仍返回 **200** JSON：

- `workspace.ok = false`
- `workspace.error` 为错误码（如 `workspace_root_missing`）
- `capabilities` 仍可读
- UI 应禁用发送并提示 workspace 未就绪
- `POST /api/nexus/turn` → **503** fail-closed

### Claude unavailable

在 `agent_availability.claude.available === false` 时，UI 不得提供 Claude 发送入口；不得假定 cwd 即隔离。

## Clear / Rewind

- **Clear**：纯前端本地动作（清空消息视图），无后端 endpoint
- **Rewind**：首版不存在，不要调用、不要画入口

## 明日接线第一步

1. 调 `GET /api/nexus/status` 确认 `capabilities`
2. 用下方 fixture 的 `POST /api/nexus/turn` → 订阅 `events_url`
3. 渲染九类事件；在 `done`/`err` 结束
4. 接线 `interrupt` 与 `GET /api/nexus/git`

## 不依赖真实 Agent 的接线 Fixture

后端单测使用 Fake adapter。UI 开发可对 mock 服务或 test client 期望：

```http
POST /api/nexus/turn
Content-Type: application/json

{"agent":"claude","instruction":"update nexus_fixture.txt"}
```

随后 SSE 至少出现：`meta` → `think` → `tool_use` → `tool_result` → `text` → `git` → `done`。

并发第二发应收到 **423 nexus_busy**。

## 明确未提供

- Clear / Rewind / shell / push / merge / deploy endpoints
- 浏览器传入 cwd、env、sandbox
- Last-Event-ID 续接
- 跨进程 turn 恢复（进程内存 only）
