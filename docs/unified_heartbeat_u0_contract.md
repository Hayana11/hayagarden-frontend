# Unified Heartbeat U0｜Wake × 主聊天统一合同

> 状态：docs-only 合同冻结候选；不接生产、不改 flag、不 merge/deploy。
> 基线：`main@4b82f2fb6921f9298d0498449c3db17814b2057e`。

## 1. 产品目标

普通 `normal Wake` 与主聊天必须属于同一个 Claude 连续身份，不再依赖“独立 Wake resident 生成一句话，再用 wake_reply_bridge 告诉聊天 resident 它刚才说过什么”的长期架构。

本阶段首先解决用户可见的连续性：

- normal Wake 发出的消息由主聊天的 Claude session lineage 生成；
- 用户随后回复时，同一条 Claude session lineage 已经知道自己刚才发过什么；
- 不重复注入 Wake 文本来伪造记忆；
- 不让 Wake 的结构化 `THOUGHTS/ACTION`、工具说明或后台心理报告污染日常聊天语气。

**重要修订：**“统一”冻结为 **同一 Claude session lineage / 同一 Persona-bearing conversational continuity**，不再把“永远复用同一个 OS subprocess”当成产品不变量。进程是否热复用是实现细节；只要为了工具安全或 text-only Renderer 需要安全 respawn/resume，可以换进程，但不能换成另一条独立记忆链。

## 2. 当前实现事实

当前生产代码明确存在两只不同 resident：

- 主聊天：`_CC_RESIDENT`；
- Wake：`_CC_WAKE_RESIDENT`。

`ClaudeCodeWakeRunner` 也明确按“独立 Wake resident”设计，并会：

1. 根据本次 Wake 的 prepared tools 重算 `allowed_tools`；
2. 若 allowlist 与 resident 当前值不同，直接改 `_allowed_tools`；
3. 把 `_system_text` 置空以强制下一次 `ensure_alive()` respawn；
4. 使用 Wake 专属 system + `WAKE_CONTRACT`；
5. 把结构化 `THOUGHTS/ACTION/CONTENT` 写进 Wake 自己的 transcript。

而 `ResidentSession` 对以下变化都会 respawn：

- `system_text` 改变；
- tool profile 改变；
- model 改变；
- context/turn limit 等既有条件。

因此，**禁止**把现有 `ClaudeCodeWakeRunner(_CC_WAKE_RESIDENT, ...)` 机械改成 `ClaudeCodeWakeRunner(_CC_RESIDENT, ...)`。

## 3. 为什么“直接共用主 resident”被否决

### 3.1 System 冲突

Wake 当前使用 Wake 专属 system；主聊天 resident 使用聊天 system。

若同一热 resident 在两者之间切 system，`ResidentSession` 会判定 `system_changed` 并 respawn。于是“共用对象”并不等于“共享连续热 resident”。

### 3.2 Tool surface 冲突

Wake 的 Claude Code 工具合同刻意比主聊天窄：

- Wake 明确禁止裸 `mcp__codebase`，因为它会暴露 `patch/create_file`；
- Wake 只允许受控 home + codebase read-only 子工具；
- 主聊天当前固定 `CC_ALLOWED_TOOLS` 包含裸 `mcp__codebase` 及更宽工具面。

CLI `--allowedTools/--disallowedTools` 是进程启动级配置；当前 stream-json 用户消息没有已验证的 per-turn allowlist 合同。

所以仅靠 prompt 写“这轮不要调用工具”不能充当确定性权限门。

### 3.3 Transcript 污染

旧 Wake runner 的输出合同是：

```text
THOUGHTS: ...
ACTION: ...
CONTENT: ...
```

如果直接把这套内部决策 transcript 塞进主聊天连续会话，后续私聊会看到后台决策格式、工具说明和自我分析，重新制造已经明确要避免的“导演腔 / 心理报告污染”。

## 4. 与 Internal State V3 / Behavior Authority 的新职责对齐

以现有上位合同为准：

- **Wake Planner**：决定想做什么（Intent / Action candidate）；
- **Action Gate**：决定现实上能不能做；
- **Persona / Renderer**：只有需要语言时决定怎么说；
- **V3 Settlement**：做完以后结算真实结果。

因此 Unified Heartbeat 不再要求“主聊天 resident 同时充当后台 Planner + 工具执行器”。

冻结的新边界：

> Planner 可以是结构化后台机制；Persona-bearing 自然语言 Renderer 必须进入主聊天 Claude session lineage。

这样不会再产生“第二个会说话的费佳”。

## 5. 第一刀只做 normal，不碰其他 Wake 模式

U0/U1 只讨论 `mode=normal`。

以下保持原路径：

- `morning`
- `nightwatch`
- `ritual`
- `self_trigger`
- `dream`
- `summarize`

它们不能因为 normal 统一而顺手迁移。

## 6. Normal Unified 最终运行链

第一版目标链：

```text
Wake trigger
  → authoritative V3 PlannerStateView / Decision-time facts
  → Planner 得到结构化 Intent / Action candidate
  → Action Gate
  → 若 action 不需要语言：按既有 executor / Settlement 完成
  → 若 action=message：进入 Unified Renderer
  → Unified Renderer 在主聊天 Claude session lineage 上生成自然语言
  → assistant message 正常落库
  → V3 Settlement
  → 下一次用户聊天继续同一 session lineage
```

关键点：

- 主 session 里只留下最小 Renderer turn 与自然语言结果；
- 不把完整 Drives / Affect / Thought Pool / `THOUGHTS/ACTION` 塞进主 transcript；
- 不通过 `wake_reply_bridge` 再告诉模型“你刚才说过这句”；
- Wake message 成功后，聊天连续性来自 session 本身，不来自桥接提示。

## 7. Renderer 的工具安全

在没有 per-turn capability enforcement 之前，Unified Renderer **不得**直接在主聊天广工具热进程上运行 Wake turn。

第一版允许的安全实现是：

1. 保存当前主聊天 session id / lineage；
2. 在全局 generation/rewrite 序列化边界内结束当前热进程；
3. 用 **text-only** 工具 profile resume 同一个 Claude session；
4. 使用与聊天 Persona 一致的稳定 system，不切成旧 Wake 专属 system；
5. 只发送极小 Renderer envelope；
6. 得到自然语言结果并让该 turn 写入同一 session transcript；
7. 后续主聊天需要广工具时，再以正常聊天 profile resume 同一 session lineage。

这意味着进程可以换，但“脑子的日记本”不能换。

若后续证明 Claude Code 提供可靠、可回归的 per-turn permission/capability contract，可以再优化成单热进程；**该优化不是第一版前置条件**。

## 8. Renderer envelope 约束

Renderer 只能拿：

- 当前 Persona（由已有聊天 stable system 持有，不重复长注入）；
- 已冻结的 `Intent`；
- 已通过 Gate 的 `Action=message`；
- `content_target` / 必要事实；
- 最多少量与这条消息直接有关的连续事实。

Renderer 不得拿：

- 完整 Drives；
- 完整 Affect / Thought / Fixation；
- `THOUGHTS:` 决策链；
- 工具 brochure；
- “你要更霸道/更冷/更主动”等导演指令。

第一版输出只接受自然语言正文，不接受新的 Action 决策。

## 9. 与 Behavior Authority 当前进度的关系

现有 B1-1 已经具备：

- 同一 Decision-time PlannerStateView；
- CapabilitySkillView；
- Planner Shadow；
- per-attempt pairing；
- Decision-time provenance。

B1-2 的自然观察不是 Unified Heartbeat 的永久产品组件。

若 Owner 将 Unified Heartbeat 提升为当前主线，允许停止继续扩张 B1-2 收样本；后续只保留实现 Unified 所需的最小 Behavior 能力：

1. 最小 Action Gate；
2. message 路径 Decision-time provenance；
3. Renderer + Settlement。

不得为了“先把 B1-2 做得更漂亮”新增 CASE、canary、监控或隔离设施。

## 10. UH-A 第一版施工边界

### UH-A1｜normal message-only 接线

只允许：

- `normal`；
- `Action=none`；
- `Action=message`。

`diary/explore` 暂不通过 Unified Renderer 接管。

### UH-A2｜text-only same-session Renderer

新增最小 session handoff/resume 能力，让 Renderer turn 写入主聊天 session lineage。

### UH-A3｜旧 bridge 只在 unified flag-off / fallback 使用

Unified 成功路径不得再依赖 `wake_reply_bridge`。

flag-off 时旧路径完全不变。

### UH-A4｜成功后再删旧 normal 路

只有 normal Unified 的自然用户回复连续性通过后，才允许删除 normal 独立 Wake resident 路由。

其他 Wake 模式仍保留旧 resident，直到各自单独迁移。

## 11. Feature flag 与失败策略

第一版必须默认关闭，例如：

```text
UNIFIED_NORMAL_WAKE_ENABLED=0
```

失败策略：

- Unified Renderer 在写出任何 Wake 用户可见消息前失败 → 回到旧 normal Wake 路，不污染主 session；
- Unified Renderer 已经产生并持久化用户可见消息后失败 → 不再自动生成第二份回答；记录失败，等待后续正常聊天恢复；
- 任何失败不得重复插入同一条 assistant Wake message。

## 12. 第一版验收只回答四件事

不建新的测试体系。只需要足够支持当前决策的证据：

1. `normal message` 由主聊天 session lineage 生成；
2. 用户随后回复时，不需要 wake_reply_bridge 也能自然承接刚才的 Wake message；
3. Renderer turn 不执行任何工具，且不会把 `THOUGHTS/ACTION` 带进普通聊天；
4. flag-off / 失败时旧 normal Wake 仍可安全工作且不重复消息。

达到以上证据即停。

## 13. 直接否决项

出现任一项不得合并：

- 直接把 `_CC_WAKE_RESIDENT` 替换成 `_CC_RESIDENT`；
- 每次 Wake 改主 resident `_allowed_tools`；
- 每次 Wake 把主 resident `_system_text` 清空/替换成 Wake system；
- 让 autonomous Wake 在主聊天广工具面上仅靠 prompt 自觉不调用工具；
- 把完整 `THOUGHTS/ACTION/CONTENT` 作为主聊天长期 transcript；
- 为了 normal 顺手迁移 morning/nightwatch/ritual/self_trigger/dream/summarize；
- Unified 成功路径仍靠 `wake_reply_bridge` 伪造“我刚才说过”；
- Renderer 已输出后自动 fallback 再生成第二条不同内容。

## 14. 当前结论

静态代码审计已经足够回答“能不能直接复用当前主 resident”这一问题：

```text
BLOCKED_BEFORE_TEST
```

阻塞不是环境问题，也不是运行时功能失败，而是当前代码合同明确不兼容：system 与 tool surface 都会迫使旧 WakeRunner 改写/重启 resident。

因此不跑 LIVE，不再扩张诊断。

下一步主线是按本合同做 **UH-A1：normal / none+message 的最小接线准备**，并复用 Behavior Authority 已有的 Decision-time V/C/provenance，不另造第二套 Planner/Action 系统。
