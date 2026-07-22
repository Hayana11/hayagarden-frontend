# HayaGarden Frontend

HayaGarden 是部署在个人 VPS 上的 React 仪表盘与 Flask 服务集合。主站 `app.py` 负责页面和持久化 API，`gateway.py` 负责模型流式生成、工具调用以及多 Agent 调度；运行时数据保存在 `/opt/frontend/memories.db`。

## 葡萄海大富翁游戏室

当前版本提供“**双人棋局 · 三人聊天**”：哈娅和 CC 参与棋局，Codex 位于观察席。三人引擎仍属于后续 Fork，不在这一阶段伪装开放。

后端遵守四条边界：

- 棋盘真值只来自自建 `spicy-monopoly` 引擎，网关通过 `127.0.0.1:8069` 直连 REST，不经过 MCP。
- 任务、真心话、过路费、对决和超级任务采用 lazy 悬账；选择先写入房间，下一次 roll 才结算。
- CC 每轮读取主聊天当前线路，并区分 Anthropic 官端、真实中转名和 Claude Code；Codex 固定 OpenAI 官端，离线时不会降级到中转。
- 整个 `/api/monopoly` blueprint 复用 Moments owner session/Bearer 鉴权，未登录者不能读房、建房或触发模型生成。
- AI 只能提交结构化意图，文本不能直接修改金币、位置、卡牌或回合。

主要模块：

| 文件 | 职责 |
| --- | --- |
| `monopoly_engine.py` | 引擎 REST 客户端、完整动作覆盖、所有变更请求的不确定结果冻结 |
| `monopoly_rooms.py` | 房间状态机、悬账、跨进程锁、真实玩家名映射、精确安全词 |
| `monopoly_store.py` | 房间、游戏事件、临时 live 事件、消息持久化与 WAL 并发读取 |
| `monopoly_routes.py` | owner 鉴权保护的 `/api/monopoly/rooms...` 与单一 SSE |
| `monopoly_agents.py` | CC/Codex 适配器、发言预算、明确内容拒绝后的 swap/skip 降级 |
| `app/src/screens/MonopolyRoomScreen.tsx` | 游戏室页面、REST/SSE 编排和响应式双栏布局 |
| `app/src/components/monopoly/` | 棋盘、悬账操作区、手牌、设置抽屉与三人聊天组件；详见目录内 README |

### 本地验证

```bash
python3.11 -m py_compile \
  monopoly_engine.py monopoly_store.py monopoly_rooms.py \
  monopoly_routes.py monopoly_agents.py codex_app_server.py
python3.11 -m unittest tests.test_monopoly_backend

cd app
pnpm run lint
pnpm run build
```

### 部署

引擎必须只监听 loopback，并将存档目录纳入备份。systemd 模板、环境变量、接口说明和上线冒烟步骤见 [`docs/monopoly-backend.md`](docs/monopoly-backend.md)。整体服务关系见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

当前适配器按上游真实契约读取 `/roll` 的 `next_turn` 和 `/state` 的 `turn`，并在开局前从 `/help` 获取当前 `rules_ack`。安全词默认是 `404`，只有整条消息（或显式 `safeWord` 字段）命中才暂停；“HTTP 404”不会误停。任何可能改变棋盘的请求只要得到空响应、坏 JSON、HTTP 5xx 或连接中断，房间都会冻结并要求人工对账；上游没有完整结果缓存或幂等键前，后端绝不会自动重发。`final_result` 也只调用一次：若已取得终局 payload、只是 state 刷新失败，恢复仅重试 state；若终局响应本身丢失，则保持冻结。

聊天流式 `chat.start/delta/done` 使用独立 live 队列，不递增游戏 `event_seq`，所以 AI 打字不会让操作按钮持续收到 `STALE_ROOM_STATE`，也不会污染 AI 的最近游戏事件。网络异常只更新线路状态并允许稍后重试；只有明确内容拒绝才消耗 swap/skip。

## 目录速览

- `app/`：React dashboard。
- `app.py`：主站 Flask 服务，默认端口 5050。
- `gateway.py`：AI 网关，默认端口 5051。
- `chat/`、`relay/`：上下文构建和模型线路适配。
- `tools/`：受控工具、备份与后台任务。
- `tests/`：后端单元与回归测试。
