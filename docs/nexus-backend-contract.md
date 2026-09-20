# NEXUS-BACKEND-MVP-0｜后端冻结合同

状态：冻结。外部字段名、路径与事件名不得自行改名。

## 目标

Nexus 是双 Agent 施工台后端：Claude Code 与 Codex 各有独立 session，共用一个固定隔离 workspace，同一时间只允许一个 Agent 执行，统一流式事件，可精确中断当前 Nexus turn，并返回固定 workspace 的 git 摘要。

首版不实现 UI，不接 CLWD，不 merge，不 deploy。

## 隔离边界

- Nexus 不复用日常聊天 prompt。
- Nexus 不读写正式聊天 session。
- Nexus 不读写 `memories.db` 正式会话。
- Nexus 不修改 group-chat、Wake、persona 或无缝换窗。
- turn 状态只存在进程内存：不落盘、不创建 JSON 状态文件、不写入任何现有 store。
- 进程重启后所有旧 turn 一律视为终止；不恢复旧 turn。
- Clear 是前端本地动作；Rewind 首版不存在。
- 不提供 push、merge、deploy。
- 浏览器不能指定 shell、cwd、env、sandbox。

## Workspace

- 环境变量：`NEXUS_WORKSPACE_ROOT`
- 生产预期值：`/opt/workspace/projects/nexus/current`
- 两个 Agent 共用该固定 workspace
- 导入模块时不创建生产目录
- 路径经 canonical realpath 校验；拒绝 `..` 与软链接越界；fail-closed
- 不回退到 `/opt/frontend`、当前 cwd 或 HOME

## Session 与串行

- Claude 与 Codex 各有独立 session identity
- 同一 workspace 同时最多一个 running turn
- 第二个并发请求立即 busy，不排队
- 只有当前 active turn 可被中断；interrupt 幂等
- Claude 中断不影响正式 Claude resident
- Codex 中断只取消当前 Nexus turn，不关闭全局 app-server
- **Claude 硬隔离（R1）**：仓库无可复用的 Claude Code 硬 workspace confinement（cwd/prompt 不算）。因此生产 Claude capability 保持 `available=false` / `ENVIRONMENT_BLOCKED`；不得用 prompt 或 cwd 冒充隔离。
- **Codex 硬隔离（P0）**：只有 Codex binary、workspace 外已存在的 `NEXUS_CODEX_HOME`、该 home 内认证、以及原生 permission profile 支持全部就绪时，Codex 才可 `available=true`。任一条件缺失均为 `available=false` / `ENVIRONMENT_BLOCKED`，`POST` Codex turn → 503 `codex_unavailable`。
- Nexus 专用 Codex 实例：`env_mode=nexus_allowlist`（不继承 BOARD_TOKEN/DB/API secrets）；`NEXUS_CODEX_HOME` 必须是 workspace 外的绝对已有目录，且不得为 `/opt/frontend`、`/root/.codex` 或其子目录；login/auth probe 与 app-server 使用同一 home。
- Codex thread 使用 named profile `hayagarden_nexus`、`runtimeWorkspaceRoots=[workspace]` 和 `ephemeral=true`。permissions 与 sandbox 互斥，因此 Nexus start/resume 不发送 sandbox；resume 也不发送 ephemeral/serviceName。resume 失败必须启动新 thread 并把新 id 回写 session，不能伪装连续。
- **Interrupt 终态保真（R3）**：`interrupting` 时不得把任意 err 改写成 `interrupted`。仅 `code=interrupted`（provider 已确认）→ turn.state=`interrupted`；`interrupt_failed` / `interrupt_rejected` / `interrupt_unconfirmed` 原样进入唯一 `err` 终态且 `state=error`。未确认中断时必须清理 background terminals 并停止 Nexus app-server 完整 process group；只有 `provider_stop_confirmed=true` 才释放串行门。
- **status 降级（R1）**：workspace 缺失时 `GET /api/nexus/status` 仍返回可读 JSON（`workspace.ok=false` + capabilities）；`POST /api/nexus/turn` 继续 503 fail-closed。
- **Git（R2/R3）**：`status --porcelain -z` / `diff -z --name-only` 解析特殊文件名；rename/copy 只保留目标路径；未跟踪目录展开为文件（`UNTRACKED_DIR_POLICY=expand_files`）。Codex home 位于 workspace 外，不需要 git 过滤伪隔离目录。
## 冻结 Endpoint

| Method | Path |
|--------|------|
| GET | `/api/nexus/status` |
| POST | `/api/nexus/turn` |
| GET | `/api/nexus/turn/<turn_id>/events` |
| POST | `/api/nexus/turn/<turn_id>/interrupt` |
| GET | `/api/nexus/git` |
| GET | `/api/nexus/turns` |

## POST /api/nexus/turn

### 请求（只允许这两个字段）

```json
{
  "agent": "claude",
  "instruction": "请修改……"
}
```

- `agent`：只能是 `claude` 或 `codex`
- `instruction`：JSON string，长度 1–8000 Unicode 字符；纯文本；允许换行与制表符；拒绝 NUL 与非必要控制字符
- 明确拒绝额外字段：`cwd`、`command`、`shell`、`env`、`sandbox`、`allowedTools`、`mcp-config`、`push`、`merge`、`deploy` 等

### 响应

```json
{
  "turn_id": "…",
  "accepted": true,
  "events_url": "/api/nexus/turn/<turn_id>/events"
}
```

busy 时使用仓库既有临时锁语义：**HTTP 423 Locked**，`code: nexus_busy`。

## 统一事件

九个事件名（只能使用这些）：

`meta` | `think` | `text` | `tool_use` | `tool_result` | `status` | `git` | `done` | `err`

公共字段（只能使用这些）：

| 字段 | 说明 |
|------|------|
| `event` | 事件名 |
| `turn_id` | turn 标识 |
| `agent` | `claude` 或 `codex` |
| `sequence` | 单调递增整数 |
| `timestamp` | ISO-8601 UTC |
| `data` | 事件载荷对象 |

终态规则：

- `sequence` 单调递增
- 每个 turn 只能产生一个终态事件：`done` 或 `err`
- 确认中断：可选 `status.phase=interrupted`，再以单一 `err` 结束（`data.code = "interrupted"`，turn.state=`interrupted`）
- 中断失败终态（不得伪装成 interrupted）：`interrupt_failed` / `interrupt_rejected` / `interrupt_unconfirmed` → 单一 `err`，turn.state=`error`。强制停止失败时终态/turn record 带 `provider_stop_confirmed=false`，runtime 保持 blocked，后续 turn 返回 503 `provider_stop_unconfirmed`
- 未知原始事件归一化为 `status`
- 不得透传 token、密钥、完整环境变量与敏感绝对路径

## Git 摘要字段

只能返回：

| 字段 | 类型 |
|------|------|
| `branch` | string |
| `changed_files` | string[] |
| `diff_stat` | string |
| `additions` | number |
| `deletions` | number |
| `clean` | boolean |

只读命令固定为参数列表（不拼接用户 shell）：

- `git status --porcelain`
- `git diff --stat`
- `git diff --numstat`
- `git diff --name-only`

## Turn 状态机

`idle` → `running` → (`interrupting`) → `done` | `error` | `interrupted`

历史最多保留 50 条摘要。事件缓冲与订阅者有上限并可释放。

## 明确不实现

- Clear endpoint
- Rewind endpoint
- shell endpoint
- push / merge / deploy / reset / checkout / clean endpoint
- UI
- CLWD 接入
