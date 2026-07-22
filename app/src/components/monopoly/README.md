# 葡萄海大富翁前端

大富翁游戏室是一个独立的 React 全屏页面，入口为 `/dash/monopoly/:roomId`。通讯录里的「葡萄海大富翁」会先进入 `/dash/monopoly/new`，创建房间后替换成真实房间地址。

## 设计边界

- 棋盘、金币、位置、回合和悬账都只渲染后端快照；聊天内容不能直接改变棋局。
- 页面只调用 `/api/monopoly/rooms...`，不复用 `/chat/stream`，也不读写 Internal State、Wake、Shadow、评分或 outbox。
- `expectedEventSeq` 取当前房间快照的 `event_seq`；收到 409 时刷新快照，不自动重放动作。
- CC 的线路铭牌使用后端返回的脱敏 `provider_meta`；Codex 只显示官端状态。
- 安全词 `404` 会显式带 `safeWord: true` 发给房间消息接口，最终暂停仍由服务端裁决。

## 组件

| 文件 | 职责 |
| --- | --- |
| `MonopolyRoomScreen.tsx` | 页面编排、建房跳转、骰子反馈和抽屉状态 |
| `MonopolyBoard.tsx` | 20 格棋盘、棋子、回合和掷骰入口 |
| `PendingCard.tsx` | 任务、真心话、对决、过路费、超级任务悬账 |
| `GameActionDock.tsx` | 按房间状态给出唯一合法的一组动作 |
| `HandDock.tsx` | 功能卡、弃牌、身份提醒和身份主动技 |
| `MiniRoomChat.tsx` | 三人消息、流式临时消息、目标选择和继续聊 |
| `SetupDrawer.tsx` | 两步设置与 `active_limits` 生效结果确认 |

数据与连接逻辑分别位于 `lib/monopolyRoom.ts`、`hooks/useMonopolyRoom.ts` 和 `hooks/useRoomStream.ts`。

## Lazy 结算

任务类按钮的文案和请求保持 lazy 语义：玩家选择「做完／跳过／交钱／买断」后，决定随下一次 `roll` 一并结算。`DUEL_PENDING` 是例外：必须先提交胜者，服务端确认前没有掷骰入口。即时换卡仍走 `swap`。

| 房间状态 | 主操作 |
| --- | --- |
| `idle` | 掷骰子 |
| `task_pending` | 做完并掷下一轮、跳过、换卡 |
| `duel_pending` | 选择哈娅或 CC 为胜者 |
| `toll_pending` | 交钱并掷下一轮、劳动抵债 |
| `super_pending` | 做完、买断 |
| `jail_turn` | 狱中掷骰 |
| `paused` / `engine_down` | 调用 `/resume` 对账恢复；未知 mutation 收到 409 后继续冻结 |

## SSE

页面维持一个房间 EventSource，处理这些命名事件：

- `room.snapshot`
- `game.event`、`game.state`、`game.pending`
- `chat.message`、`chat.start`、`chat.delta`、`chat.done`
- `agent.status`、`room.error`

刷新时先用 REST 取完整快照，再由 SSE 接续。流式 delta 只更新临时气泡，最终消息仍以后端持久化消息为准。

## 响应式

- `>= 980px`：棋局列与 390px 房间聊天并排。
- `< 980px`：聊天固定在底部，默认 206px，可上拉至 `70dvh`。
- `<= 430px`：压缩棋盘间距和铭牌信息，操作按钮改为纵向，保留安全区内边距。

## 本地验证

```bash
cd app
pnpm run lint
pnpm run build
```

视觉验收应至少覆盖 1280×900 与 430×932，并检查 `idle`、五种 pending、`jail_turn`、暂停、引擎离线和终局状态。
