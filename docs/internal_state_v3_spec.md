# Internal State v3 规格说明

> 状态：架构冻结合同。本文定义 ISV3-1B 的权威迁移边界，以及之后 Behavior Authority、Chat Exposure 与新能力扩展的分界；不代表所有后续能力已经接入生产。

## 1. 总纲

- Wake 调度器负责“什么时候获得一次自主行动机会”。
- Internal State 负责保存统一内在事实并支撑“醒来以后想做什么”。
- Longing 只是一项时间派生内在变量，不直接决定句长、语气或文风。
- Relationship Context 只承载关系事实、近期连续性和未完成事项，不读取 Affect、Longing 或 Drives。
- `RELATIONSHIP_CONTEXT_ENABLED` 在最小修复与 A/B 通过前保持关闭。

最终目标是一套权威状态、一套写入口、一套事件账本、一次行为结算。

事件层一级原则：**Parse once, reduce many**。一个现实发生只建立一个 Canonical Root Event；Affect、Bond、Trace、Thought、Body、Drives 等子系统消费统一事实与 evidence，各自计算本域结果，不得重新解析同一原始用户消息、Wake outcome 或 world input 形成第二套事实。

权威迁移一级原则：**ISV3-1B 的任务是统一现有器官的权威，不是提前长出新器官。**

## 2. 权威时钟

保留 `chat.interaction_state.read_interaction_clock()` 作为唯一权威来源：

- `user_idle_hours`：距最后一条真实用户消息的时长；用于 Longing。
- `effective_idle_hours`：取真实用户消息与最近 Wake message 中更近者；用于 Wake 限流和概率。

规则：

- Wake message 不重置 `user_idle_hours`。
- Longing 不读取 `emotion_state.last_interaction` 或 `desire_state.last_hayana_msg_time`。
- 时钟不可读时 fail closed，不制造 999h。

## 3. 权威状态表与语义类型

建议表名：`internal_state_v3`。

### 3.1 Affect

- `pa`
- `na`
- `valence`
- `arousal`
- `mood_word`
- `mood_source_message_id`

`mood_word` 仅供兼容壳、调试面板和历史展示使用：

- 不进入普通 Chat。
- 不影响句长、语气、文风或“话少一些”之类形式指导。

Ombre 不是第二份 Current Affect Authority。当前生产中 DeepSeek 70% + Ombre 30% 的 V/A 混权可在 ISV3-1B 暂时作为 **legacy continuity heuristic / evaluator observation** 保留，以避免权威迁移时顺手改变产品情绪动力学；即使 V3 Affect 已 authoritative，Ombre 也只能作为 reducer 的一个可识别 observation 输入，不能拥有独立状态写权。最终移除固定 30% 属于后续 Affect continuity 迁移，不阻塞 State Authority Complete。

### 3.2 Bond

- `intimacy`
- `passion`
- `commitment`
- `p_updated_at`
- `i_updated_at`

Bond 描述 **relationship baseline**：关系长期是什么样；它不是当前需求缺口。

- `intimacy`：长期亲密、熟悉、信任与连接基础；不等于 attachment。
- `passion`：相对慢变的关系激情/吸引基础；不等于 libido。它可以成为 libido 或某些 Intent 的显式上游调制信息，但不能冒充当前需求值。
- `commitment`：关系持续性、稳定性与承诺基础；不得被翻译为“当前想亲近”。

迁移期保留现有规则层与异步评分层分别作用于 P/I 的双通道叠加，以避免迁移阶段顺手调参。两类处理必须分开记账并引用同一个用户 Root Event，后续凭真实数据决定是否降权。

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

Drive 描述 **current deficit**：此刻缺什么、想得到什么；它不是关系本身，也不是情绪。

- `attachment`：当前接近、确认连接、获得回应的关系需求缺口；不等于 Bond intimacy。
- `libido`：当前身体/性相关需求缺口；不等于 Bond passion。

以下状态都完全合法：

```text
intimacy 高 + attachment 低
# 关系很亲密稳定，但此刻不缺确认与陪伴。

passion 高 + libido 低
# 长期吸引力很强，但此刻没有明显身体需求。
```

驱动读取时采用解析解计算自然增长；Longing、Passion、NA 等联动在同一权威快照内显式计算，不允许跨模块隐式读取。

### 3.4 未来语义归属（本轮只冻结类型，不新增生产字段）

- `jealousy`：Affect Trace；不得建立永久 jealousy Drive。
- `possessive_susceptibility`：Eventide 对占有/关系威胁类刺激的易感调制；不是人格“占有欲值”。
- `possessive_impulse`：事件发生后的短时派生倾向；不常驻为独立 Drive。

**这些定义冻结的是未来归属，不代表 ISV3-1B 需要新增 `jealousy_state`、`possessiveness_state`、Trace 或 Eventide 生产字段。**

### 3.5 Commitment 红线

Commitment 只描述关系持续性、稳定性与承诺基础。

禁止建立以下直接或隐式等价：

```text
commitment
→ attachment deficit
→ libido deficit
→ seek_closeness
→ message impulse
→ “当前想亲近她”
```

Commitment 可以作为 relationship security、Bond update context、event interpretation context 等较慢上游信息；不得单独推出即时 closeness / sexual / message 行为倾向。

### 3.6 版本与并发字段

- `state_version`
- `last_scored_message_id`
- `updated_at`

### 3.7 语义总纲

> **Bond 描述关系本身长期是什么样；Drive 描述此刻缺什么；Affect Trace 描述真实经历留下、尚未完全消退的情绪余波；Eventide susceptibility 描述当前身体对某类刺激有多容易被穿透；Longing 只描述由真实分离时长派生的思念程度。它们允许成为彼此的显式上游信息，但任何一个都不得冒充另一个的语义。**

## 4. Longing

ISV3-1B 正式候选采用固定 `tau = 18h`，只由 `user_idle_hours` 派生：

```text
L = 0.85 * (1 - (1 + t / 18)^(-0.8))
clamp <= 0.90
```

原则：

- 不单独落库。
- 不受 Wake message 重置。
- 不直接写入 prompt。
- 作为明确上游信息参与 attachment/Intent 等后续计算，但根只认 authoritative interaction clock。
- Event / Settlement 不得直接执行 `longing += / -=`。

保留迁移对照：

- `longing_emotion_legacy`：tau=8，仅作旧公式对照。
- attachment 调制 tau：保留实验代码，不在 ISV3-1B 启用。

## 5. Canonical Event Authority

沿用并演化现有 `internal_state_events` 作为**唯一 Canonical Event Ledger**；不要再造一张“总 Event 表”去包它。最终字段名与 Root/Event/evidence 的物理表结构留到施工时冻结，但事件权威现在冻结。

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
- 旧迁移观测 `legacy_wake_materialize:*` / `legacy_wake_va_calibration:*` 只用于迁移对拍，不升级为永久现实事件语义。

### 5.2 ISV3-1B 的 Event 最低完成线

本轮只需要做到“够迁移”：

- 一个现实发生具有唯一 root identity。
- rule / scorer processing 能归属于同一 root。
- outcome 具有 provenance。
- application 幂等且不重复记账。
- `internal_state_events` 是唯一逻辑 ledger authority。

本轮**不需要**因为 Event Authority 已冻结就实现 Trace events、Thought feeding、memory-recall retrigger、Body events 或 Eventide events。

### 5.3 时间演化边界

Event 是新的**离散因果事实**入口，不要求为纯时间流逝制造海量伪事件。

- Longing 的 `user_idle_hours` 派生、Drive 自然增长、未来 Trace 衰减、Body 自然恢复等“没有新事实发生，只是时间过去”的变化，使用 authoritative timestamp / snapshot 解析计算。
- 只有真正的离散产品事实（例如未来实际晨起 baseline 建立、明确 phase transition、真实自主 Action outcome）才按需要成为 Event。

### 5.4 Memory retrieval evidence 与禁止召回自激

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

### 5.5 Ombre Contract

Ombre V/A 的最终身份是 **Memory Affective Metadata / Memory Climate**，不是 Current Affect Authority。

1. Ombre 描述当前可参与记忆集合的 affective aggregate，以及单条/一组记忆的情感 metadata；不要把它严格描述成“最后 12 条聊天记忆的情绪”。eligible set / selection policy 可以随 Ombre 演化。
2. Ombre 的长期职责是 memory retrieval、semantic/topic relevance、salience、旧经历证据与记忆层 affective metadata。语义/主题相关性优先；高/低 V/A 本身不能强制召回。
3. 最终架构中，Ombre aggregate 不按固定权重直接混入 V3 Current Affect；现有 70/30 是 legacy continuity heuristic，不是永久产品常数。
4. **最终目标与当前迁移动作分离。** ISV3-1B 不因为本条已经冻结就直接把生产 70/30 改成 100/0；当前 30% 可作为 reducer 的 legacy observation 暂存，直到后续 Affect continuity 迁移单独验收。
5. 由 Ombre retrieval 引起的状态变化必须留在当前 Canonical Root Event causal chain 中：retrieval evidence → V3 reducer。Ombre 本身无 Affect/Bond/Drive/Trace/Thought/Body 写权。
6. Legacy Ombre observation 必须作为 observation/evidence 可识别地保留 provenance，不能静默烧进 Canonical Event fact。未来移除 30% 时不需要重写 Event 历史。
7. `/emotion_snapshot` 的 endpoint 名称未来可以调整以避免误解，但 endpoint rename 不是本轮施工内容。

一句话边界：**Ombre 负责“我记得什么，以及现在什么旧经历值得被想起”；V3 Affect 负责“眼前发生的事与被召回的旧经历，此刻让我感觉怎样”。**

## 6. 写接口与 Settlement

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
4. 运行规则/语义 assessment，并把结果作为该 Root Event 的 processing/evidence；应用当前迁移合同允许的 Affect/Bond delta。
5. 记录旧 attachment 固定减法结算结果与乘性候选结果。
6. 权威迁移初期沿用旧 attachment 结算，不启用新乘性参数。
7. fatigue 恢复沿用旧行为，候选方案仅 shadow。

`get_reunion_boost()` 当前为死代码：只记录 candidate，不实际增加 intimacy 或 attachment。

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
- provenance 缺失时 **fail closed**；不得 fallback 到 `Action → Drive / Intent` 推断。宁可留下可诊断的未结算/部分结算记录，也不能让 `infer_fired_drive_for_action()` 以新名字复活。
- 降低被满足的驱动。
- 增加 fatigue 成本。
- 写回新的基准值和时间。
- 同事务写 outcome Root Event / Settlement application 记录。

迁移期不顺手拍新参数：

- attachment 先沿用旧 `-0.55`。
- desire 中已有乘性 ratio 的维度按旧映射对拍。
- attachment 乘性回落作为 candidate 并排记录，观察后再裁决。

### 6.4 Provenance 反作弊验收

迁移期 legacy Decision 的 provenance 必须在 Action 执行前冻结。

至少增加一条 **metamorphic / 变形测试**：

```text
State Snapshot S
→ Legacy Decision D
→ freeze provenance P
→ test harness 人为替换最终 Action / outcome A1 / A2
→ P 必须保持不变
```

若同一状态快照与同一 Decision 下，`action=message` 时 provenance 变成 attachment、`action=explore` 时 provenance 又变成 curiosity，则判定为事后反推，测试失败。

## 7. State Authority 与旧器官退休

### 7.1 Domain Authority 条件

一个状态域只有同时满足以下条件，才能宣布 authoritative：

1. 唯一生产写入口：该域所有生产状态变化只能通过 V3 的 Event / Settlement 写路径发生。
2. 统一生产事实源：所有会影响状态或产品行为的生产读取最终读取 V3；旧 getter 若暂存，只能作为只读转发。
3. 单次来源只应用一次：同一 source event / outcome 不得在 V3 与旧模块重复结算；重试、regen、异步迟到也不得二次应用。
4. 旧生产副作用停止：相关旧 write / flush / calibrate / discharge / touch 等副作用必须同时停止。
5. 回退不得破坏单一事实源：允许回退消费者、Decision 或 View 实现，但不默认承诺冻结旧状态表可无同步重新成为权威。
6. Settlement 必须携带 Decision-time provenance；禁止根据最终 Action 事后反推 fired drive 或 intent。

Domain Authority 一旦切换，该域的 Mutation Authority 必须同步切换；不存在“大家都读 V3，但旧器官仍在后台偷偷改值”的半权威状态。

### 7.2 旧器官退休合同

迁移的是能力和数学，不迁移旧模块的生命。

- `emotion_engine`：Affect 与 I/P/C 状态所有权迁入 V3；DeepSeek/规则评分等可复用能力拆成无状态 evaluator。中期仅保留只读 compatibility facade，最终删除。
- `drive_engine`：八维 Drive 全部迁入 V3；`get_drive()` 中期仅转读 V3。`rest / discharge / discharge_by_action / decide / infer_fired_drive_for_action / get_wake_snippet` 等行为逻辑退休，最终删除模块。
- `desire.py`：七维 Drive 不迁移，直接退休；Longing 仅保留可复用纯公式并改由 authoritative interaction clock 派生。`get_longing()` 可短期转读 V3，其余 Drive / Intent / satisfy / Wake snippet / touch_hayana 等路径退休。

兼容壳纪律：不计算；不写库；不产生 Prompt；不允许新调用方。删除时以“已知生产调用已迁移、回滚窗口结束、证据足够”为条件；不得为了删除兼容壳专门建设新的监控基础设施。

### 7.3 迁移阶段：按依赖夺权，不按旧 Phase 编号

旧的 `Phase 1A / Phase 1B / Phase 2 / Phase 3 / Phase 4` 命名退休，避免把“Wake 接管”误当成状态权威完成条件。正式采用：

```text
Migration Stage A
Authoritative Interaction Clock

        ↓

Migration Stage B
Derived Longing Authority

        ↓

Migration Stage C
Affect + Bond Authority

        ↓

Migration Stage D
Eight Drives Authority
+ State Mutation Authority Complete

        ↓

👑 STATE AUTHORITY COMPLETE
```

Stage A：统一“多久没见”的外部事实源。

Stage B：Longing 只由统一 interaction clock 派生；所有仍存活的生产消费者必须转读同一条 Longing。

Stage C：Affect 与 Bond 是两个语义域，但作为一个 migration cohort 同批切换，因为共享 user rule / async scorer / 事务边界；切权后 `emotion_engine` 失去状态写权。现有 DeepSeek/Ombre 70/30 可以暂时作为 V3 reducer 的 legacy observation behavior 保留，不因此改变产品动力学。

Stage D：八维 Drive 由 V3 唯一定义；旧 Drive 的时间增长、flush、rest、discharge 等生产副作用停止；所有能够改变现有内部状态的 Action Settlement 都只能写 V3。

### 7.4 State Authority Complete 皇冠条件

只有同时满足以下事实，才允许称 V3 为“唯一内部心理状态权威”：

- Interaction Clock：唯一外部事实源。
- Longing：只由权威 clock 派生。
- Affect：只由 V3 保存/计算为生产事实。
- Bond：只由 V3 保存/计算为生产事实。
- Eight Drives：只由 V3 的同一权威快照与时间动力学定义。
- 所有生产 mutation：User Event、异步评分、时间推进与 Action Settlement 只能经 V3 Event / Settlement 路径改变权威状态。
- 所有旧模块：至多剩只读 compatibility facade，不能再影响下一轮状态、Wake、Chat 或反写 V3。

**Thought / Affect Trace / Eventide / Morning Baseline / Body 尚未上线，不妨碍 State Authority Complete。**它们属于后续扩展器官，不属于旧权威迁移的完成条件。

### 7.5 Settlement 与回退边界

在 State Authority 已切、Behavior Authority 尚未切的迁移期，允许 legacy Decision 暂时决定 action，但必须在决策时留下明确 provenance，再由 V3 Settlement 消费。

```text
Legacy Decision
→ Decision-time provenance
→ Action
→ V3 Settlement
```

禁止 Action 完成后根据 `message / explore / diary` 等最终动作，再猜刚才是哪根 Drive 或哪个 Intent 触发。

回退分两类：

- **便宜回退**：V3 State 继续作为事实源，回退 Wake decision、消费者或 View 实现；Settlement 仍写 V3。
- **状态权威反向切换**：V3 → 冻结 legacy table 不属于默认即时能力。只有未来存在可靠 replay / backfill / 显式 state migration 时才允许；不得为了可能的一次回退维持永久双写影子心脏。

## 8. Behavior Authority：独立于 State Authority

Behavior Authority 必须发生在 **State Authority Complete 之后**；它不是 ISV3-1B 的完成条件。

Wake Planner 的长期输入：

- **Wake State View**：经过授权的结构化 V3 状态与连续事实。
- **Capability Skill**：只描述“现在能做什么”、工具用途、前置条件、外部影响与现实限制；不描述“什么情绪应该做什么”。
- **Wake Planner**：结合前两者自主形成 Intent、选择 Action 候选并留下 provenance。
- **Action Gate**：确定性判断工具可用性、正在聊天/忙碌、勿扰与时间、fatigue/安全、冷却/重复、前置条件等。
- **Renderer**：只有选定 Action 需要语言时才负责怎么说。
- **Settlement**：行为完成后消费 provenance 并改变 V3。

禁止把状态写成固定生产映射：

```text
longing > x → message
curiosity > x → explore
arousal > x → social_post
```

同一个内在状态可以选择不同动作，也可以选择 `none`；同一个动作也可以来自不同 Intent。

### 8.1 旧 Drive→Action 表的身份降级

旧实现中 `attachment→message`、`curiosity→explore` 等映射只可作为 **legacy compatibility / migration observation mapping** 用于对拍，不再是最终 Planner 规范。

旧 Wake `drive_engine.get_wake_snippet()` / `desire.get_wake_snippet()` 的“内在驱动中文提示 → 模型自己猜行动”路径最终退休。

## 9. Views 与 Chat Exposure

### 9.1 Debug View

可以完整输出 Affect、Bond、Drives、Longing、timestamps、source health、warnings、diff、provenance 等调试信息。Debug 可读不等于模型可见。

### 9.2 Wake State View

Behavior Authority 阶段可以给 Planner 更丰富的结构化状态，例如 Affect、Bond、Derived Longing、Eight Drives，以及未来上线后真正相关的 Trace/Fixation、近期行动、工具/环境事实。

禁止先把这些结构化字段翻译成“你现在很……所以应该……”的中文心理报告。

### 9.3 Reviewed View / ModelContextGate

ISV3-1B 可以建设 Reviewed View / ModelContextGate 的**结构和过滤能力**，用于替代旧 `persona_state_semantic` 的导演式路径；这不等于必须在本轮开启新的 V3 Chat state block。

普通 Chat 默认 **零状态注入**。候选事实只有同时满足：

- Relevant：与当前消息直接相关。
- Non-inferable：等价事实当前不存在于 model-visible context，模型不能仅靠当前可见聊天事实可靠恢复。
- Salient：足够显著，确实会改变本轮体验。
- Non-directive：只是事实，不是行为/文风/人格指令。

才允许进入候选集；通过四关后仍受预算限制。

规则：

- Current Affect 原始 PA/NA/V/A/mood_word 默认不进入普通 Chat。
- Eight Drives 永远不直接进入普通 Chat，也不得翻译成“更愿意说话 / 更想亲近 / 好奇心活跃”等心理报告。
- Intent 默认不进入普通 Chat；极少数跨轮连续性场景优先暴露“未完成事实”，不是心理动机说明。
- Body 未来上线后最多一行、合计 2–3 个必要体验事实。
- Trace/Fixation 未来上线后跨轮心理连续事实合计最多 1–2 项。
- `persona_state_semantic` 作为 Chat Prompt 生产路径最终退休；若 UI/diagnostics 需要数值→标签，可另作 UI formatter，但不得重新成为 Prompt formatter。

### 9.4 Gate Judge Contract：标准先冻，裁判后拍

四关标准已经冻结，但“由谁、用什么算法裁决”**不是 ISV3-1B 的 State Authority 完工门**。

Track C 正式灰度 Chat Exposure 前必须单独冻结 Gate Judge Contract，至少明确：

- model-visible context 的边界是什么；哪些事实算已经可见。
- Relevant 的判断来源：provenance / topic relation / 其他受控语义匹配。
- Salient 的阈值、迟滞或其他稳定机制。
- Non-directive 的结构/schema 白名单与验收规则。
- 失败时是 fail closed 还是降级到零注入。

第一版优先用**信息可见性**解释 Non-inferable，而不是每轮预测“某个具体模型到底聪不聪明”。是否增加模型 reranker 属于 Track C 后续实验，不是当前宪法。

### 9.5 Legacy Chat Exposure Removal ≠ New V3 Chat Exposure Rollout

ISV3-1B 可以：

```text
旧导演状态块
→ 删除 / 静默 / 退役

Reviewed View / ModelContextGate
→ 具备生成安全候选的能力
```

同时保持：

```text
新的 V3 Chat Exposure = OFF
```

即：**把坏喇叭拆掉，不等于必须马上安装新广播站。**

State Authority Complete 之后，Behavior Authority 与 Chat Exposure 分叉独立灰度：

```text
             👑 STATE AUTHORITY COMPLETE
                 /                  \
                ▼                    ▼
       Behavior Authority        Chat Exposure
       Intent/Wake/Action      Reviewed View rollout
```

两者不是串行硬依赖；一个完整产品可以长期保持绝大多数普通 Chat 为零状态注入。

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

切权前人工检查：

```sql
PRAGMA journal_mode;
PRAGMA busy_timeout;
```

WAL 有利于并发，但是否开启必须在维护窗口显式决定；迁移代码不得偷偷修改 journal mode。

## 11. ISV3-1B Scope / Stop Line

### 11.1 本轮完成目标

ISV3-1B 的完成标准，是让现有 Affect、Bond、Derived Longing、Eight Drives 完成 **State + Mutation Authority** 统一；建立 Canonical Event、Settlement provenance、Reviewed View / ModelContextGate 的正确边界；并让旧 emotion / drive / desire 系统进入确定的只读兼容与退休路径。

本轮应完成：

```text
Authoritative Interaction Clock
Derived Longing Authority
Affect Authority
Bond Authority
Eight Drives Authority
State Mutation / Settlement Authority
Canonical Event Authority 的最低迁移语义
Reviewed View / ModelContextGate 基础
legacy compatibility / retirement path
```

其中 provenance 的 Decision-time 冻结、fail-closed 与反作弊测试属于本轮 State Mutation / Settlement Authority 的验收内容。

### 11.2 Deferred after ISV3-1B

以下属于后续**新能力或软语义设计门**，不得成为 ISV3-1B 的完工前置条件：

```text
Affect Trace
Thought Pool / Fixation
Eventide cycle / susceptibility dynamics
Pulse Body
Morning Baseline
memory retrieval → Trace continuity
Ombre fixed-30% removal
advanced coupling
new autonomous behavior semantics
Track A Topic Identity / Semantic Match Contract
Track C Gate Judge Contract / Chat Exposure rollout
```

这些能力可以在当前文档冻结语义归属、登记核心难题、预留接口、写 future TODO，但不得因此扩建本轮生产字段或动力学。

### 11.3 本轮明确不做

- 不改 Wake 调度概率。
- 不把 Behavior Authority 当作 State Authority Complete 的前置条件。
- 不强制开启新的 V3 Chat Exposure。
- 不启用 reunion boost。
- 不启用 attachment 调制 tau。
- 不采用新 attachment 乘性参数作为权威结算。
- 不新增 jealousy Drive / possessiveness Drive。
- 不因为未来语义已冻结就施工 Affect Trace / Thought / Eventide / Body / Morning Baseline。
- 不因为最终 Ombre Contract 已冻结就修改生产 DeepSeek/Ombre 70/30 权重。
- 不为替代 Ombre 30% 提前扩建 Affect Trace / memory retrieval continuity。
- 不在本轮重命名 `/emotion_snapshot` endpoint。
- 不为了回滚维持永久 legacy state 双写。
- 不为了四关闸门提前引入“每轮额外模型审查”。
- 不为了“同主题”提前拍死 embedding、阈值、topic schema 或数据库结构。

## 12. 后续工作分区

### A. Behavior Authority

在 State Authority Complete 后单独灰度 Wake Planner + Capability Skill + Action Gate + Renderer + Settlement 的自主行动闭环。

### B. Chat Exposure

在 Reviewed View / ModelContextGate 基础上独立 A/B；默认零状态注入，只有必要事实进入普通 Chat。

正式灰度前必须完成 **Gate Judge Contract**：明确 Relevant / Non-inferable / Salient / Non-directive 的裁判机制、model-visible context 边界与失败策略。

### C. Future Internal-State Expansion

按独立窄阶段逐步施工：Affect Trace → Thought/Fixation → Eventide → Body → Morning Baseline → memory retrieval continuity 等。所有新机制先 Shadow，再验收，再进入下游。

Track A 的核心研究难题之一是 **Topic Identity / Semantic Match Contract**。“同主题”不是实现细节；它直接决定 Trace retrigger、flit→fixation feeding 与 memory retrieval continuity 是否会过松自激或过紧失效。开工前至少要区分 exact continuation、related-but-not-same、broad-category-only、joke/hypothetical/fiction，并分别控制 false positive / false negative；当前不冻结具体算法。

### 12.1 Body 文本冲突的默认处理

模型生成文本不是 Body sensor。未来 Body 上线后，若模型自由生成文本与权威 Body fact 不一致：

- 默认视为生成层偏差。
- 不据此反写 Body。
- 不启动“文本一致性校正 → 状态写回”的反向因果闭环。
- 可以作为 diagnostics / A/B 样本记录，但冲突文本本身不产生新的状态因果。

### 12.2 Deferred 预设计的未来重审规则

本文对 Affect Trace、Thought、Eventide、Body、Morning Baseline 等 deferred 器官给出了高精度预设计，但它们不是永久字段/算法规范。

未来真正开工时，应基于当时仓库、实验结果和已冻结 invariants 重新评审。若新判断与本文件的旧预设计冲突：

- 必须先显式更新本合同或对应 ADR，再施工。
- 禁止代码以“实现方便”为由静默漂移，反向覆盖文档。

以下仍属于上位 invariant，不因 deferred 细节重审而自动失效：

- 单一权威 / 单一事实源。
- Canonical Event provenance。
- 不重复记账。
- 不允许文本或 Action 事后反推制造反向因果。
- 后台丰富，模型贫穷。
- 状态只造成体验，不写导演指令。
- Bond / Drive / Trace / Eventide / Longing 语义不得串线。

## 13. 验收补充：防过注，也防失忆

现有 Chat Exposure 验收除了防“注太多”，必须增加对称失败模式。

### Q. 必要连续性不得漏注

给定一条：

- 仍存活；
- 达到显著阈值；
- 与当前输入强相关或被当前输入 retrigger；
- 其存活状态/后效**无法从当前 model-visible context 恢复**；

的跨轮 Trace，Reviewed View 不得为空；至少应输出一条最小连续性事实。

若等价事实已经存在于当前模型可见上下文中，则继续遵守 Non-inferable，不要求重复注入。

### R. Provenance 不得由最终 Action 反推

对同一状态快照与同一 legacy Decision，在测试中替换最终 Action / outcome 后，Decision-time provenance 必须保持不变；若 provenance 随 Action 类型变化，测试失败。provenance 缺失时必须 fail closed，禁止 Action→Drive / Intent fallback。

---

最终边界：**状态统一、让它自主行动、让模型看状态、给它长新器官，是四件不同的事情。**