# Internal State v3 规格说明

> 状态：设计冻结前草案。本文只定义 Phase 1 及后续迁移边界，不代表已经接入生产。

## 1. 总纲

- Wake 负责“什么时候醒”。
- Internal State 负责“醒来以后想做什么”。
- Longing 只是一项派生内在变量，不直接决定句长、语气或文风。
- Relationship Context 只承载关系事实、近期连续性和未完成事项，不读取 Affect、Longing 或 Drives。
- `RELATIONSHIP_CONTEXT_ENABLED` 在最小修复与 A/B 通过前保持关闭。

最终目标是一套权威状态、一套写入口、一套事件账本、一次行为结算。

事件层的一级原则：**Parse once, reduce many**。一个现实发生只建立一个 Canonical Root Event；Affect、Bond、Trace、Thought、Body、Drives 等子系统消费统一事实与 evidence，各自计算本域结果，不得重新解析同一原始用户消息、Wake outcome 或 world input 形成第二套事实。

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

Ombre 不是第二份 Current Affect Authority。当前生产中 DeepSeek 70% + Ombre 30% 的 V/A 混权可在 ISV3-1B 暂时作为 **legacy continuity heuristic / evaluator observation** 保留，以避免权威迁移时顺手改变产品情绪动力学；即使 V3 Affect 已 authoritative，Ombre 也只能作为 reducer 的一个可识别 observation 输入，不能拥有独立状态写权。最终移除固定 30% 属于后续 Affect continuity 迁移，不阻塞 State Authority Complete。

### 3.2 Bond

- `intimacy`
- `passion`
- `commitment`
- `p_updated_at`
- `i_updated_at`

Phase 1 保留现有规则层与异步评分层分别作用于 P/I 的双通道叠加，以避免迁移阶段顺手调参。两类处理必须分开记账并引用同一个用户 Root Event，后续凭真实数据决定是否降权。

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

## 5. Canonical Event Authority

建议沿用并演化现有 `internal_state_events` 作为**唯一 Canonical Event Ledger**；不要再造一张“总 Event 表”去包它。最终字段名与 Root/Event/evidence 的物理表结构留到施工时冻结，但事件权威现在冻结。

### 5.1 Root Event、evidence 与 reducer

语义边界：

- **Root Event = 世界发生了什么。** 一个现实发生只建立一个 Root Event。
- **processing / evidence = 我们拿到什么证据理解它。** rule evaluation、async scorer observation、memory retrieval result 通常属于同一 Root Event 的处理/证据，不是第二次现实发生。
- **reducer result = 这件事对内部状态造成什么。** Affect/Bond/Trace/Thought/Body/Drive 各自消费统一 Event/evidence，不得重新解释原始输入。
- **planner = 我现在想做什么。** Intent/Action 不是 Event fact。

一个 Canonical user causal chain 的概念形态：

```text
user_message:{message_id}  # Root Event
    ├─ semantic/rule assessment
    ├─ async scorer observation
    ├─ legacy Ombre observation（迁移期可选）
    └─ memory retrieval evidence（未来）
           ↓
      domain reducers
      Affect / Bond / Trace / Thought / Body / Drives
```

规则：

- Event 只描述“发生了什么”。例如 `relationship_threat` / factual summary 可以属于事件事实；`jealousy=0.7`、`attachment += 0.2`、`intent=seek_closeness` 属于 reducer/planner 结果，不能烧回 Event fact。
- 真实用户消息是 Root Event。
- 真实 Action 的成功/失败、真实 world observation 等，在确实形成新的独立经历时可以成为新的 Root Event。
- 当前 `user_rule:{message_id}` / `user_scored:{message_id}` 名称在迁移期可以继续作为幂等 processing/application 记录，但必须引用同一个 user Root Event；它们不代表用户“经历了两次”。
- `wake_outcome:{wake_run_id}` 若对应真实执行结果，则属于新的 outcome Root Event；其 Settlement 必须携带 Decision-time provenance。
- 旧迁移观测 `legacy_wake_materialize:*` / `legacy_wake_va_calibration:*` 只用于 Phase 1A 对拍，不升级为永久现实事件语义。

### 5.2 时间演化边界

Event 是新的**离散因果事实**入口，不要求为纯时间流逝制造海量伪事件。

- Longing 的 `user_idle_hours` 派生、Drive 自然增长、Trace 衰减、Body 自然恢复等“没有新事实发生，只是时间过去”的变化，使用 authoritative timestamp / snapshot 解析计算。
- 只有真正的离散产品事实（例如实际晨起 baseline 建立、明确 phase transition、真实自主 Action outcome）才按需要成为 Event。
- Event/Settlement 不得直接执行 `longing += / -=`；Longing 只认 authoritative interaction clock。

### 5.3 Memory retrieval evidence 与禁止召回自激

Memory retrieval 默认属于当前 Root Event 的 processing/evidence，不另立平行 Root Event：

```text
Canonical Root Event
    ├─ semantic assessment
    └─ memory retrieval evidence
         memory_ids / relevance / affective_salience / ...
                  ↓
             Trace reducer
```

只有随后真的发生新的独立 Action / outcome，才产生新的 Root Event。

硬红线：

- 同一 causal chain 内，内部 recall result 不得递归成为新的 recall trigger。
- 同一个 root event + 同一 memory evidence 不得对同一 Trace / state effect 重复施加同一种 retrigger。
- 具体实现可以用 event key、provenance key 或 reducer idempotency；本规格先冻结语义，不冻结字段。

### 5.4 Ombre Contract

Ombre V/A 的最终身份是 **Memory Affective Metadata / Memory Climate**，不是 Current Affect Authority。

规则：

1. Ombre 描述当前可参与记忆集合的 affective aggregate，以及单条/一组记忆的情感 metadata；不要把它严格描述成“最后 12 条聊天记忆的情绪”。eligible set / selection policy 可以随 Ombre 演化。
2. Ombre 的长期职责是 memory retrieval、semantic/topic relevance、salience、旧经历证据与记忆层 affective metadata。语义/主题相关性优先；高/低 V/A 本身不能强制召回。
3. 最终架构中，Ombre aggregate 不按固定权重直接混入 V3 Current Affect；现有 70/30 是 legacy continuity heuristic，不是永久产品常数。
4. **最终目标与当前迁移动作分离。** ISV3-1B 不因为本条已经冻结就直接把生产 70/30 改成 100/0；当前 30% 可作为 reducer 的 legacy observation 暂存，直到后续 Affect continuity 迁移单独验收。
5. 由 Ombre retrieval 引起的状态变化必须留在当前 Canonical Root Event causal chain 中：retrieval evidence → V3 reducer。Ombre 本身无 Affect/Bond/Drive/Trace/Thought/Body 写权。
6. Legacy Ombre observation 必须作为 observation/evidence 可识别地保留 provenance，不能静默烧进 Canonical Event fact。未来移除 30% 时不需要重写 Event 历史。
7. `/emotion_snapshot` 的 endpoint 名称未来可以调整以避免误解，但 endpoint rename 不是本轮施工内容。

一句话边界：**Ombre 负责“我记得什么，以及现在什么旧经历值得被想起”；V3 Affect 负责“眼前发生的事与被召回的旧经历，此刻让我感觉怎样”。**

## 6. 写接口

```python
observe_user_message(message_id, text, created_at)
observe_scored(message_id, scores, scored_at)
apply_outcome(event_id, intent, result)
```

### 6.1 `observe_user_message`

同一事务内：

1. 以 `message_id` 建立/确认唯一 user Root Event。
2. 读取事件发生前的权威快照。
3. 记录 `longing_before_reunion`。
4. 运行规则/语义 assessment，并把结果作为该 Root Event 的 processing/evidence；应用关键词层 Affect/Bond delta。
5. 记录旧 attachment 固定减法结算结果与乘性候选结果。
6. Phase 1 权威迁移初期沿用旧 attachment 结算，不启用新乘性参数。
7. fatigue 恢复沿用旧行为，候选方案仅 shadow。

`get_reunion_boost()` 当前为死代码：

- 只记录 `candidate_reunion_boost`。
- 不实际增加 intimacy 或 attachment。

### 6.2 `observe_scored`

- `user_scored` 是同一 user Root Event 的异步 scorer processing/application 记录，不是第二个现实 Root Event。
- 幂等 key 保证同一评分结果只应用一次。
- 必须防乱序覆盖。
- 仅当 `message_id > last_scored_message_id` 时更新状态。
- 旧评分迟到时仍落处理记录，标记 `stale_skipped`，不覆盖当前状态。
- 统计 `stale_score_count` 与 `stale_score_rate`。
- DeepSeek / legacy Ombre 等 assessment observation 必须与 Canonical Event fact 分离并保留可识别 provenance。

若迟到率不可接受，后续改为单写入队列或可重放投影；不能把 stale skip 描述成零代价方案。

### 6.3 `apply_outcome`

同一 Action outcome 只能结算一次：

- 消费 Decision-time provenance；不得根据最终 action 事后猜 fired drive / intent。
- 降低被满足的驱动。
- 增加 fatigue 成本。
- 写回新的基准值和时间。
- 同事务写 outcome Root Event / Settlement application 记录。

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
-- INSERT event/application key，确保唯一
-- 校验 expected state_version
-- UPDATE internal_state_v3
-- 写 state_version + 1
COMMIT;
```

要求：

- 状态行与 Event/application 记录同事务提交。
- 设置合理 `busy_timeout`。
- 发生版本冲突时重读、重算并有限次数重试。
- 不允许静默覆盖。
- 同一个 root event + evidence + reducer effect 不得重复应用。

切换前人工检查：

```sql
PRAGMA journal_mode;
PRAGMA busy_timeout;
```

WAL 有利于并发，但是否开启必须在维护窗口显式决定；迁移代码不得偷偷修改 journal mode。

## 11. Phase 计划

### Phase 1A：权威表 + 事件账本 + Shadow Write

- 建立 `internal_state_v3` 与 `internal_state_events`。
- 把 `internal_state_events` 按 Canonical Event Ledger 方向演化；先冻结 root/evidence/reducer 语义，不急着拍最终字段。
- 实现三个幂等写接口。
- 旧系统继续掌权。
- 新系统 shadow write，不接 prompt，不接 Wake 决策。
- 覆盖所有写入者，包括 Wake materialize 与 VA calibration。
- 并排记录 legacy 与 candidate settlement。
- 现有 DeepSeek 70% + Ombre 30% 继续按 legacy evaluator behavior 对拍，不在本阶段改变产品动力学。

### Phase 1B：Wake 切换

只在满足硬门槛后执行：

- 至少连续 72 小时 shadow write。
- `user_rule / user_scored / wake_outcome` 等迁移记录均有有效样本，并能追溯到正确 Root Event / outcome。
- Wake materialize / VA calibration 已纳入对拍。
- 所有已知写入口计数一致。
- duplicate applied = 0。
- 同一 causal chain 的 recall/retrigger 不存在递归自激或重复应用。
- 未解释的 DB lock / rollback = 0。
- 状态值越界 = 0。
- dominant drive / intent 无未解释分歧。
- legacy 数学复现 diff 在预设容差内。
- stale score rate 已记录并评估。
- Wake 样本数足够；三天日历不能替代样本量。
- 存在一个开关可立即回退旧 Wake consumer/decision path，同时保持 V3 事实源不被破坏。

切换内容：

- 调度器仍按 effective idle。
- Wake 改读 `Wake View`。
- 行为后只调用一次 `apply_outcome()`。
- 停止双 discharge / satisfy。
- **不因为 Ombre 最终身份已经冻结而把生产 70/30 改成 100/0。** Ombre 30% 的移除属于后续 Affect continuity 迁移。

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
- Ombre 的固定 30% 是否移除、如何由 retrieval/Trace continuity 接班，必须作为单独迁移项验收，不与旧表删除绑死。

## 12. 非目标

Phase 1 不做：

- 不改 Wake 调度概率。
- 不启用 reunion boost。
- 不启用 attachment 调制 tau。
- 不采用新 attachment 乘性参数作为权威结算。
- 不把 mood/longing/drive 文案直接注入 Chat prompt。
- 不删除旧表。
- 不恢复 `RELATIONSHIP_CONTEXT_ENABLED=1`。
- 不因为最终 Ombre Contract 已冻结就修改生产 DeepSeek/Ombre 70/30 权重。
- 不为替代 Ombre 30% 提前扩建 Affect Trace / memory retrieval continuity。
- 不在本轮重命名 `/emotion_snapshot` endpoint。
