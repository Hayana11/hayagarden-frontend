# Internal State v3 规格说明

> 状态：架构冻结合同（ISV3-1B）。
>
> 上位合同：**《哈娅花园｜Internal State V3 身心—念头—行动融合方案（Eventide × Pulse × Desire）｜2026-08-03_权威迁移合同修订版》**。
>
> 本文件只把上位合同落实成 ISV3-1B 的仓库规格：统一现有 Affect、Bond、Derived Longing、Eight Drives 与现有状态 mutation 的权威；定义 Canonical Event / Settlement / Reviewed View 的边界；明确旧器官退休路径。若本文件与上位合同冲突，以上位合同为准，并应先显式更新本文件或 ADR，再改生产实现。

## 0. 本规格的停止线

ISV3-1B 的任务是：

> **统一旧器官的权威，不提前长新器官。**

本轮目标是一套权威状态、一套写入口、一套事件账本、一次行为结算。

本轮不是：

- Behavior Authority 上线；
- 新 V3 Chat Exposure 上线；
- Affect Trace / Thought / Eventide / Body / Morning Baseline 上线；
- Ombre 70/30 动力学重做；
- 新 autonomous behavior 语义设计；
- 新的 Drive→Intent→Action 固定映射设计。

状态统一、让它自主行动、让模型看状态、给它长新器官，是四件不同的事情。

---

## 1. 一级不变量

### 1.1 单一权威

任一状态域只有同时满足以下条件，才能称为 authoritative：

1. **唯一生产写入口**：该域所有生产 mutation 只能通过 V3 的 Event / Settlement 路径发生。
2. **统一生产事实源**：所有会影响状态或产品行为的生产读取最终读取 V3；旧 getter 若暂存，只能只读转发。
3. **单次来源只应用一次**：同一 source event / outcome 不得在 V3 与旧模块重复结算；重试、regen、异步迟到不得二次应用。
4. **旧生产副作用停止**：相关旧 write / flush / calibrate / discharge / touch 等副作用必须停止。
5. **回退不破坏单一事实源**：允许回退消费者、Decision 或 View；不默认承诺冻结 legacy table 可无同步重新成为权威。
6. **Settlement 消费 Decision-time provenance**：不得根据最终 Action 事后反推 fired drive 或 intent。

Domain Authority 一旦切换，Mutation Authority 必须同步切换。不存在“大家都读 V3，但旧器官仍在后台偷偷改值”的半权威状态。

### 1.2 Parse once, reduce many

一个现实发生只建立一个 Canonical Root Event。

```text
Root Event = 世界发生了什么
Evidence   = 我们拿到什么证据理解它
Reducer    = 它对某个状态域造成什么
Planner    = 我现在想做什么
```

Affect、Bond、Trace、Thought、Body、Drives 等只能消费统一 Event / evidence，不得各自重新解析同一条用户消息、Wake outcome 或 world input，制造第二套事实。

### 1.3 运行时因果与迁移顺序分离

Stage A→B→C→D 是**夺权顺序**，不是运行时数学因果链。

运行时应保持：

```text
authoritative interaction clock → Derived Longing
Event → Affect / Bond
Affect + Bond + Longing + 其他已授权上游
    → Eight Drives / Intent 计算
```

不得通过跨模块隐式 import 重新制造第二条偷偷的因果链。

### 1.4 后台丰富，模型贫穷

后台可以拥有丰富状态；普通 Chat 默认不需要听见这些状态。

- 状态负责造成体验，不负责解释体验。
- 不允许把状态翻译成“你现在很……所以你应该……”的导演提示。
- Drive 不直接进入普通 Chat。
- Current Affect 默认不进入普通 Chat。
- 零状态注入是正常结果，不代表 V3 没工作。

---

## 2. ISV3-1B 权威迁移阶段

旧 `Phase 1A / Phase 1B / Phase 2 / Phase 3 / Phase 4` 命名正式退休。它们容易把 Wake/行为接管误认为 State Authority 的完成条件。

正式采用以下有依赖顺序的 Domain Authority 迁移：

```text
Stage A
Authoritative Interaction Clock
        ↓
Stage B
Derived Longing Authority
        ↓
Stage C
Affect + Bond Authority
        ↓
Stage D
Eight Drives Authority
+ State Mutation Authority Complete
        ↓
👑 STATE AUTHORITY COMPLETE
```

### Stage A｜Authoritative Interaction Clock

保留 `chat.interaction_state.read_interaction_clock()` 作为“多久没见”的唯一外部事实源：

- `user_idle_hours`：距最后一条真实用户消息的时长；用于 Longing。
- `effective_idle_hours`：真实用户消息与最近 Wake message 中较近者；可继续服务现有 Wake 限流/概率逻辑，但不得反向污染 `user_idle_hours`。

规则：

- Wake message 不重置 `user_idle_hours`。
- Longing 不再读取 `emotion_state.last_interaction` 或 `desire_state.last_hayana_msg_time`。
- 时钟不可读时 fail closed；不得伪造 999h 等异常时长。

### Stage B｜Derived Longing Authority

Longing 是**纯派生量**，不是另一张需要 Event / Settlement 修改的需求条。

```text
authoritative interaction clock
→ user_idle_hours
→ Derived Longing(tau = 18h)
```

当前 ISV3-1B 候选公式：

```text
L = 0.85 * (1 - (1 + t / 18)^(-0.8))
clamp <= 0.90
```

规则：

- 不单独落库为第二份权威状态。
- 不受 Wake message 重置。
- Event / Settlement 不得直接执行 `longing +=` / `longing -=`。
- 所有仍存活的生产消费者必须最终转读这一条 authoritative Longing。
- `longing_emotion_legacy` 等旧公式只可用于迁移对照，不再拥有生产权威。

### Stage C｜Affect + Bond Authority

Affect 与 Bond 是两个语义域，但作为一个 migration cohort 同批切换，因为共享 user rule、async scorer 与事务边界。

切权后：

- Affect 只由 V3 保存/计算为生产事实。
- Bond 只由 V3 保存/计算为生产事实。
- `emotion_engine` 失去全部状态写权。
- DeepSeek / rule scorer / legacy Ombre 可作为无状态 evaluator / observation 暂存，但不得拥有状态所有权。

### Stage D｜Eight Drives Authority + State Mutation Authority Complete

八维 Drive 由 V3 的同一权威快照与时间动力学定义：

- `attachment`
- `curiosity`
- `reflection`
- `social`
- `duty`
- `libido`
- `stress`
- `fatigue`

切权后：

- 旧 Drive 的时间增长、flush、rest、discharge 等生产副作用停止。
- 所有现有内部状态 mutation，只能通过 V3 Event / Settlement 路径发生。
- 旧 getter 至多只读转发 V3。

### 2.1 State Authority Complete 皇冠条件

只有同时满足以下事实，才允许正式称 V3 为“唯一内部心理状态权威”：

- Interaction Clock：唯一外部事实源。
- Longing：只由 authoritative clock 派生。
- Affect：只由 V3 保存/计算为生产事实。
- Bond：只由 V3 保存/计算为生产事实。
- Eight Drives：只由 V3 的同一权威快照与时间动力学定义。
- User Event、异步评分、时间推进、Action Settlement 等所有现有 mutation 只能经 V3 改变权威状态。
- 所有旧模块至多剩只读 compatibility facade，且不能再影响下一轮状态、Wake、Chat 或反写 V3。

**Thought / Affect Trace / Eventide / Morning Baseline / Body 尚未上线，不妨碍 State Authority Complete。**

---

## 3. 语义类型系统

一级总原则：

> **Bond = relationship baseline；Drive = current deficit。**

### 3.1 Affect

Current Affect 继续表达此刻情绪，可使用 PA / NA / valence / arousal 等现有字段。

- `mood_word` 等可读标签仅供兼容壳、调试面板或历史展示。
- 不进入普通 Chat。
- 不作为句长、语气、文风或“话少一点”等形式控制器。

未来 `jealousy` 的归属是 **Affect Trace**，不是永久 Drive；本轮只冻结语义，不新增 Trace 生产字段。

### 3.2 Ombre Contract

Ombre V/A 的最终身份是 **Memory Affective Metadata / Memory Climate**，不是第二份 Current Affect Authority。

长期职责：

- memory retrieval；
- semantic / topic relevance；
- salience；
- 旧经历证据；
- 记忆层 affective metadata。

迁移边界：

- 当前 DeepSeek 70% + Ombre 30% 可以暂时作为 **legacy continuity heuristic / evaluator observation** 存活，以避免 State Authority 迁移时顺手改变情绪动力学。
- 即使 Stage C 后 V3 Affect 已 authoritative，Ombre 也只能是 reducer 可识别的 observation/evidence，不能拥有 Affect/Bond/Drive/Trace/Thought/Body 写权。
- Legacy Ombre observation 必须保留 provenance，不能静默烧进 Canonical Event fact。
- 最终移除固定 30% 属于后续 Affect continuity 迁移，不阻塞 State Authority Complete。
- ISV3-1B 不重命名 `/emotion_snapshot` endpoint。

### 3.3 Bond

Bond 描述关系长期是什么样：

- `intimacy`：长期亲密、熟悉、信任与连接基础；不等于 attachment。
- `passion`：长期关系激情/吸引基础；不等于 libido。
- `commitment`：关系持续性、稳定性与承诺基础；不等于“当前想亲近”。

Commitment 红线：禁止建立以下直接或隐式等价：

```text
commitment
→ attachment deficit
→ libido deficit
→ seek_closeness
→ message impulse
→ “当前想亲近她”
```

Commitment 可以作为 relationship security、Bond update context、event interpretation context 等慢速上游信息，但不能单独推出即时 closeness / sexual / message 行为倾向。

### 3.4 Drives

Drive 描述当前缺口，而不是关系本身，也不是情绪。

- `attachment`：当前接近、确认连接、获得回应的需求缺口；不等于 intimacy。
- `libido`：当前身体/性相关需求缺口；不等于 passion。

以下状态完全合法：

```text
intimacy 高 + attachment 低
passion 高 + libido 低
```

### 3.5 未来语义归属，只冻结类型

- `jealousy`：Affect Trace。
- `possessive_susceptibility`：Eventide 对占有/关系威胁类刺激的易感调制，不是人格“占有欲值”。
- `possessive_impulse`：事件发生后的短时派生倾向，不常驻为独立 Drive。

这些定义不授权 ISV3-1B 新增 `jealousy_state`、`possessiveness_state`、Trace、Eventide 或 Body 生产字段。

---

## 4. Canonical Event Authority

沿用并演化现有 `internal_state_events` 作为**唯一 Canonical Event Ledger**。不要再造第二张“总 Event 表”包住它。

最终字段名、Root/evidence/application 的物理表结构在施工时冻结；本规格冻结的是语义合同。

### 4.1 Root Event、Evidence、Reducer、Planner

```text
user_message:{message_id}      # Root Event
    ├─ semantic/rule assessment
    ├─ async scorer observation
    ├─ legacy Ombre observation（迁移期可选）
    └─ memory retrieval evidence（未来）
             ↓
        domain reducers
        Affect / Bond / Trace / Thought / Body / Drives
             ↓
        Planner / Decision
```

规则：

- Event 只描述“发生了什么”。
- `relationship_threat` / factual summary 可以属于 Event fact。
- `jealousy=0.7`、`attachment += 0.2`、`intent=seek_closeness` 属于 reducer / planner 结果，不能反写成 Event fact。
- 真实用户消息是 Root Event。
- 真实 Action 的成功/失败、真实 world observation 等，在确实形成新的独立经历时可以成为新的 Root Event。
- `user_rule:{message_id}` / `user_scored:{message_id}` 在迁移期可以继续作为同一 user Root Event 的 processing/application 记录，但不代表发生了两次现实。
- `wake_outcome:{wake_run_id}` 若对应真实执行结果，可成为 outcome Root Event；其 Settlement 必须携带 Decision-time provenance。

### 4.2 ISV3-1B 的 Event 最低完成线

本轮只做到“够迁移”：

- 一个现实发生有唯一 root identity。
- rule / scorer processing 归属于同一 root。
- outcome 有 provenance。
- application 幂等，不重复记账。
- `internal_state_events` 是唯一逻辑 ledger authority。

本轮无需实现：Trace events、Thought feeding、memory-recall retrigger、Body events、Eventide events。

### 4.3 纯时间演化不是伪 Event

Longing 时长派生、Drive 自然增长、未来 Trace 衰减、Body 恢复等“没有新事实，只是时间过去”的变化，通过 authoritative timestamp / snapshot 解析计算。

只有真正的离散产品事实才按需要记 Event。

### 4.4 Memory retrieval 是 Evidence，不是第二个现实

未来 memory retrieval 默认属于当前 Root Event causal chain：

```text
Canonical Root Event
→ retrieval evidence
→ reducer
```

禁止召回自激：

- 同一 causal chain 内 recall result 不得递归成为新的 recall trigger。
- 同一个 root event + 同一 memory evidence 不得对同一 Trace / state effect 重复施加同一种 retrigger。

---

## 5. Mutation / Settlement Contract

### 5.1 User Event 与异步 scorer

同一真实用户消息只能产生一个 user Root Event。

规则 assessment、DeepSeek scorer、legacy Ombre observation 等都属于该 Root Event 的 processing/evidence；它们可以分别到达，但必须：

- 有幂等 key；
- 防止异步迟到覆盖新状态；
- 保留 observation provenance；
- 不能分别制造第二次 Affect/Bond 现实事件。

如仍使用 `last_scored_message_id` / state version 等现有并发字段，可继续作为 ISV3-1B 的迁移实现细节；不得让旧 scorer 获得独立状态写权。

### 5.2 Decision-time provenance 是硬合同

在 State Authority 已切、Behavior Authority 尚未切的迁移期，legacy Decision 可以暂时继续决定 Action，但**原因必须在 Decision 时冻结并随行为传递**：

```text
State Snapshot
→ Legacy Decision
→ freeze Decision-time provenance
→ Action
→ V3 Settlement
```

Provenance 可以是 primary drive，也可以包含多个 contributor；字段结构施工时冻结。

这里冻结的是语义：

- Settlement 不得从最终 Action 反推原因。
- provenance 缺失时 fail closed。
- 禁止 fallback 到 `Action → Drive / Intent` 猜测。
- `infer_fired_drive_for_action()` 不得以新名字复活。

### 5.3 Provenance 反作弊验收

至少保留一条变形测试：

```text
State Snapshot S
→ Decision D
→ freeze provenance P
→ 测试中替换最终 Action / outcome A1 / A2
→ P 必须保持不变
```

如果 provenance 随 `message / explore / diary` 等最终 Action 类型改变，测试失败。

### 5.4 Settlement

同一 Action outcome 只能结算一次。

Settlement 负责：

- 消费 Decision-time provenance；
- 对已授权状态做一次且仅一次 mutation；
- 记录 outcome / application；
- 与权威状态写入保持可恢复的一致性。

ISV3-1B 的目标是**迁移现有结算权威**，不是顺手重调行为参数。旧数学/比率可作为迁移对拍或纯函数能力复用；它们不能继续以旧模块状态副作用的形式存活。

---

## 6. 旧器官退休合同

迁移的是能力和数学，不迁移旧模块的生命。

### `emotion_engine`

- Affect 与 I/P/C 状态所有权迁入 V3。
- DeepSeek / 规则评分等可复用能力拆成无状态 evaluator。
- 中期至多保留只读 compatibility facade。
- 最终删除旧状态写路径。

### `drive_engine`

- 八维 Drive 全部迁入 V3。
- `get_drive()` 中期仅转读 V3。
- `rest / discharge / discharge_by_action / decide / infer_fired_drive_for_action / get_wake_snippet` 等旧行为/写状态路径退休。

### `desire.py`

- 旧七维 Drive 不迁移，直接退休。
- Longing 仅保留 `tau=18h` 等可复用纯公式，由 authoritative interaction clock 派生。
- `get_longing()` 可短期转读 V3。
- 其余 Drive / Intent / satisfy / Wake snippet / touch_hayana 等旧路径退休。

兼容壳纪律：

- 不计算新状态；
- 不写库；
- 不产生 Prompt；
- 不允许新调用方。

不得为了删除兼容壳专门建设新的监控/测试基础设施。

---

## 7. 决策与行为边界：删除旧固定映射

Behavior Authority **不属于 ISV3-1B 的完成条件**。

旧实现中的：

```text
attachment → message
curiosity → explore
longing > x → message
arousal > x → social_post
```

只允许作为 **legacy compatibility / migration observation** 对拍材料；它们不是最终 Planner 规范，也不能被 Settlement 用来事后猜原因。

长期职责边界：

- **Wake State View**：给 Planner 结构化状态事实。
- **Capability Skill**：只描述“当前能做什么”、用途、前置条件、外部影响与现实限制。
- **Wake Planner**：自主形成 Intent / Action 候选，并在 Decision 时留下 provenance。
- **Action Gate**：确定性判断现实是否允许执行。
- **Persona / Renderer**：需要语言时决定怎么说。
- **V3 Settlement**：行为完成后消费 provenance 并结算状态。

同一个内在状态可以选择不同动作，也可以选择 `none`；同一个动作也可以来自不同 Intent。

**禁止在 Skill、Planner 或生产规则中重建“某状态必然对应某 Action”的固定映射。**

---

## 8. Views 与 Chat Exposure

### 8.1 Debug View

可以完整输出 Affect、Bond、Drives、Longing、timestamps、source health、warnings、diff、provenance 等调试信息。

Debug 可读不等于模型可见。

### 8.2 Wake State View

Behavior Authority 阶段，Planner 可以读取比普通 Chat 丰富得多的结构化 V3 状态；不得先把它们翻译成“你现在很……所以应该……”的中文心理报告。

### 8.3 Reviewed View / ModelContextGate

ISV3-1B **可以完成 Reviewed View / ModelContextGate 的结构与过滤基础**，用于替代旧 `persona_state_semantic` 的导演式路径。

这不等于本轮开启新的 V3 Chat Exposure。

普通 Chat 默认零状态注入。候选事实必须同时通过四关：

1. **Relevant**：与当前消息/行动直接相关。
2. **Non-inferable**：等价事实当前不存在于 model-visible context。
3. **Salient**：足够显著，会真实改变本轮体验。
4. **Non-directive**：只是事实，不含行为、文风、人格指令。

规则：

- Current Affect 原始 PA/NA/V/A/mood_word 默认不进入普通 Chat。
- Eight Drives 永远不直接进入普通 Chat，也不得翻译成“更愿意说话 / 更想亲近 / 好奇心活跃”等心理报告。
- Intent 默认不进入普通 Chat；极少数连续性场景优先暴露“未完成事实”，而不是心理动机解释。
- Body 未来上线后，单轮最多一行、2–3 个必要体验事实。
- Trace/Fixation 未来上线后，跨轮必要心理连续事实合计最多 1–2 项。
- `persona_state_semantic` 作为 Chat Prompt 生产路径最终退休；如 UI/diagnostics 仍需要数值→标签，可另作 UI formatter，但不得重新成为 Prompt formatter。

### 8.4 Gate Judge Contract 不是 1B 完工门

四关语义已冻结，但“由谁、用什么算法裁判”不在 ISV3-1B 内拍死。

**Track C 开工前**必须单独冻结 Gate Judge Contract，至少定义：

- model-visible context 边界；
- Relevant 的判断来源；
- Salient 的阈值/迟滞；
- Non-directive 的 schema / 白名单；
- 失败策略。

第一版 Non-inferable 优先解释为**信息可见性**，而不是每轮预测某个模型的推理能力。

### 8.5 Legacy Chat Exposure Removal ≠ New V3 Chat Exposure Rollout

允许：

```text
旧 persona_state_semantic 导演状态块
→ 删除 / 静默 / 退役

Reviewed View / ModelContextGate
→ 建成安全候选能力
```

同时保持：

```text
New V3 Chat Exposure = OFF
```

把坏喇叭拆掉，不等于必须马上安装新广播站。

---

## 9. 并发与事务

app、gateway、wake 等多进程共享状态存储时，每次权威状态更新必须满足：

```sql
BEGIN IMMEDIATE;
-- 建立/确认唯一 event/application key
-- 校验 expected state_version
-- UPDATE authoritative state
-- state_version + 1
COMMIT;
```

要求：

- 状态行与 Event/application 记录同事务提交。
- 设置合理 `busy_timeout`。
- 版本冲突时重读、重算并有限次数重试。
- 不允许静默覆盖。
- 同一个 root event + evidence + reducer effect 不得重复应用。
- WAL 是否开启必须在维护窗口显式决定；迁移代码不得偷偷修改 journal mode。

---

## 10. ISV3-1B Scope / Stop Line

### 10.1 本轮应完成

```text
Authoritative Interaction Clock
Derived Longing Authority
Affect Authority
Bond Authority
Eight Drives Authority
State Mutation / Settlement Authority
Canonical Event Authority 的最低迁移语义
Decision-time provenance + fail-closed
Reviewed View / ModelContextGate 基础
legacy compatibility / retirement path
```

完成这些，且满足皇冠条件，才允许宣布：

```text
STATE AUTHORITY COMPLETE
```

### 10.2 Deferred after ISV3-1B

以下属于后续能力或设计门，不得成为 ISV3-1B 完工前置条件：

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
Topic Identity / Semantic Match Contract
Gate Judge Contract / Chat Exposure rollout
```

这些内容可以在设计文档中冻结语义归属、登记核心难题、预留接口，但不得因此顺手新增本轮生产字段或动力学。

### 10.3 本轮明确不做

- 不改 Wake 调度概率。
- 不把 Behavior Authority 当作 State Authority Complete 前置条件。
- 不强制开启新的 V3 Chat Exposure。
- 不启用 reunion boost。
- 不启用 attachment 调制 tau。
- 不把新的 attachment 乘性参数切成权威结算。
- 不新增 jealousy Drive / possessiveness Drive。
- 不施工 Affect Trace / Thought / Eventide / Body / Morning Baseline。
- 不因为 Ombre 最终身份已裁决就把生产 70/30 改成 100/0。
- 不为替代 Ombre 30% 提前扩建 Trace / retrieval continuity。
- 不在本轮重命名 `/emotion_snapshot` endpoint。
- 不为了回滚维持永久 legacy state 双写。
- 不为了四关闸门提前引入“每轮额外模型审查”。
- 不为了“同主题”提前拍死 embedding、阈值、topic schema 或数据库结构。
- 不建立新的固定 Drive→Intent→Action 生产映射。

---

## 11. ISV3-1B 之后：三条独立灰度线

**Track 名称与字母在本合同中冻结如下。不得再用旧的 A/B/C 对应关系。**

### Track A｜Future Internal-State Expansion

- **A1 Affect Trace**：基于 Canonical Event 建立带来源、可衰减、可 retrigger 的余波；先 Shadow，不进 Prompt。
- **A2 Thought Pool / Fixation**：真实事件生成 flit；同主题重复触发后才允许升级 fixation。
- **A3 Eventide**：慢周期、sensitivity、control、possessive_susceptibility 等幕后调制；不直接输出 Action / 台词。
- **A4 Pulse Body + Morning Baseline**：最小身体链与每日身体起点；模型文本不能反向写身体状态。
- **A5 Trace / Thought 弱联动 Drive**：只允许有上限、有阻尼、可去重的小幅影响。
- **A6 Memory retrieval continuity / Ombre 30% 迁移**：在连续性机制有足够证据后再单独评估。

Track A 开工门：**Topic Identity / Semantic Match Contract**。必须先区分 exact continuation、related-but-not-same、broad-category-only、joke/hypothetical/fiction 等边界；当前不冻结 embedding、阈值、字段或数据库结构。

### Track B｜Behavior Authority

- **B1 Planner Shadow**：Wake Planner 读取结构化 V3 State View + Capability Skill，自主形成 Intent / Action 候选，与 legacy Decision 并行对比但不接管。
- **B2 Action Gate + 局部接管**：先接安全、易验证的行动；允许同状态不同动作或 `none`。
- **B3 Renderer + Settlement**：需要语言时才交 Persona/Renderer；完成后 V3 Settlement 消费 Decision-time provenance。

### Track C｜Chat Exposure

- **C1 Reviewed View / ModelContextGate A/B**：默认零状态注入，只允许四关通过的必要事实进入候选。
- **C2 后台丰富，模型贫穷**：Drive 不直达 Chat；Current Affect 默认不进；Body 与 Trace/Fixation 受严格预算。
- **C3 rollout 可长期为 0**：新的 V3 Chat Exposure 不依赖 Behavior Authority 完成，也不是 State Authority Complete 的证明。

Track C 开工门：**Gate Judge Contract**。

三条 Track 在 State Authority Complete 之后独立灰度；彼此不是串行硬依赖。

---

## 12. 验收红线

### A. 人格不被状态夺舍

状态层不得包含文风、人格或固定表演命令。

### B. 无来源不生情

未来 Trace / Thought / Body 等新增能力必须能追到 source event。

### C. 不重复记账

重试、regen、异步迟到、重复 message_id、重复 outcome 不得二次 mutation / Settlement。

### D. 时间单一

所有“多久没见”统一读取 authoritative interaction clock；时钟异常 fail closed。

### E. Shadow 不影响生产

接管 gate 关闭时，Shadow 只观察，不改变 Chat/Wake 输出。

### F. Provenance 不得由最终 Action 反推

Decision-time provenance 必须在 Action 前冻结；缺失时 fail closed。

### G. 后台丰富、模型贫穷

后台字段增加时，普通 Chat 的状态注入长度不得随字段数量线性增长。

### H. 零相关即零注入

与当前输入无关时，即使后台存在高 Drive、未来 Eventide phase 或 Morning Baseline，也允许状态块为空。

### I. 不允许导演语义

模型可见 state 不得出现“应该、要更、表现得、语气、话少、霸道、危险、主动一些”等导演式措辞。

### J. Drive 不直达 Chat

Eight Drives 的变化不得自动产生自然语言 Chat 心理报告。

### K. Eventide 只做幕后调制

未来 phase / susceptibility / control 不得直接成为 Persona 指令或 Action 规则。

### L. 必要连续性不得漏注

Track C 上线后，若一条跨轮连续事实仍存活、足够显著、与当前输入强相关，且无法从 model-visible context 恢复，Reviewed View 不得漏掉最小必要事实；若等价事实已可见，则继续遵守 Non-inferable，不重复注入。

---

## 13. Deferred 预设计的未来重审规则

上位设计对 Affect Trace、Thought、Eventide、Body、Morning Baseline 等 deferred 器官给出的细节属于高精度预设计，不是永久字段/算法规范。

未来真正开工时，若新仓库事实、实验结果或已冻结 invariant 要求调整：

1. 先显式更新本合同或对应 ADR；
2. 再施工；
3. 禁止代码以“实现方便”为由静默漂移反向定义规范。

以下 invariant 不因 deferred 细节重审而自动失效：

- 单一权威 / 单一事实源；
- Canonical Event provenance；
- 不重复记账；
- 不允许文本或 Action 事后反推制造反向因果；
- 后台丰富，模型贫穷；
- 状态只造成体验，不写导演指令；
- Bond / Drive / Trace / Eventide / Longing 语义不得串线。

---

最终边界：

> **ISV3-1B 只负责把现有内部状态收进同一颗权威心脏。**
>
> 新器官、主动行为、Chat 暴露分别在 Track A / B / C 独立灰度；任何后续实现都不能把旧阶段名、旧 Drive→Action 决策映射或旧扩大范围重新写回生产规范。
