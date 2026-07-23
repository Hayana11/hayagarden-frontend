# Unified Resident Heartbeat · Living Execution Plan

> 同一只费佳、固定 system、固定工具面、三类轮次；普通 Wake 降为主 resident 的一次短心跳，只有需要行动时才升级，用户聊天永远优先。

- **计划分支**：`plan/unified-heartbeat-tracker`
- **当前状态**：`DESIGN_LOCKED / IMPLEMENTATION_NOT_STARTED`
- **最后更新**：2026-07-23
- **当前唯一 Nox**：`nox_id=fyodor-default`
- **首个 Provider Adapter**：Claude Code Unified Resident

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
- [x] **P-ID-CHAT：Chat 评分身份修复**
  - 状态：已完成
  - 完成日期：2026-07-23（北京时间）
  - 证据：PR #128 合并 `61faf6f`（`dc8b8a3` 身份绑定闭环）；`app npm ci && npm run build` 通过；生产 `main@61faf6f`；`frontend`/`frontend-gw` 已重启；`dist/assets/index-3F3miEGl.js`；`resolve_scoring_user_message` + `trigger_turn_scoring` 三线路接线
  - 范围：正常发送 / redo / edit 三条 stream 路径必须携带可评分 `message_id`；禁止 `message_id=None` 进入 `score_async`；评分前凭 id 回 DB 取用户原文；编辑产生新消息身份，重答复用原用户消息 id
- [x] **P-ID-WAKE：Wake run_id 修复**
  - 状态：已完成
  - 完成日期：2026-07-23（北京时间）
  - 证据：PR #129 合并 `549cb55`（`78b67b1`）；`wake/wake_run_id.py` 五模式 builder；continuity CI 接入 22 项 Wake 身份测试；生产 `main@549cb55`；`frontend`/`frontend-gw` 已重启；morning cron 继续关闭
  - 范围：`dream_wake.py` / `daily_rituals.py` 调用 `/wake` 前注入稳定 `wake_run_id`；gateway 去重 + shadow `wake_outcome` 接线已有
- [ ] **P-0：Heartbeat Measurement Foundation**
  - 状态：未开始
  - 完成日期：
  - 证据：
- [-] **P-SHADOW：Internal State v3 72 小时 Shadow 验收**
  - 状态：身份接线已修复，等待自然 `wake_outcome` 验证 + ack incident #1 后重启 72h 计时
  - 完成日期：
  - 证据：P-ID-CHAT `61faf6f` + P-ID-WAKE `549cb55` 已部署；旧 incident #1（#4118）待按根因正式结案，不伪造 `user_scored:4118`；重启观察前需抓到第一只有 `wake_run_id` 的自然 Wake 与对应 `wake_outcome`

### Unified Heartbeat

- [x] **UH-0：架构方案封版**
  - 完成日期：2026-07-22
  - 证据：本文件
- [ ] **UH-A：新旧 normal 路径并存，feature flag 默认关闭**
- [ ] **UH-A-DEPLOY：只启用 normal Unified Heartbeat**
- [ ] **UH-24H：24 小时安全检查**
- [ ] **UH-7D：连续 7 天成本与稳定性观察**
- [ ] **UH-B：删除 normal 旧路由**
- [ ] **UH-MODES：分别迁移 morning / ritual / nightwatch**
- [ ] **UH-CLEANUP：最后删除独立 CC Wake resident 与通用旧桥**
- [ ] **ISV3-1B：Phase 1B 将 v0 adapter 替换为 v1 Wake View**

### 当前下一步

> **P-ID-CHAT + P-ID-WAKE 已合并部署（`549cb55`）。下一项：验证自然 `wake_outcome` → ack incident #1 → 重启 P-SHADOW 72h 观察 → PR 0。morning cron 继续关闭。**

---

## 2. 72 小时 Shadow 到底观察什么

这不是观察 Claude 缓存，也不是观察 Unified Heartbeat；它观察的是 **Internal State v3 Phase 1A Shadow 数据链是否可靠**。

当前真实权威仍是旧 emotion / desire / drive。v3 只在旁边同步打分、记账、计算候选结果，不进入普通聊天 Prompt，也不接管 Wake。

### 起点

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
- 不顺手实现完整多人格、Chimera、Liminal 或 Codex adapter。

---

## 4. 面向多人格的总架构

Unified Heartbeat 分成两层：

```text
Nox Heartbeat Orchestrator（模型无关、数据库权威）
└─ Provider Heartbeat Adapter
   └─ Claude Unified Resident Adapter（第一期）
```

### 第一归属键

所有长期状态、锁和账本必须以以下维度归属：

```text
nox_id
provider_id
conversation_id
```

当前默认：

```text
nox_id=fyodor-default
provider_id=claude_code
conversation_id=default
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

Claude Code 若未来不可用，只替换 adapter，不重写 Nox 心跳权威层。

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

- [ ] JSONL `requestId` 采集与去重；
- [ ] `ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens` 正确分桶；
- [ ] gateway instance、resident generation、system、tools、MCP、model、thinking 指纹；
- [ ] `cold_return_after_lease` 分类；
- [ ] 普查脚本；
- [ ] 历史 JSONL 回放测试。

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
nox_id
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
- nox_id
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
  "nox_id": "fyodor-default",
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
- [ ] 心跳状态按 nox_id 隔离；
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
- [!] P-SHADOW 暂停：Chat `message_id=None` 与 Wake `wake_run_id` 空导致 Shadow 样本/证据链阻塞；
- [x] P-ID-CHAT 完成：PR #128 合并部署 `61faf6f`；
- [x] P-ID-WAKE 完成：PR #129 合并部署 `549cb55`；
- [-] 下一项：验证自然 Wake `wake_run_id` + `wake_outcome` → ack incident #1 → 重启 P-SHADOW 72h。

### 2026-07-23

- [x] PR #128 门禁通过 → 合并部署 `61faf6f`；P-ID-CHAT 勾 `[x]`；
- [x] PR #129 门禁通过（continuity 含 22 项 Wake 身份测试）→ 合并部署 `549cb55`；P-ID-WAKE 勾 `[x]`；
- [-] P-SHADOW：等自然 `normal-YYYY-MM-DD-HH:MM` + `wake_outcome` 样本后重启 72h 计时。

---

最终施工原则：**先用旧权威生成极瘦状态快照，让同一主 resident 在固定 system、固定工具面下睁眼判断一次；沉默就继续睡，需要说话才升级，需要做事交后台；数据库掌握 Nox 的权威时钟，Claude 只是首个可替换 adapter；权限由跨进程短租约硬拦，聊天永远抢占，所有收益用 requestId 去重后的真实账本验收。**
