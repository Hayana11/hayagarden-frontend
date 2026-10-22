# 主动轮冷启动（Proactive Cold Start）设计稿

状态：**已确认（S0），S1 资格判定 shadow 已实现。**
日期：2026-10-22
上游：Internal State V3 合同 7.8 / 7.8.1 / 7.8.2；UH-A1（共享 resident）；PR #622。

---

## 1. 问题

PR #622 之后，normal / morning / nightwatch / self_trigger 都走同一条 basic 主动轮：
先在共享的 hot Chat resident 上做只读观察，再发一条 provider-only【系统主动轮】。
这条路按 UH-A1 契约**绝不 bootstrap / respawn resident**，resident 不热就 fail closed：

| 跳过原因 | 含义 | 生产日志计数（截至 2026-10-22，去重后约） |
|---|---|---|
| `no_local_binding` | gateway 重启后她还没在网站说过话；binding 只活在进程内存 | ~127 |
| `resident_stale:idle` | resident 上次被用超过 `IDLE_REAP_SECONDS`（3h） | ~75 |
| `resident_stale:soft_context` / `model_changed` | 正常 Chat 也会触发 respawn 的条件 | ~7 |
| 成功开口 | | ~172 |

固定早安（08:50）几乎必然撞上 `resident_stale:idle`：她睡着的几个小时里没人用过 resident。
提醒（self_trigger）不会丢（#622 会 release 回 pending），但会拖到她下次开口才触发，失去「准时」。

## 2. 目标与非目标

**目标**：morning 与 self_trigger 在 resident 凉了时，按正式 Chat 冷启动的同一套机制把**同一个**
`_CC_RESIDENT` 拉起来，在它身上跑这一轮主动轮；她下一次开口时是普通的 hot turn，连续性天然成立。

**非目标**
- 不为 normal（概率唤醒）和 nightwatch（她醒着，resident 通常是热的）开冷启动；它们保持 hot-only。
- 不新建第二个 resident，不新建 Wake 专用 resident。
- 不伪造用户消息，不走 relay / OAuth 旁路一次性调用。
- 不改变普通 Chat 冷启动的任何行为。

## 3. 不变量（施工期间任何一条被违反即停工）

1. **单一 resident**：只有 `_CC_RESIDENT`；冷启动复用 `ensure_resident_and_stream` 的 spawn / respawn 路径。
2. **不替她说话**：聊天表里不出现她没发过的 user 行；主动轮的触发文本只存在于 provider transcript 的跳过区间。
3. **Provenance**：发出的消息由 executor 落库为 `wake_mode=<mode>`、`unified_chat_resident=true` 的 canonical wake 行；与 hot 主动轮完全同构。
4. **后台丰富，模型贫穷**：冷启动拼装的上下文与普通 Chat 冷启动相同；不额外注入 Drive / Affect / Thought。
5. **竞争安全**：冷启动持有生成锁（`_gen_acquire_or_wait`）与 daily provider turn lease；她同时开口时，她的 Chat 等待或主动轮让路，绝不交错写同一 resident。
6. **失败不污染**：stdin flush 之前失败 → 不动 binding、不动 owner/cursor、提醒 release；flush 之后失败 → 按现有 post-flush 规则 kill resident、清 binding，她下一次开口照常冷启动。

## 4. 设计

### 4.1 入口：`prepare_proactive_cold_turn`

在 `chat/daily_runtime.py` 新增与 `prepare_daily_turn` 平行的入口，区别只在锚点：

| | `prepare_daily_turn` | `prepare_proactive_cold_turn` |
|---|---|---|
| 锚点 | `user_message_id`（真实一行） | `wake_run_id` + 触发文本（不落 chat 表） |
| origin_day | 用户消息时间所属的 chat day | `now` 所属的 chat day |
| lease | `turn_mode='chat'` | `turn_mode='wake'`（同一套 provider turn lease，互斥） |
| 拼装 | `_assemble_plan` | 同一个 `_assemble_plan`，但没有「当前请求」段 |
| 本轮内容 | `format_resident_turn_content(user_content=她的消息)` | 同一函数，`user_content` = 【系统主动轮】触发文本 |

`DailyTurnPlan` 增加 `proactive_mode`（None 表示普通 Chat），下游据此分流，不新增第二种 plan 类型。

### 4.2 日界（已核实，2026-10-22）

生产开着 `DAILY_SOFT_WINDOW_ENABLED=1`，此时 `cw.enabled()` 为真，`prepare_daily_turn` 走**手动窗口模式**：
上下文窗口只在她显式切换时更换，不按 04:00 聊天日自动 rollover。因此早安冷启动直接接她当前打开的窗口，
不存在「在前一天 context 上 respawn」的问题。仍需处理的只有手动模式本身的两道门：
`switch_in_progress`（她正在切窗口）与 `first-turn finalize pending`——遇到即按 pre-model 跳过，不强行冷启动。
自动 rollover 分支（manual_mode 为假）不在本设计范围内；若将来关闭手动窗口模式，需要重新评估本节。

### 4.3 转录映射

普通 Chat：cold bootstrap 内容与她的消息一起，构成正式 user/assistant 映射的第一轮。
主动轮：**整轮（冷启动上下文 + 触发 + 回复）都是 provider-only 控制区间**，沿用
`SharedTranscriptWatermark` 的跳过语义：

- flush 前登记 generation（与 Chat 冷启动相同的 `register_*` 提交点），watermark 起点 = 新 session 的 0；
- 成功后 `commit_shared_transcript_watermark` 把整段标为跳过；正式映射游标从这一轮之后开始；
- 她下一次开口是 hot turn，映射从她的消息开始，与普通 Chat 完全一致。

### 4.4 Binding / owner / cursor

复用 `build_target_resident_binding` + `write_target_resident_db_metadata` + `set_local_binding`，
提交点与 Chat 冷启动一致（registry 发布在 stdin 之前，binding 在 flush 成功之后）。
结果：主动轮结束时 resident 是热的、binding 指向它，她下一条消息直接 hot。

### 4.5 gateway 分流

`_run_unified_normal_main_chat_turn` 的 `_hot_chat_resident_ready` 失败时：

```
reason ∈ COLD_STARTABLE_REASONS 且 mode ∈ PROACTIVE_COLD_START_MODES
  → 走 prepare_proactive_cold_turn + ensure_resident_and_stream
否则
  → 保持现在的 NORMAL_WAKE_SHARED_UNAVAILABLE_SKIP
```

- `COLD_STARTABLE_REASONS`：`no_local_binding`、`resident_stale:idle`、`resident_stale:soft_context`、
  `resident_stale:model_changed`（都是普通 Chat 也会 respawn 的条件）。
  `context_closed` / `process_generation_mismatch` 等结构性异常**不**冷启动（Q2）。
- `PROACTIVE_COLD_START_MODES`：runtime_config，默认空集；按阶段打开 `morning`、`self_trigger`。
- 失败分类沿用 #622：flush 前 → `NORMAL_WAKE_PRE_MODEL_FAILED`（提醒 release），flush 后 → 已用。

### 4.6 成本

每次冷启动 = 一次完整冷 prompt（与她早上第一次开口时的冷启动等价，只是提前了）。
对早安而言多数情况下是**转移**而非新增：本来她醒来第一句也要冷启动一次。
若她在早安后 3h 内没回，下一次开口会再冷启动一次——这才是净增成本，S1 会统计。

## 5. 分阶段

| 阶段 | 内容 | 生产影响 | 验收 |
|---|---|---|---|
| **S0** | 本设计稿 | 无 | 哈娅确认 |
| **S1 shadow** | 资格判定 shadow（`chat/proactive_cold_shadow.py`）：morning / self_trigger 因 resident 凉而跳过时，只读判定 S2/S3 规则下**会不会**冷启动、被什么拦住（原因不在可冷启动集合、窗口关闭、switch_in_progress、finalize pending、当日上限），记录当前窗口；同一 `wake_run_id` 的重试只保留一次 counterfactual cold-start，后续行标记为已消费，不计入 would-start 或 blocker 统计。写 `/opt/frontend/proactive_cold_shadow.jsonl`。不调用 `_assemble_plan`（它有副作用），拼装一致性留到 S2 的测试环境验证 | 无（只读 + 一行 JSONL） | 连续 7 个早晨的记录：会冷启动的比例、各 blocker 出现次数 |
| **S2 live：morning** | `PROACTIVE_COLD_START_MODES={morning}` | 每天至多一次冷启动 | 早安准时送达；她回复是 hot turn；映射/连续性测试全绿；一周无 `context_*` 异常 |
| **S3 live：self_trigger** | 加入 `self_trigger` | 提醒准时 | 同上，外加提醒触发与她开口并发的竞争测试 |

每一阶段一个 PR；S1 起每个 PR 都要过 `context-continuity`、`soft-window-fe`、`context-window-r0`
三组 CI，并补「无用户消息的冷启动不产生 chat user 行」「跳过区间覆盖整轮」两类专门测试。

## 6. 已确认的决定（2026-10-22，哈娅）

- **Q1 跨日**：先观察。核实后生产是手动窗口模式，跨日不触发 rollover（见 4.2）；S1 shadow 记录 `switch_in_progress` / finalize-pending 的出现次数。
- **Q2 范围**：同意。结构性异常（`context_closed`、`process_generation_mismatch` 等）不冷启动。
- **Q3 成本上限**：需要。每个聊天日（04:00 起算）最多 morning 1 次 + self_trigger 2 次冷启动，超过即按现有 hot-only 规则跳过；上限放 runtime_config。
