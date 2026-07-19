# HayaGarden Frontend

HayaGarden 是部署在个人 VPS 上的 React 仪表盘与 Flask 服务集合。主站 `app.py` 负责页面和持久化 API，`gateway.py` 负责模型流式生成、工具调用以及多 Agent 调度；运行时数据保存在 `/opt/frontend/memories.db`。

## 葡萄海大富翁游戏室

当前版本提供“**双人棋局 · 三人聊天**”：哈娅和 CC 参与棋局，Codex 位于观察席。三人引擎仍属于后续 Fork，不在这一阶段伪装开放。

后端遵守四条边界：

- 棋盘真值只来自自建 `spicy-monopoly` 引擎，网关通过 `127.0.0.1:8069` 直连 REST，不经过 MCP。
- 任务、真心话、过路费、对决和超级任务采用 lazy 悬账；选择先写入房间，下一次 roll 才结算。
- CC 每轮读取主聊天当前线路；Codex 固定 OpenAI 官端，离线时不会降级到中转。
- AI 只能提交结构化意图，文本不能直接修改金币、位置、卡牌或回合。

主要模块：

| 文件 | 职责 |
| --- | --- |
| `monopoly_engine.py` | 引擎 REST 客户端、完整动作覆盖、roll 超时对账与不确定结果冻结 |
| `monopoly_rooms.py` | 房间状态机、悬账、跨进程锁、真实玩家名映射、精确安全词 |
| `monopoly_store.py` | 房间、事件、消息持久化与 WAL 并发读取 |
| `monopoly_routes.py` | `/api/monopoly/rooms...` 与单一 SSE |
| `monopoly_agents.py` | CC/Codex 适配器、发言预算、拒绝后的 swap/skip 降级 |

### 本地验证

```bash
python3.11 -m py_compile \
  monopoly_engine.py monopoly_store.py monopoly_rooms.py \
  monopoly_routes.py monopoly_agents.py codex_app_server.py
python3.11 -m unittest tests.test_monopoly_backend
```

### 部署

引擎必须只监听 loopback，并将存档目录纳入备份。systemd 模板、环境变量、接口说明和上线冒烟步骤见 [`docs/monopoly-backend.md`](docs/monopoly-backend.md)。整体服务关系见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

当前适配器按上游真实契约读取 `/roll` 的 `next_turn` 和 `/state` 的 `turn`，并在开局前从 `/help` 获取当前 `rules_ack`。安全词默认是 `404`，只有整条消息（或显式 `safeWord` 字段）命中才暂停；“HTTP 404”不会误停。若 roll 已生效但响应丢失，由于上游 `/state` 不返回本轮事件和悬账，房间会冻结并要求人工对账，绝不会猜成 `IDLE` 或再次掷骰。

## 目录速览

- `app/`：React dashboard。
- `app.py`：主站 Flask 服务，默认端口 5050。
- `gateway.py`：AI 网关，默认端口 5051。
- `chat/`、`relay/`：上下文构建和模型线路适配。
- `tools/`：受控工具、备份与后台任务。
- `tests/`：后端单元与回归测试。

