# Unified Resident Heartbeat · Living Execution Plan

> 同一只费佳、固定 system、固定工具面、三类轮次；普通 Wake 降为主 resident 的一次短心跳，只有需要行动时才升级，用户聊天永远优先。

- **计划分支**：`plan/unified-heartbeat-tracker`
- **当前状态**：`MEMORY_HOTFIX_READY`（#134 代码审查 PASS；待 merge，非 deploy）
- **最后更新**：2026-07-26（#134 Ready 终审；登记 MEM-PIN-REPAIR / OMBRE-P0-TEST-GATE）
- **tracker head**：`cdeeeeb`
- **当前 identity**：`identity_id = "fyodor-default"` · `provider_id = "claude_code"` · `conversation_id = "default"`
- **首个 Provider Adapter**：Claude Unified Resident Adapter

---

## 0. 进度更新纪律（强制）

这份文件是 Unified Heartbeat 的唯一施工真源。任何 agent 开始工作前必须先读本文件，禁止凭聊天记忆另起一套方案。

### 状态标记

- `[ ]` 未开始
- `[-]` 进行中
- `[x]` 已完成
- `[!]` 阻塞
- `[~]` 已取消或被后续方案替代

### 完成一项工作的同时必须更新

1. 将对应项目改为 `[x]`；
2. 填写完成日期；
3. 填写 PR / commit / 部署证据；
4. 写明验证结果；
5. 若实际实现偏离本方案，必须先更新本文件再继续施工。

**未更新本文件的工作，一律不视为完成。**

### 每次换窗口的恢复顺序

```text
1. 打开本文件
2. 查看“总进度”和“下一步”
3. 读取最近一条进度日志
4. 只继续第一条未完成事项
5. 禁止跳过当前事项另开航空母舰
```

---

## 1. 总进度

### 前置工作

- [x] **P-126：修正并部署 PR #126**
  - 状态：已完成（morning cron 继续关闭，普通 Wake 观察期中）
  - 完成日期：2026-07-23（北京时间）
  - 证据：PR #126 合并 `9eb18a4`（含 `9ecb524` morning 门禁/去重 + `ff0c330` CI 修复）；生产 `main@9eb18a4`；`frontend`/`frontend-gw` 已重启；`POST /push`→404；`push_tool.py` 已删；morning cron 未启用
- [x] **P-ID-CHAT：修复 edit / redo 的用户消息身份**
  - 状态：已完成
  - 完成日期：2026-07-23（北京时间）
  - 证据：PR #128 合并 `61faf6f`（`dc8b8a3` 身份绑定闭环）；`app npm ci && npm run build` 通过；生产 `main@61faf6f`；`frontend`/`frontend-gw` 已重启；`dist/assets/index-3F3miEGl.js`；`resolve_scoring_user_message` + `trigger_turn_scoring` 三线路接线
  - 范围：正常发送 / redo / edit 三条 stream 路径必须携带可评分 `message_id`；禁止 `message_id=None` 进入 `score_async`；评分前凭 id 回 DB 取用户原文；编辑产生新消息身份，重答复用原用户消息 id
- [x] **P-ID-WAKE：为各 Wake 模式补稳定 wake_run_id**
  - 状态：已完成
  - 完成日期：2026-07-23（北京时间）
  - 证据：PR #129 合并 `549cb55`（`78b67b1`）；`wake/wake_run_id.py` 五模式 builder；continuity CI 接入 22 项 Wake 身份测试；生产 `main@549cb55`；`frontend`/`frontend-gw` 已重启；morning cron 继续关闭
  - 范围：`dream_wake.py` / `daily_rituals.py` 调用 `/wake` 前注入稳定 `wake_run_id`；gateway 去重 + shadow `wake_outcome` 接线已有
- [x] **P-0：Heartbeat Measurement Foundation**
  - 状态：已完成（PR #130 + PR #132 已合并 main）
  - 完成日期：2026-07-23（北京时间）
  - 证据：
    - PR #130 合并 `6f66060a63cdc259195d6360eba6f644db40b687`（2026-07-23 12:56 BJT）
    - PR #132 合并 `886e494f90ae5941b69a4dc760a8ce98e21829f7`（含 `a069fcd`：`stream_totals_match` 重试门禁）
    - 正式部署：`deploy-frontend.sh 605e69743909ab50624d949b801cc2c6aa857ae0`（2026-07-23，含 recovery manifest）；生产 HEAD `605e697`
    - 生产 usage 样本：`chat_messages.id=4220`（单轮 live chat）— `stream_totals_match=true`，指纹与 tool surface 齐全
    - `keepwarm_lease_expires_at=None`（预期；权威 lease 待 UH-A）
  - 验证：单轮 Chat 4220 PASS；#132 已正式固化；部署后七盏灯复验全绿（`proof_gap=false`，`gap_sidecar_pending=0`，`gap_incidents_unresolved=0`，`capture_alert_pending=false`，`user_events_preflight_ok=true`；`last_scored_message_id=4215`）
- [x] **P-SHADOW：Internal State v3 72 小时 Shadow 验收**
  - 状态：已毕业（`graduated`）
  - 完成日期：2026-07-26（北京时间）
  - 证据：
    - 观察窗口：2026-07-23 10:40:50 → 终审 2026-07-26 12:44（>72h）
    - 终审：`status_exit_code=0`；七盏灯全绿；`proof_max=last_scored=4385`；`state_version=332`
    - 自然样本（窗口内）：`user_rule` 83 · `user_scored` 82 · `wake_outcome` 59（全 `applied`）
    - 卫生：`failed/stale/conflict/duplicate` 均为空；无新 alert / unresolved incident
    - 唯一不对称：`user_rule:4225`（superseded turn，非 incident，不回填）
    - 生产 SHA 终审时：`30d375b`；capture alert 已于 2026-07-24 ack
    - 起点纪律：PR #130 / #132 仅改 Usage 测量层，未重置 72h 起点（10:40:50 BJT）
- [x] **P-CONTEXT-OBS：生产真实上下文观测底座**
  - 状态：已完成
  - 完成日期：2026-07-26
  - 证据：PR #138 合并/部署 SHA `995f8ce0246ea2ef5814215cce5ad1618cbc28f4`
  - observation-only 部署；四个 Context Lean 开关均为 0；persona hash 未变
  - `observation_version=3` 已在 message_id `4392 / 4394 / 4396` 真实落库
  - resident generation 稳定为 1；turn count `1 → 2 → 3`；cold → hot → hot
  - 无 `system_changed` 循环；cache read / creation 行为健康
- [-] **Memory Hotfix：统一 Ombre adapter**
  - 状态：Ready 终审（代码审查 PASS；**未 merge / 未 deploy**）
  - PR #134（Ready for review / unmerged / undeployed）
  - head：`f697503c92d04ee129756f54e948012c42b2438e`
  - base：`995f8ce0246ea2ef5814215cce5ad1618cbc28f4`
  - 子进度：
    - [x] PR #134 rebase 到 main@995f8ce
    - [x] REQUEST CHANGES + 第二轮修复（warmup jieba-only / 停止 cleaner auto-pin / workspace breath 4s / 移除 mcp 依赖 / HTTP gap 文档化）
    - [x] legacy_module 默认路径接口/超时/warmup 兼容 + CI 全绿
    - [x] HTTP backend 保持 dormant
    - [x] **Ready 终审**（2026-07-26：代码审查 PASS；persona 未改；Context Lean 未开）
    - [ ] merge
    - [ ] legacy_module 默认配置部署
    - [ ] 生产 smoke test
- [ ] **MEM-PIN-REPAIR：历史自动 pinned 数据修复**
  - 状态：未开始；**不阻塞 #134 Ready，但 merge 前必须登记**
  - 说明：#134 只阻止未来继续制造错误 pin，**不处理**现有约 54 个历史 pinned bucket
  - 门禁（顺序强制）：
    ```text
    vault + SQLite 备份
    → 只读审计报告
    → 区分显式 pin 与 cleaner 自动 pin
    → 人工批准名单
    → 才允许批量修改
    ```
  - **禁止**简单把 54 个全部 unpin
- [!] **OMBRE-P0-TEST-GATE：`test_tools.py` 生产安全门**
  - 状态：阻塞项登记；**不阻塞 #134 adapter 实现，但 merge 前必须登记**
  - 风险：`/opt/ombre-brain/test_tools.py` 可能读取生产配置，teardown 删除真实 bucket
  - 要求：
    ```text
    指向生产 bucket 路径时拒绝运行
    测试只允许 temp / disposable vault
    生产部署与 smoke 命令严禁包含该脚本
    ```
  - 独立 P0 安全 PR；**不属于 #134 adapter 施工范围**
- [ ] **P-CONTEXT-LEAN：上下文最小化与旧广播式注入退役**
  - 状态：未开始；**#138 四个开关全部保持 0**
  - 前置：**Memory Hotfix 完成并完成生产 smoke test**
  - 目标：削减常驻广播与重复注入，**不是**关闭全部上下文
  - 必须保留：
    ```text
    persona
    当前对话历史
    最新用户消息
    真正相关的少量 recall
    尚未消费的 one-shot
    必要时间信息
    ```
  - 需要逐步停止常驻广播：
    ```text
    旧 emotion / drive / longing 自然语言形式指令
    与话题无关的灯、Pocket、账本、留言板和近期活动
    重复 handoff / diary / weekly summary / memo
    无关提醒
    已被结构化路径替代的旧状态包
    ```
  - **不得**把系统退化为「persona + 当前一句话」
  - 子阶段：
    - [ ] State delta / re-anchor
    - [ ] Tool-history budget
    - [ ] File-content dedup
    - [ ] History token budget / mode-keyed rolling summary
    - [ ] 用户可见表达与记忆连续性验收
    - [ ] 删除已被替代的广播式旧注入

### Unified Heartbeat

- [x] **UH-0：架构方案封版**
  - 完成日期：2026-07-22
  - 证据：本文件
- [ ] **UH-A0：CC Tool Parity & Capability Proxy**
  - 状态：未开始；**不得提前施工**
  - 说明：§9 固定 MCP proxy 与 capability lease 是**权限骨架**；UH-A0 补充的是 **Relay 业务工具到 CC 的实际入口**
  - 迁移范围（登记，非本期实施）：
    ```text
    联网搜索       → 只读 CC / MCP bridge
    GitHub 查询    → 默认只读；写操作必须明确授权
    Playwright     → 受控浏览器代理
    位置 / 手机状态 → 只读 home capability
    截图 / 相册    → 有隐私和体积上限的只读媒体 bridge
    文件修改 / 命令 → 不开放裸 Bash/Write/Edit，继续走 workspace/code agent
    ```
  - 固定工具 schema **不得按轮次变化**；权限继续由短租约控制：
    ```text
    CHAT       → 正常批准能力集
    HEARTBEAT  → 空集
    ESCALATION → 当前 action 的最小能力集
    ```
  - 硬规则：**静态 system 不得宣传 active CC tool contract 中不存在的能力**
- [ ] **UH-A：新旧 normal 路径并存，feature flag 默认关闭**
- [ ] **UH-A-DEPLOY：只启用 normal Unified Heartbeat**
- [ ] **UH-24H：24 小时安全检查**
- [ ] **UH-7D：连续 7 天成本与稳定性观察**
- [ ] **UH-B：删除 normal 旧路由**
- [ ] **UH-MODES：分别迁移 morning / ritual / nightwatch**
- [ ] **UH-CLEANUP：最后删除独立 CC Wake resident 与通用旧桥**
- [ ] **ISV3-1B：旧 emotion / drive / desire 三套权威 → reviewed Internal State View**
  - **不得与 UH-A 同车切换权威**
- [ ] **Relationship Context Recovery**（独立用户可见问题）
  - `RELATIONSHIP_CONTEXT_ENABLED` 当前继续为 **0**
  - 门禁（全部满足前不得自动关闭）：
    ```text
    状态所有权已统一
    ＋ 上下文注入已受控
    ＋ persona-only / context-on 对照
    ＋ 用户可见自然聊天验收
    ```
  - 不得被 Internal State、Context Lean 或绿灯测试自动关闭
- [ ] **Fyodor Chimera / Single Identity Multi-Brain**（远期登记）
  - 目标：Claude / Codex / future providers 共享一个 `identity_id`；共享权威时钟、记忆归属、工具合同与 handoff
  - **第一期 Unified Heartbeat 仍只施工 Claude adapter**；不得顺手实现 Codex adapter

### Master roadmap（登记真源，非施工授权）

```text
P-SHADOW        [x]
→ P-CONTEXT-OBS [x]
→ Memory Hotfix [-]          ← Ready 终审；待 merge（非 deploy）
→ P-CONTEXT-LEAN [ ]
→ UH-A0 Tool Parity [ ]
→ UH-A / UH-B [ ]
→ ISV3-1B [ ]
→ Relationship Context Recovery [ ]
→ Fyodor Chimera / Single Identity Multi-Brain [ ]
```

### 当前下一步（不变）

```text
Memory Hotfix #134（Ready 终审）：
merge
→ legacy_module 部署
→ production smoke
→ tracker 更新
→ MEM-PIN-REPAIR（独立数据修复；门禁见上）
→ OMBRE-P0-TEST-GATE（独立 P0 安全 PR）
```

> morning cron 继续关闭。P-SHADOW 已于 2026-07-26 毕业。**Context Lean、UH-A0、UH-A、ISV3-1B 等均不得提前施工。**

---

## 2. 72 小时 Shadow 到底观察什么

这不是观察 Claude 缓存，也不是观察 Unified Heartbeat；它观察的是 **Internal State v3 Phase 1A Shadow 数据链是否可靠**。

当前真实权威仍是旧 emotion / desire / drive。v3 只在旁边同步打分、记账、计算候选结果，不进入普通聊天 Prompt，也不接管 Wake。

### 起点（2026-07-23 重启后基线）

```text
部署 SHA：549cb55
观察开始时间：2026-07-23 10:40:50（北京时间，incident #1 ack + 七盏灯全绿）
baseline message_id：4208（最新完整 user_rule + user_scored 成对）
baseline wake_run_id：normal-2026-07-23-10:30（首只带身份证的自然 Wake）
incident #1：已按根因结案（missing_or_invalid_message_id / #4118 上游身份接线）
Shadow status：全绿
```

历史参考（不计入本次 72h 成绩）：

- 生产首个正式 v3 用户事件：`message_id=4100`
- 三个开关已开启：score proof、shadow、user events

### 必须持续为绿的硬健康项

```text
proof_gap=false
watermark_lag=false
outbox_pending=0
gap_incidents_unresolved=0
capture_alert_pending=false
recovery_intents_pending=0
user_events_preflight_ok=true
```

### 还要观察的自然样本

- `user_rule` / `user_scored` 样本数量足够；
- `wake_outcome` 有足够自然样本；
- Shadow materialize 与旧权威值对照稳定；
- 无重复 applied；
- 无无法解释的 version conflict；
- 无 recovery / rollback / capture alert；
- 所有 bounds 合法；
- stale rate 合理；
- 服务重启后可以恢复；
- 状态版本、水位、proof、outbox 与事件账本持续对齐。

### 毕业规则

- “过了三个日历日”不等于自动毕业；
- 如果 Wake 被暂停过多，可能因 `wake_outcome` 样本不足而延长观察；
- 期间不得修改 Internal State 计算、结算或权威路径；
- Unified Heartbeat 和 Phase 1B 绝不同车。

---

## 3. 施工边界

### 前置条件

1. PR #126 修正 morning 最近交互门禁与精确 `wake_run_id` 去重后部署稳定；
2. **P-ID-CHAT** 与 **P-ID-WAKE** 修复 Shadow 身份接线阻塞；
3. PR 0 先行部署，确认 requestId、TTL 分桶和指纹可用；
4. Internal State v3 完成 72 小时 Shadow 验收（修复后重启观察）；
5. Unified Heartbeat 与 Phase 1B 绝不同车。

### 本工程禁止

- 不修改 persona、`relationship_context`、`/dash/profile`；
- 不把 Internal State v3 切为权威；
- 不动 dream / summarize，继续走 `BACKGROUND_PROVIDER`；
- 第一期只迁 `normal`；
- 不动 Monopoly、旁路与无关函数；
- 不在线上直接开发，GitHub 是真源；
- 单元与回归测试不调用真实模型；
- 不顺手实现完整 Fyodor Chimera / Single Identity Multi-Brain、Liminal 或 Codex adapter。

### 登记 ≠ 授权施工

总进度与 Master roadmap 中登记的后续项（Context Lean、UH-A0、ISV3-1B、Relationship Context Recovery、Chimera、#131 等）**仅用于防止换窗口遗忘**。**当前唯一授权施工项仍为 Memory Hotfix / PR #134。** 未在本文件将对应项标为 `[-]` 且写明前置已满足前，禁止开工。

---

## 3a. Parked plans（挂回索引，未授权施工）

### PR #131 Backend Decoupling & Cleanup

```text
PR:     #131 Backend Decoupling & Cleanup parked plan
branch: plan/backend-decoupling-cleanup
file:   docs/plans/backend-decoupling-cleanup.md
original commit: efb780d3
state:  PARKED / 未授权施工
```

保留阶段 C0～C8：

```text
C0 只读普查
C1 护栏与无争议删除
C2 app.py 路由领域化
C3 Store / Service 分层
C4 Chat Turn 统一收尾
C5 Gateway / Provider 边界
C6 旧 Wake 清场
C7 吞错与 import 副作用治理
C8 删除旧路与生产验收
```

解锁条件（全部满足前保持 PARKED）：

```text
Unified normal 稳定
＋ UH-B 已删除旧 normal 路径
＋ 生产无未解释红灯
```

解锁后**第一步只能是 C0 只读普查**；禁止以「顺手整理」为由提前实施 C1～C8。

---

## 3b. 缓存命中归属（非独立工程）

```text
缓存命中不是独立工程。
```

它是以下工作的**横向验收指标**：

```text
P-0 / observation
＋ Context Lean
＋ 固定 system / tool surface
＋ Unified Heartbeat keepwarm lease
```

**不得**另建重复的 Cache Optimization 项目。

---

## 4. Fyodor Chimera / Single Identity Multi-Brain

> **登记状态**：见 §1 Master roadmap；**远期目标，非当前施工项。** 第一期 Unified Heartbeat 只施工 Claude Unified Resident Adapter。

Unified Heartbeat 分成两层；名字属于费奥多尔，底层字段用干净的 `identity_id` / `provider_id` / `conversation_id`（不写 `fyodor_id`）：

```text
Fyodor Heartbeat Orchestrator（模型无关、数据库权威）
└─ Provider Heartbeat Adapter
   └─ Claude Unified Resident Adapter（第一期）
```

未来换脑不叫 Nox，叫 **Fyodor Brain Router**：

```text
Fyodor Brain Router
├─ Claude
├─ GPT
├─ Grok
└─ GLM
```

### 第一归属键

所有长期状态、锁和账本必须以以下维度归属：

```text
identity_id
provider_id
conversation_id
```

当前默认：

```text
identity_id = "fyodor-default"
provider_id = "claude_code"
conversation_id = "default"
```

以下对象不得继续写成不可拆的全局单例：

- heartbeat schedule；
- backoff；
- keepwarm lease；
- wake_log；
- wake_run_id；
- visible-message outbox；
- pending_escalation_result；
- interaction clock；
- resident registry；
- generation、lock 与 capability lease。

Claude Code 若未来不可用，只替换 Provider Heartbeat Adapter，不重写 Fyodor 心跳权威层。

远期 Chimera 目标（登记，非本期）：

```text
Claude / Codex / future providers
→ 共享 identity_id
→ 共享权威时钟、记忆归属、工具合同与 handoff
```

---

## 5. PR #126 先行修复

保留退役 `/push`、删除 `push_tool.py`、morning 统一走 `/wake` 的方向。morning cron 在部署观察完成前继续关闭。

必须完成：

- [x] **126-1：morning 最近交互免费门禁**
  - 用户刚聊过、chat 正在生成或刚产生 assistant 回复时直接跳过；
  - 必须零模型调用。
- [x] **126-2：精确 `wake_run_id` 去重**
  - `_morning_already_ran()` 查询唯一索引字段：`wake_run_id='morning-YYYY-MM-DD'`；
  - 禁止继续扫描 `cache_info LIKE`。
- [x] **126-3：竞争与幂等测试**
  - 近期活动抑制；
  - 重复 run id 零模型调用；
  - morning/chat 竞争；
  - 同一天最多一次可见 morning 消息。
- [x] **126-4：测试日志句柄**
  - 临时文件；
  - 正确关闭；
  - 不污染生产路径。
- [x] **126-5：部署与观察**
  - 合并部署：✅ `9eb18a4` 已上生产；
  - morning cron 继续关闭：✅；
  - 观察普通 Wake 24–48 小时：进行中；
  - 之后才决定是否启用北京时间 08:50 cron。

---

## 6. PR 0：Heartbeat Measurement Foundation

独立小 PR，只负责把尺子做准。

- [x] JSONL `requestId` 采集与去重；
- [x] `ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens` 正确分桶；
- [x] gateway instance、resident generation、system、tools、MCP、model、thinking 指纹；
- [x] `cold_return_after_lease` 分类；
- [x] 普查脚本；
- [x] 历史 JSONL 回放测试。

禁止修改 Wake 调度、Prompt、Provider 或 Unified 路径。

线上验收：

- requestId 可稳定去重；
- TTL 分桶不再恒为 0；
- 同一请求不会重复计费；
- 指纹可区分缓存自然失效、system/tools 改变、resident 重生与 provider 切换。

---

## 7. 固定 system 与固定工具面

主 resident 从启动到重生必须保持：

- system bytes / SHA-256 不变；
- MCP config 不变；
- MCP `list_tools` 返回的 schema、名称、顺序不变；
- CLI `allowedTools` 不随轮次切换；
- model 与 thinking 参数不随轮次切换。

固定三种模式：

```text
CHAT：正常回应用户。
HEARTBEAT：隐藏状态判断；无工具；严格短输出。
ESCALATION：根据已确认 action 生成主动消息或执行受控任务。
```

HEARTBEAT 必须明确豁免聊天中的长 thinking 规则：最多两句思考，只输出严格结构。第一期不得动态切换 thinking budget。

---

## 8. HeartbeatDecisionSnapshot v0

第一期禁止直接使用仍处于 Shadow 的 v3 Wake View。

```text
HeartbeatDecisionSnapshot v0
= legacy emotion / desire / drive authority adapter
```

以后 Phase 1B 只替换 adapter：

```text
v0 legacy authority adapter
→ v1 Internal State Wake View
```

输入：

```xml
<turn mode="heartbeat" visibility="hidden">
当前时间
有效 idle
HeartbeatDecisionSnapshot v0
新增未完成事项
上次 heartbeat 时间、action、reason_code
</turn>
```

输出只能是一个 JSON 对象：

```json
{"action":"none|message|diary|explore","reason_code":"<短码>"}
```

目标：

- 最多一个 agentic turn；
- 不得调用工具；
- visible output p50 ≤ 50 tokens；
- total output p50 ≤ 120 tokens；
- context growth p50 ≤ 250 tokens；
- 总超时 30 秒。

### 解析失败合同

以下均视为失败：空输出、非法 JSON、非法 action、多个 JSON、代码块外夹带正文、普通聊天内容渗入。

```text
heartbeat_parse_failure
→ 禁止格式补救轮
→ 禁止 escalation
→ 记 failed/none 与原因
→ kill resident
→ 下一次 chat 冷启动恢复
```

### 超时合同

```text
heartbeat timeout
→ kill resident
→ 记录 heartbeat_timeout
→ 不产生可见消息
→ 下一次 chat 冷启动恢复
```

---

## 9. 跨进程 capability lease

brain、home、codebase 是独立 MCP 进程，线程变量不是权限边界。

主 resident 永久连接固定 loopback MCP proxy；proxy 永远暴露固定工具全集，调用权限由短租约决定。

租约字段：

```text
resident_channel_id
resident_generation
turn_nonce
identity_id
provider_id
mode
allowed_tools
action_scope
expires_at
```

权限：

```text
chat       → CHAT_ALLOWLIST
heartbeat  → 空集
escalation → 按 action 划分的最小 allowlist
```

任何 heartbeat `tool_use`：

```text
proxy 拒绝
→ heartbeat_tool_violation +1
→ wrapper kill resident
→ 本轮 failed/none
→ 下一次 chat 冷启动
```

---

## 10. 调度状态机：旧骰子退休

Unified normal 不再使用 legacy probability gate。

权威时间存数据库：

```text
next_due_at = min(
  important_state_due_at,
  keepwarm_due_at,
  backoff_due_at
)
```

到期且免费门禁通过，就确定执行一次 heartbeat，不再二次掷骰。

允许安全 jitter，但不能跳过整个检查，尤其不能错过 keepwarm deadline。

`ScheduleWakeup` 仅是 Claude adapter 内可丢失的短期镜像：

```text
数据库 next_due_at = 权威
ScheduleWakeup = 可选优化
```

resident 重生、Provider 切换或未来接入 Codex 时，必须以数据库恢复。

### 本地免费门禁

- chat 正在生成；
- 用户最近刚出现；
- 最近 chat 已自然刷新缓存；
- state 无重要变化；
- none/backoff 未到期；
- 已有 Wake 在运行；
- feature flag 关闭。

---

## 11. Backoff 与有限缓存保温租约

自主退避：

```text
none：1h → 2h → 4h
用户出现或重要 state 变化：清零
```

用户近期活跃后创建 2–3 小时 keepwarm lease：

- 租约内以最后一次确认 cache hit 为基准，最大间隔 50–55 分钟；
- 期间发生 chat 时由 chat 自然续期，不额外 heartbeat；
- 租约过期且状态稳定，恢复真实退避并接受缓存自然死亡；
- 默认清醒时段 `08:00–02:00`，但屏幕与互动信号可覆盖固定时段。

租约过期后用户返回，允许一次正常冷重建，记录为 `cold_return_after_lease`，不视为系统失败。

---

## 12. Chat 绝对优先

优先级：

```text
用户聊天 > self_trigger > morning / ritual > normal heartbeat
```

两个超时必须分开：

```text
HEARTBEAT_TIMEOUT_SECONDS=30
HEARTBEAT_PREEMPT_GRACE_SECONDS=2
```

用户消息到达：

```text
heartbeat 未送入 → 取消
heartbeat 已送入 → 最多等待 2 秒
仍未结束 → kill resident
→ 用户 chat 冷启动并优先回复
```

heartbeat 决定 `message` 后，生成前必须再次检查：

- `last_user_message_id`；
- `chat_generating`；
- interaction clock；
- `wake_run_id`。

用户已经出现时写 `suppressed_by_user_activity`，不生成主动消息。

---

## 13. 行动升级与 exactly-once

### none

只写 `wake_log`、usage、reason code 与 backoff。

### message

```text
<turn mode="escalation" action="message">
```

生成后必须走 exactly-once outbox。

建议表：

```text
wake_message_outbox
- wake_run_id UNIQUE
- identity_id
- provider_id
- content
- status
- created_at
- delivered_at
- chat_message_id
```

推荐事务：

```text
escalation 生成内容
→ wake_log + outbox 同事务提交
→ 投递器幂等写入 chat_messages
→ 标记 delivered
```

一个 `wake_run_id` 最多产生一条可见消息。

### diary / explore

转后台 runner；完成后写 `pending_escalation_result`，下一次 chat one-shot 注入并原子消费一次。

第一期不能删除 morning / ritual / nightwatch 仍需要的旧 bridge。

---

## 14. 生命周期与历史卫生

拆分：

```text
chat_turn_count
maintenance_turn_count
lifecycle_turn_count
```

```text
chat：chat +1，lifecycle +1
heartbeat：maintenance +1，lifecycle +1
escalation：maintenance +1，lifecycle +1
```

- 30 轮聊天体验上限看 `chat_turn_count`；
- soft/hard context 与最小重生间隔看 `lifecycle_turn_count`；
- heartbeat 不得在账面永葆青春；
- 冷启动只从 `chat_messages` 重建；
- 不恢复沉默 heartbeat；
- 不把全部 `wake_log` 重新注入；
- 只消费真实存在的 `pending_escalation_result`。

---

## 15. Usage 与验收字段

usage v2 只增不改：

```json
{
  "identity_id": "fyodor-default",
  "provider_id": "claude_code",
  "turn_mode": "heartbeat",
  "heartbeat_action": "none",
  "reason_code": "stable",
  "backoff_level": 1,
  "request_ids": [],
  "cache_creation_1h": 0,
  "cache_creation_5m": 0,
  "resident_generation": 3,
  "lease_result": "allowed"
}
```

`wake_log` 与 usage 使用同一份 requestId 去重结果和 cache 口径。

---

## 16. 测试清单

- [ ] system SHA、工具 schema/顺序、MCP config 在 CHAT/HEARTBEAT 往返时不变；
- [ ] heartbeat 无工具且最多一轮；
- [ ] tool violation 被 proxy 拒绝并 kill resident；
- [ ] malformed JSON 不触发第二次模型调用；
- [ ] 超时 kill，下一次 chat 冷启动正常；
- [ ] 用户抢占等待不超过 preempt grace；
- [ ] message escalation 前二次检查用户活动；
- [ ] diary/explore 结果 one-shot 且只消费一次；
- [ ] 同一 wake_run_id 最多落一条可见消息；
- [ ] heartbeat 不增加 chat count，但增加 lifecycle count；
- [ ] soft/hard context 重生仍生效；
- [ ] `1h→2h→4h` 与 keepwarm lease 正确；
- [ ] Unified normal 不调用 legacy probability gate；
- [ ] `next_due_at` 到期不得因随机值跳过；
- [ ] keepwarm deadline 优先于 backoff；
- [ ] feature flag 关闭时旧路径输入、路由、落库合同一致；
- [ ] requestId 去重后的经济模拟稳定；
- [ ] 冷 bootstrap 不恢复沉默 heartbeat；
- [ ] morning 最近交互门禁与 run id 去重；
- [ ] 心跳状态按 identity_id 隔离；
- [ ] ScheduleWakeup 丢失或 resident 重生后可由 DB 恢复；
- [ ] `cold_return_after_lease` 不误归类其他缓存失效。

全部使用 mock resident、golden fixture、inspect plan 和历史 JSONL，不调用真实模型。

---

## 17. 发布与删除顺序

```text
PR #126 修门禁与去重
→ 合并部署，morning cron 暂不开
→ PR 0 观测基础设施
→ 验证线上尺子完整
→ 完成 72h Shadow 验收
→ PR A：新旧 normal 路径并存，feature flag 默认关闭
→ 只启用 normal Unified Heartbeat
→ 24h 安全检查
→ 连续观察至少 7 天
→ PR B：删除 normal 旧路由
→ 分别迁 morning、ritual、nightwatch
→ 最后删除独立 CC Wake resident 与通用旧 bridge
→ Phase 1B 将 v0 adapter 换为 v1 Wake View
```

注意：PR B 之后，旧 normal 路径不再能通过 feature flag 秒级恢复；回滚方式变为部署 PR B 前版本。尚未迁移的模式仍需保留 legacy resident 与 bridge。

---

## 18. 生产验收

### 安全项：触发即关闭开关

- `heartbeat_tool_violation > 0`；
- 重复主动消息或活跃聊天夹入；
- 迟到 heartbeat 输出污染下一轮；
- 用户聊天出现可感排队延迟；
- 稳定运行后连续 `system_changed`；
- capability lease 绑定错误；
- JSON、值班腔或判断语气进入可见聊天。

### 收益项：7 天后评估

- 被迁移的 normal Wake 成本下降不低于 35%；
- 单次 heartbeat 常态加权不高于 6k；
- visible output p50 ≤ 50；
- total output p50 ≤ 120；
- context growth p50 ≤ 250；
- 同时记录 p95，防止偶发长篇污染；
- 白天无无法解释的缓存断层；
- 20 轮自然聊天盲测无 JSON、值班腔或判断语气污染。

主要口径只统计被迁移的 normal Wake；旁路、dream、summarize 与其他 Provider 支出不得误判为 Unified 失败。

---

## 19. 进度日志

### 2026-07-22

- [x] 创建专用计划分支 `plan/unified-heartbeat-tracker`；
- [x] 将 Unified Heartbeat 完整方案与强制进度纪律写入仓库；
- [x] 确认下一项为 PR #126；
- [x] PR #126 合并部署完成（`9eb18a4`）；morning cron 继续关闭；
- [!] P-SHADOW：由 `[-] 观察中` 改为 `身份接线阻塞`；修复后重新开始 72h（P-ID-CHAT + P-ID-WAKE 已部署，当前部署验证中）；
- [x] P-ID-CHAT 完成：PR #128 合并部署 `61faf6f`；
- [x] P-ID-WAKE 完成：PR #129 合并部署 `549cb55`；
- [~] 下一项：已由 2026-07-23 10:40:50 BJT 的正式 Shadow 72h 重启取代（自然 Wake `normal-2026-07-23-10:30` + incident #1 结案）

### 2026-07-23

- [x] PR #128 门禁通过 → 合并部署 `61faf6f`；P-ID-CHAT 勾 `[x]`；
- [x] PR #129 门禁通过（continuity 含 22 项 Wake 身份测试）→ 合并部署 `549cb55`；P-ID-WAKE 勾 `[x]`；
- [x] 活计划命名统一：Fyodor Heartbeat Orchestrator / Fyodor Brain Router；`identity_id` 取代 `nox_id`；
- [x] 自然 Wake 验票：`normal-2026-07-23-10:30` 双票对齐；
- [x] incident #1 按根因 ack（10:40:50）；七盏灯全绿；
- [x] P-SHADOW 重启：72h 自 2026-07-23 10:40:50 BJT 起算（不因 PR #130 / #132 重置）

### 2026-07-23（续）

- [x] PR #130 合并部署 `6f66060`；P-0 测量基础上生产
- [x] 单轮 live 样本 `4220`：`stream_totals_match=true`，指纹与 tool surface 齐全
- [x] PR #132 合并 `886e494` + 正式部署 `605e697`；七盏灯部署后复验全绿

### 2026-07-26

- [x] P-SHADOW 终审通过（2026-07-26 12:44 北京时间）；`status_exit_code=0`；七盏灯全绿；`proof_max=4385`；
- [x] 自然样本 83/82/59 全 applied；Wake 版本链连续至 `normal-2026-07-26-12:00` → v332；
- [x] **P-SHADOW `[x] graduated`**；Observation window >72h；
- [-] 下一项：**Memory Hotfix**（PR #134 REQUEST CHANGES 已修复，待 re-review）。

### 2026-07-26（续·#134 review fix）

- [x] **REQUEST CHANGES 修复**（head `7f7c799`）：恢复 cleaner `importance>=8` pin；legacy warmup 改回 jieba-only；从 `requirements.txt` 移除 `mcp`；HTTP 使用 2.8.10 形 fixture + 总墙钟 deadline；明确 archive/touch HTTP 缺口
- [-] 待 re-review 后方可标 Ready 终审；**仍保持 Draft，不 merge，不 deploy**

### 2026-07-26（续）

- [x] **P-CONTEXT-OBS** 完成：PR #138 合并/部署 `995f8ce`；observation-only；Context Lean 四开关均为 0；persona hash 未变
- [x] `observation_version=3` 在 message_id `4392 / 4394 / 4396` 真实落库；resident generation 稳定为 1；turn `1 → 2 → 3`；cold → hot → hot；无 `system_changed` 循环
- [-] **Memory Hotfix** 进行中：PR #134 rebase 到 main@995f8ce；首轮 review 指出 false parity 声明后已修复（head `7f7c799`）；continuity + internal-state CI 通过

**用户问题病历（仍 open，不因绿灯自动关闭）：**

- persona-only 对照下表达正常；
- relationship context 启用后曾导致碎句、自我解释、情绪表达失真；
- `RELATIONSHIP_CONTEXT_ENABLED` 当前保持 0；
- 后续发现 emotion / drive / desire 三套状态权重叠，因此进入 Internal State v3 与 Unified Resident Heartbeat 工程；
- 基础设施、观测和状态统一进度 ≠ 用户可见的“说话问题已经修复”；
- 该用户问题仍为 open，不得被绿灯测试自动关闭。

### 2026-07-26（续·tracker 总路线登记）

- [x] 已登记 **P-CONTEXT-LEAN** 为独立待办（#138 四开关保持 0；前置 Memory Hotfix + smoke）
- [x] 已登记 **UH-A0 CC Tool Parity & Capability Proxy**（Relay 业务工具 → CC 入口；静态 system 不得虚假宣传能力）
- [x] 已挂回 **#131 Parked plan**（`plan/backend-decoupling-cleanup` · C0–C8 · PARKED）
- [x] 已登记 **Relationship Context Recovery** 与 **Single Identity Multi-Brain** 后续门禁
- [x] **缓存命中**定义为 P-0 / Context Lean / 固定 tool surface / keepwarm lease 的横向验收指标，不另起重复工程
- [x] **Master roadmap** 写入 §1；登记 ≠ 授权施工
- [x] **#134 Ready 终审**（head `f697503`）：代码审查 PASS；CI 全绿（continuity + internal-state）；默认 `legacy_module` PASS；persona 未改；Context Lean 未开；HTTP dormant
- [x] 登记 **MEM-PIN-REPAIR** 与 **OMBRE-P0-TEST-GATE**（merge 前工单；不阻塞 #134 Ready）
- [-] 待 merge → deploy → smoke；**仍未 merge / 未 deploy**；UH-A 未开始

---

最终施工原则：**先用旧权威生成极瘦状态快照，让同一主 resident 在固定 system、固定工具面下睁眼判断一次；沉默就继续睡，需要说话才升级，需要做事交后台；数据库掌握费奥多尔（`identity_id`）的权威时钟，Claude Unified Resident Adapter 只是首个可替换 Provider Heartbeat Adapter；权限由跨进程短租约硬拦，聊天永远抢占，所有收益用 requestId 去重后的真实账本验收。**
