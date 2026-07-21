# Internal State v3 规格说明

> 状态：设计冻结前草案。本文只定义 Phase 1 及后续迁移边界，不代表已经接入生产。

## 1. 总纲

- Wake 负责“什么时候醒”。
- Internal State 负责“醒来以后想做什么”。
- Longing 只是一项派生内在变量，不直接决定句长、语气或文风。
- Relationship Context 只承载关系事实、近期连续性和未完成事项，不读取 Affect、Longing 或 Drives。
- `RELATIONSHIP_CONTEXT_ENABLED` 在最小修复与 A/B 通过前保持关闭。

最终目标是一套权威状态、一套写入口、一套事件账本、一次行为结算。

## 2. 权威时钟

保留 `chat.interaction_state.read_interaction_clock()` 作为唯一权威来源：

- `user_idle_hours`：距最后一条真实用户消息的时长；用于 Longing。
- `effective_idle_hours`：取真实用户消息与最近 Wake message 中更近者；用于 Wake 限流和概率。

规则：

- Wake message 不重置 `user_idle_hours`。
- Longing 不读取 `emotion_state.last_interaction` 或 `desire_state.last_hayana_msg_time`。
- 时钟不可读时 fail closed，不制造 999h。

## 3. 权威状态表

建议表名：`internal_state_v3`。

### 3.1 Affect

- `pa`
- `na`
- `valence`
- `arousal`
- `mood_word`
- `mood_source_message_id`

`mood_word` 仅供兼容壳、调试面板和历史展示使用：

- 不进入 Chat View。
- 不影响句长、语气、文风或“话少一些”之类形式指导。

### 3.2 Bond

- `intimacy`
- `passion`
- `commitment`
- `p_updated_at`
- `i_updated_at`

Phase 1 保留现有规则层与异步评分层分别作用于 P/I 的双通道叠加，以避免迁移阶段顺手调参。两类事件必须分开记账，后续凭真实数据决定是否降权。

### 3.3 Drives

- `attachment`
- `curiosity`
- `reflection`
- `social`
- `duty`
- `libido`
- `stress`
- `fatigue`
- `drives_updated_at`

驱动读取时采用解析解计算自然增长；Longing、Passion、NA 等联动在同一快照内显式计算，不允许跨模块隐式读取。

### 3.4 版本与并发字段

- `state_version`
- `last_scored_message_id`
- `updated_at`

## 4. Longing

Phase 1 正式候选采用固定 `tau = 18h`，只由 `user_idle_hours` 派生：

```text
L = 0.85 * (1 - (1 + t / 18)^(-0.8))
clamp <= 0.90
```

原则：

- 不单独落库。
- 不受 Wake message 重置。
- 不直接写入 prompt。
- 只参与 attachment cap、intent 判断和调试视图。

保留两条对照：

- `longing_emotion_legacy`：tau=8，仅作旧公式对照。
- attachment 调制 tau：保留实验代码，不在 Phase 1 启用。

## 5. 事件账本

建议表名：`internal_state_events`。

字段至少包括：

- `event_key` UNIQUE
- `event_type`
- `source_id`
- `payload_json`
- `payload_hash`
- `status`：`applied | duplicate | stale_skipped | shadow_only | failed`
- `state_version_before`
- `state_version_after`
- `applied_at`
- `error`

正式事件：

- `user_rule:{message_id}`
- `user_scored:{message_id}`
- `wake_outcome:{wake_run_id}`

迁移期兼容观测：

- `legacy_wake_materialize:{wake_run_id}`
- `legacy_wake_va_calibration:{wake_run_id}`

后两者只用于 Phase 1A 对拍，不应直接被宣布为永久行为。

## 6. 写接口

```python
observe_user_message(message_id, text, created_at)
observe_scored(message_id, scores, scored_at)
apply_outcome(event_id, intent, result)
```

### 6.1 `observe_user_message`

同一事务内：

1. 以 `message_id` 建立唯一事件。
2. 读取事件发生前的权威快照。
3. 记录 `longing_before_reunion`。
4. 应用关键词层 Affect/Bond delta。
5. 记录旧 attachment 固定减法结算结果与乘性候选结果。
6. Phase 1 权威迁移初期沿用旧 attachment 结算，不启用新乘性参数。
7. fatigue 恢复沿用旧行为，候选方案仅 shadow。

`get_reunion_boost()` 当前为死代码：

- 只记录 `candidate_reunion_boost`。
- 不实际增加 intimacy 或 attachment。

### 6.2 `observe_scored`

- `event_key` 保证幂等。
- 必须防乱序覆盖。
- 仅当 `message_id > last_scored_message_id` 时更新状态。
- 旧评分迟到时仍落账，标记 `stale_skipped`，不覆盖当前状态。
- 统计 `stale_score_count` 与 `stale_score_rate`。

若迟到率不可接受，后续改为单写入队列或可重放投影；不能把 stale skip 描述成零代价方案。

### 6.3 `apply_outcome`

同一 Wake 事件只能结算一次：

- 降低被满足的驱动。
- 增加 fatigue 成本。
- 写回新的基准值和时间。
- 同事务写事件账本。

Phase 1 不拍新参数：

- attachment 先沿用旧 `-0.55`。
- desire 中已有乘性 ratio 的维度按旧映射对拍。
- attachment 乘性回落作为 candidate 并排记录，观察后再裁决。

## 7. Wake 边界

Wake 调度器暂不改：

- 按 `effective_idle_hours` 做最低间隔和概率。
- 通过后调用 `/wake`。

Wake 内部最终改为：

1. 读取唯一 `Wake View`。
2. 决定 `message | explore | none`。
3. 行为结束后仅调用一次 `apply_outcome()`。

### 7.1 两个现役 Wake 写入者

当前 live Wake 开始前还有：

- `drive_engine._flush(get_drive())`
- `desire.calibrate_va(V, A)`

Phase 1A 不直接删除：

- 同时记录“旧物化/校准后结果”与“纯读时计算候选结果”。
- 对拍证明无实质差异后，Phase 1B 才允许移除。
- 切换期间禁止在新旧两边重复应用同一效果。

## 8. 决策映射

必须按模式明示，不能假设所有视图共享同一映射。

| Drive | Wake View | Chat View |
|---|---|---|
| attachment | `message` | `reassure_attachment` / `express_longing` |
| curiosity | `explore` | `pursue_curiosity` |
| reflection | `explore` | `share_reflection` |
| social | `explore` | `pursue_curiosity` |
| stress | `message` 或专用行为 | `release_stress` |
| libido | `message` 或专用行为 | `seek_closeness` |
| duty | Phase 1A 同时记录旧 `message` 与旧 `none` 两套决策，后续裁决 | `none` |
| fatigue blocked | `none` | 不改变文风，只阻止主动行为 |

## 9. 三个视图

### Debug View

完整输出 Affect、Bond、Drives、Longing、timestamps、source health、warnings、diff。

### Wake View

只输出结构化决策字段，例如：

```json
{
  "longing": 0.42,
  "dominant_drive": "attachment",
  "intent": "express_longing",
  "fatigue_blocked": false
}
```

### Chat View

只允许固定枚举和机器 token：

```json
{
  "intent": "reassure_attachment",
  "intensity": 0.58,
  "content_targets": ["confirm_being_thought_of"]
}
```

严禁输出或生成：

- 低唤醒 / 高唤醒
- 偏暖 / 偏低
- 话少一些
- 简短回应
- 安静等待
- 任何句长、段落、语气、文风指令

## 10. 并发与事务

app、gateway、wake 为不同进程，共用 `memories.db`。

每次状态更新必须：

```sql
BEGIN IMMEDIATE;
-- INSERT event_key，确保唯一
-- 校验 expected state_version
-- UPDATE internal_state_v3
-- 写 state_version + 1
COMMIT;
```

要求：

- 状态行与事件行同事务提交。
- 设置合理 `busy_timeout`。
- 发生版本冲突时重读、重算并有限次数重试。
- 不允许静默覆盖。

切换前人工检查：

```sql
PRAGMA journal_mode;
PRAGMA busy_timeout;
```

WAL 有利于并发，但是否开启必须在维护窗口显式决定；迁移代码不得偷偷修改 journal mode。

## 11. Phase 计划

### Phase 1A：权威表 + 事件账本 + Shadow Write

- 建立 `internal_state_v3` 与 `internal_state_events`。
- 实现三个幂等写接口。
- 旧系统继续掌权。
- 新系统 shadow write，不接 prompt，不接 Wake 决策。
- 覆盖所有写入者，包括 Wake materialize 与 VA calibration。
- 并排记录 legacy 与 candidate settlement。

### Phase 1B：Wake 切换

只在满足硬门槛后执行：

- 至少连续 72 小时 shadow write。
- `user_rule / user_scored / wake_outcome` 均有有效样本。
- Wake materialize / VA calibration 已纳入对拍。
- 所有已知写入口计数一致。
- duplicate applied = 0。
- 未解释的 DB lock / rollback = 0。
- 状态值越界 = 0。
- dominant drive / intent 无未解释分歧。
- legacy 数学复现 diff 在预设容差内。
- stale score rate 已记录并评估。
- Wake 样本数足够；三天日历不能替代样本量。
- 存在一个开关可立即回退旧 Wake。

切换内容：

- 调度器仍按 effective idle。
- Wake 改读 `Wake View`。
- 行为后只调用一次 `apply_outcome()`。
- 停止双 discharge / satisfy。

### Phase 2：兼容壳

旧接口保留，但内部改读新心脏：

- `emotion_engine.get_state()`
- `emotion_engine.get_desire()`
- `drive_engine.get_drive()`
- `desire.get_longing()`

### Phase 3：普通聊天切换

- 先 A/B 启用 Chat View。
- 检查回复长度、完整性、碎片化和文风污染。
- persona 单独运行仍应稳定。
- Relationship Context 仅在最小修复和独立 A/B 后恢复。

### Phase 4：清理

- 删除旧写路径。
- 停止旧表写入。
- 完成回滚窗口后再迁移或归档旧表。

## 12. 非目标

Phase 1 不做：

- 不改 Wake 调度概率。
- 不启用 reunion boost。
- 不启用 attachment 调制 tau。
- 不采用新 attachment 乘性参数作为权威结算。
- 不把 mood/longing/drive 文案直接注入 Chat prompt。
- 不删除旧表。
- 不恢复 `RELATIONSHIP_CONTEXT_ENABLED=1`。
