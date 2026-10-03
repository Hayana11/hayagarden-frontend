# Unified Heartbeat U0｜Wake × 主聊天统一合同（缓存目标修正版）

> 状态：docs-only 合同冻结候选；不接生产、不改 flag、不 merge/deploy。
> 基线：`main@4b82f2fb6921f9298d0498449c3db17814b2057e`。
> 本版修正首版偏差：Unified Heartbeat 的第一产品目标是 **同通道、同热 resident、同 session/cache 连续体以降低 Wake 调用成本**；用户可见连续性是同时获得的重要收益，但不是替代缓存目标的主目标。

## 1. 第一产品目标：不要为 Wake 再养第二套昂贵上下文

普通主聊天与普通 `normal Wake` 最终必须成为：

```text
同一个 Claude Code provider/channel
→ 同一只热 ResidentSession
→ 同一个 Claude session
→ 同一套稳定 system/cache prefix
→ 同一套固定启动 tool schema
→ Chat turn / Wake turn / Chat turn 交替发生
```

目标不是单纯“看起来像同一个人”，而是直接消除当前重复成本：

- 不再单独维护 `_CC_WAKE_RESIDENT` 的第二份 persona/system/cache；
- 不再因为普通 Wake 切换 system prompt 而触发 `system_changed` respawn；
- 不再因为普通 Wake 改 `allowed_tools` 而重启 resident；
- 不再让 Wake 冷启动重新搬一遍主聊天已经持有的长期稳定上下文；
- normal Wake 尽量直接吃主聊天已经形成的 session/cache；
- 用户回复 Wake 后继续同一 session，本身就知道刚才发生过什么，不再依赖 `wake_reply_bridge` 伪造连续性。

**产品不变量：正常 Chat → normal Wake → Chat 之间，Wake 本身不得成为 respawn 的原因。**

只有既有生命周期原因（真实进程死亡、模型切换、容量阈值、合法 history rewrite、人工切窗等）可以使 resident 重建；不能为了“这是 Wake 轮”主动切进程。

## 2. 最新项目地图中的既有施工顺序继续有效

最新版项目进度地图已经把 Unified Heartbeat 拆成两个阶段：

### UH-A0｜CC 工具能力补齐

1. 盘点 Relay 已有业务工具，定义统一 resident 必需能力；
2. 建立固定工具 schema 与 capability lease；**工具面不按轮次改变**；
3. 接入搜索、GitHub、浏览器、家庭状态、媒体等受控 bridge；
4. 做权限、缓存与用户授权验收。

### UH-A/B｜Unified Heartbeat

1. 新旧 normal 路径并存，feature flag 默认关闭；
2. 只启用 normal Unified Heartbeat；
3. 稳定后再删除 normal 旧路由；
4. morning / ritual / nightwatch 等分别迁移；
5. 最后删除独立 CC Wake resident 与通用旧桥。

因此 **UH-A0 不是旁路，也不是可跳过的安全美化**。它是“同一 resident + 同一缓存”成立的直接前置。

## 3. 当前实现事实

当前代码明确存在两只不同 resident：

- 主聊天：`_CC_RESIDENT`；
- Wake：`_CC_WAKE_RESIDENT`。

当前 `ClaudeCodeWakeRunner` 按独立 Wake resident 设计，并会：

1. 根据本次 Wake prepared tools 重算 `allowed_tools`；
2. allowlist 改变时直接修改 resident `_allowed_tools`；
3. 把 `_system_text` 置空以强制 `ensure_alive()` respawn；
4. 使用 Wake 专属 system + `WAKE_CONTRACT`；
5. 在独立 Wake session 中产生 `THOUGHTS/ACTION/CONTENT`。

`ResidentSession` 又明确把这些变化视为 respawn 原因：

- system 改变；
- tool profile 改变；
- model 改变；
- context/turn limit 等已有生命周期条件。

所以现在的独立 Wake resident **天然无法复用主聊天 resident 已经积累的热 session/cache**。

## 4. 首版错误路线正式否决：Wake 时切 text-only / resume

首版 U0 曾提出：

```text
Chat 广工具热进程
→ kill
→ text-only resume 同 session 做 Wake Renderer
→ 下一次 Chat 再恢复广工具 profile
```

这条路线在“身份连续”上可行，但与本项目真正目标冲突，因此本版正式否决为默认架构。

原因很简单：

> Wake 本身若主动造成 profile 切换 / process respawn，就会破坏我们最想保住的热通道与缓存收益。

`same session lineage` 不能替代 `same hot resident/cache`；它只能作为异常恢复能力，而不能成为正常 Wake 的执行方式。

## 5. 为什么现在也不能直接把两只 resident 变量合并

虽然最终目标就是同一 resident，但当前不能机械执行：

```text
_CC_WAKE_RESIDENT = _CC_RESIDENT
```

因为旧 WakeRunner 仍会：

- 动态改 `_allowed_tools`；
- 清 `_system_text`；
- 使用不同 stable system；
- 因此主动触发主聊天 resident respawn。

另外当前工具面还有真实差异：

- Wake 工具合同明确禁止裸 `mcp__codebase`，避免开放 patch/create_file；
- 主聊天当前 `CC_ALLOWED_TOOLS` 包含裸 `mcp__codebase` 及更宽工具面。

所以“直接共享对象”会把主聊天缓存毁掉，同时还扩大 autonomous Wake 的工具权限。

结论不是放弃同一 resident，而是：

> **先完成 UH-A0，让同一 resident 拥有固定工具 schema，同时把每轮允许做什么从进程启动参数迁到可确定性执行的 capability lease / Action Gate。**

## 6. UH-A0 的核心合同：工具面固定，权限按轮租，不按轮重启

### 6.1 固定启动 tool schema

Unified resident 启动时拥有一套稳定、可缓存的工具 schema。

正常 Chat 与 normal Wake 之间不得因为轮次类型改变：

- `--allowedTools`；
- MCP config；
- stable system 中的工具 brochure；
- tool profile。

否则等价于主动制造 cache break / respawn。

### 6.2 Capability lease

“工具在统一 schema 中存在”不等于“每一轮都获准执行”。

每轮需要一个确定性的 capability lease / action permission：

```text
turn_kind = chat | normal_wake
+ 当前 mode
+ 用户授权 / 产品规则
+ Action Gate
→ 本轮可执行能力集合
```

重要语义：

- schema 是长期固定表；
- lease 是本轮许可；
- lease 改变不能要求 Claude process respawn；
- 被 lease 拒绝的写操作必须在真正外部副作用发生前 fail closed；
- 不能仅靠 prompt 写“不要使用这个工具”当权限系统。

Capability lease 的最终字段/承载方式留到 UH-A0 施工时冻结；U0 只冻结上述语义不变量。

### 6.3 为什么 UH-A0 直接服务成本目标

完成 UH-A0 后，Chat 与 Wake 才可以共享：

```text
同一进程
同一 system
同一工具 schema
同一 session
同一 context/cache
```

而只改变一个很小的动态 turn envelope。

这才是 Unified Heartbeat 的省钱核心。

## 7. Normal Wake 在统一 resident 中应该只是“一种 turn”

完成 UH-A0 后，目标形态为：

```text
Chat turn
  ↓
[resident 保持 alive]
  ↓
normal Wake turn
  ↓
[resident 继续保持 alive；system/tool schema 不变]
  ↓
Chat turn
```

Wake 特有的信息必须放在动态输入，不得改 stable system。

可包含：

- `turn_kind=normal_wake`；
- V3 授权给 Wake Planner 的结构化状态/必要事实；
- Capability Skill / 当前 lease 的必要说明；
- 本次需要形成 Intent / Action 的任务 envelope。

不得为了 Wake 重发：

- 完整 persona；
- 已在 resident 中的聊天历史；
- 完整旧渐变脑快照；
- 另一份 Wake 专属大 system；
- 与本轮无关的环境/状态说明。

## 8. 与 Internal State V3 / Behavior Authority 的职责对齐

继续以上位合同为准：

- **Wake Planner**：决定想做什么；
- **Action Gate**：决定现实上能不能做；
- **Persona / Renderer**：决定怎么表达；
- **V3 Settlement**：行动完成后结算真实结果。

但这里新增一条成本约束：

> 上述 Wake 决策/表达若需要 Claude Code 参与，默认应在同一个 unified resident/session 中完成，不得为了架构“干净”再起第二只高成本 persona resident。

B1-1 已经建设的 PlannerStateView、CapabilitySkillView、Decision-time provenance 应复用；不得另外造第二套状态/Planner。

B1-2 的额外自然样本收集不是 Unified Heartbeat 的前置。Owner 若将 Unified 提升为当前主线，应停止为了 B1-2 扩 CASE/canary/监控。

## 9. Transcript 污染是次级约束，但不能用“再起一只 resident”解决

旧 Wake 输出合同：

```text
THOUGHTS: ...
ACTION: ...
CONTENT: ...
```

如果完整长期留在主 session，可能污染日常聊天。

这个问题要在**同 resident 内解决**，而不是通过独立 Wake resident 解决。

允许的方向包括：

- 缩短结构化 decision envelope；
- 让结构化 planner 输出尽量机器化、低语言污染；
- 将用户可见 message 与内部 decision 明确分层；
- 复用现有 Decision-time provenance / Settlement，不让完整心理报告进入普通 Chat prompt。

最终实现方式在 UH-A1 冻结；但有一条硬红线：

> 不得以“避免 transcript 污染”为理由恢复第二只昂贵 Wake resident 或每次 Wake respawn。

## 10. UH-A 第一版施工边界

### UH-A0-1｜统一工具面审计

回答：主聊天 + normal Wake 的固定 schema 最小并集/受控并集是什么。

不得顺手增加新工具产品。

### UH-A0-2｜Capability lease 最小实现

只解决一个问题：同一固定 schema 下，normal Wake 的外部副作用如何被确定性限制，而无需改 `allowedTools` / 重启进程。

### UH-A0-3｜缓存不变量验证

复用现有 usage / resident observability，不新建监控系统。

至少能证明：

- Chat → Wake → Chat 没有由 Wake 导致的 generation 重建；
- `respawn_reason` 不出现 Wake 自己造成的 `system_changed` / `tool_profile_changed`；
- `resident_turn_count` 在同一 resident 上连续增长；
- cache read/creation 数据能观察到 Wake 正在复用已有上下文，而不是重新冷装整套稳定上下文。

这里不先冻结绝对 token 阈值；先用真实同类序列对比当前独立 Wake 基线，足以支持决策即停。

### UH-A1｜normal Unified flag-off 接线

- feature flag 默认 OFF；
- 只迁移 `normal`；
- Chat/Wake 共用 `_CC_RESIDENT` 的同一热生命周期；
- Wake 只是特殊 turn；
- stable system / fixed tool schema 不切换；
- 成功路径不依赖 `_CC_WAKE_RESIDENT`。

### UH-A2｜最小用户可见连续性

- normal Wake message 正常落 `chat_messages`；
- 用户随后回复时同一 session 已知道自己刚才说过什么；
- Unified 成功路径不再需要 `wake_reply_bridge` 重复提醒。

### UH-B｜稳定后删除旧 normal 路

只有成本与连续性验收通过后，才删除 normal 独立 Wake 路。

morning / ritual / nightwatch 等仍单独迁移；dream / summarize 继续按其背景任务合同处理。

## 11. Feature flag 与失败策略

第一版必须默认关闭，例如：

```text
UNIFIED_NORMAL_WAKE_ENABLED=0
```

flag OFF：旧 normal Wake 完全不变。

flag ON 后：

- unified turn 开始前失败 → 可回旧 normal 路；
- unified turn 已产生并持久化用户可见 Wake message 后失败 → 不自动再生成第二条；
- capability lease 无法确定 → fail closed，不为了继续执行而临时切宽权限；
- 不得因为 fallback 永久把主 resident 改成 Wake tool profile/system。

## 12. 第一版测试只回答四个主线问题

不建新测试框架，不做 24h/7d 观察作为第一刀前置。

### Q1｜缓存/热 resident

Chat → normal Wake → Chat 是否保持同一 resident generation/session，且 Wake 不触发 system/tool respawn？

### Q2｜成本

与当前独立 `_CC_WAKE_RESIDENT` 基线相比，normal Wake 是否明显减少重复 stable context/cache creation？

使用现有 cache read/creation、context、resident_turn_count、respawn_reason 证据即可。

### Q3｜权限

同一固定工具 schema 下，normal Wake 的禁止副作用是否能由 capability lease / Action Gate 确定性阻断，而不改变进程 tool surface？

### Q4｜连续性

Wake 发出的消息后，用户自然回复时是否无需 `wake_reply_bridge` 也能承接？

核心正常路径 + 一个权限失败路径足够支持当前决策即停。

## 13. 直接否决项

出现任一项不得合并：

- 为 normal Wake 新起第二只 persona-bearing resident；
- 把“same session lineage”当成可频繁 respawn 的借口；
- normal Wake 每轮修改 `_allowed_tools` / MCP config / tool profile；
- normal Wake 每轮替换 stable system；
- normal Wake 正常路径主动 kill/resume 主 resident；
- 只靠 prompt 禁止 autonomous write tool；
- 为了 Unified 顺手搭新的 CI / runner / canary / 监控基础设施；
- 为了 normal 一次迁移 morning/nightwatch/ritual/dream/summarize；
- 为解决 transcript 污染重新恢复第二只高成本 Wake resident；
- 成功路径仍靠 `wake_reply_bridge` 告诉聊天模型“你刚才说过什么”。

## 14. 当前结论

静态代码与最新版项目地图已经足够得出主线决策：

```text
PASS（设计决策）
```

已确认：

1. 当前独立 Wake resident 与动态 tool/system 切换正是共享缓存目标的结构性阻碍；
2. 旧地图已经把 **固定 tool schema + capability lease** 明确列为 Unified Heartbeat 前置；
3. 因此无需继续 LIVE 诊断，也不需要建设新测试设施。

下一步主线不是直接合并 resident，也不是 text-only resume，而是：

> **UH-A0-1 / UH-A0-2：先把统一 resident 的固定工具面与 capability lease 做出来，使 Chat/Wake 不再通过切 tool/system 来区分权限。**

完成这个最小前置后，立即进入 `normal` 同热 resident 接线与缓存验收。
