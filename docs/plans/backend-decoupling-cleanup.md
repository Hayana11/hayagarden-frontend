# Backend Decoupling & Cleanup Plan

> 状态：**PARKED / 暂不施工**  
> 目的：在不改变现有产品行为的前提下，逐步降低后端耦合、删除已退役路径、缩小故障爆炸半径。  
> 原则：**先测量，后拆线；一次只动一个领域；新路切稳后必须删除旧路。**

---

## 1. 为什么要做

当前后端仍处于持续生长期：修复身份、Wake、Internal State、Provider、工作台等问题时，往往需要在多个入口和多条生成路径同步修改。真正的问题不是总代码行数本身，而是：

- 一个业务动作分散在多处完成；
- `app.py` / `gateway.py` 同时承担路由、数据库、生命周期、模型、后台任务与业务编排；
- 模块导入时存在 schema 初始化、预热线程等副作用；
- 多个路径直接访问同一数据库表并猜测彼此状态；
- 旧路径迁移完成后没有及时退役；
- 关键链路存在宽泛 `except Exception: pass`，错误常在下游才显现；
- 修复常通过“再包一层”完成，导致功能增长快于旧代码删除。

本计划的目标不是追求最少行数，而是把系统从“蜘蛛网”整理成“带标签的插头”：改动影响面明确、可测试、可回滚。

---

## 2. 何时开始

### 2.1 当前只存档，不插队

在以下工作完成前，本计划不得进入实现阶段：

- [ ] P-SHADOW：Internal State v3 当前 72 小时 Shadow 验收毕业；
- [ ] P-0：Heartbeat Measurement Foundation 部署并验收；
- [ ] Unified Heartbeat 当前路线完成其既定安全阶段，至少 normal 新旧路径切换稳定并完成旧 normal 路由退役；
- [ ] 当前生产无未解释的 proof gap、capture alert、重复可见消息或 provider/resident 异常。

### 2.2 允许提前做的只有

- 更新本文件；
- 记录发现的耦合点、死代码候选和删除条件；
- 不改变生产行为的只读普查。

不得以“顺手整理”为理由混入 PR 0、Shadow、Unified Heartbeat 或 Internal State PR。

---

## 3. 成功标准

整理完成后，应满足：

1. **一件事只有一个权威入口。**
   - Chat 回合身份、持久化与收尾只有一个服务负责；
   - Wake 编号、门禁、执行与 outcome 只有一个服务负责；
   - Internal State 只消费明确事件，不反向猜测 Chat/Wake 内部状态。

2. **依赖单向。**

   ```text
   Route / CLI / Scheduler
          ↓
   Application Service
          ↓
   Domain Service
          ↓
   Store / Provider Adapter
   ```

3. **跨领域只通过明确合同通信。**
   - `chat_turn_completed`
   - `wake_completed`
   - `provider_turn_completed`
   - 稳定 ID、明确字段、可重复处理。

4. **入口文件退化为接线板。**
   - `app.py` 只负责创建 Flask app、注册 blueprint、注入依赖和启动协调；
   - `gateway.py` 只负责 HTTP/SSE 适配、Provider/Resident 接线与少量协调；
   - 新业务不得继续塞回巨型入口。

5. **迁移后删旧路。**
   - 不永久保留“双轨保险”；
   - 每条兼容路径必须写明删除条件、负责人和阶段。

6. **故障局部化。**
   - Chat edit/redo 失败不得污染 Wake；
   - Wake 失败不得破坏普通 Chat；
   - Internal State fail-closed 只阻止对应结算，不静默改变旧权威；
   - Provider 切换不得改变业务事件身份。

---

## 4. 非目标

本计划不做：

- 不重写整个后端；
- 不更换 Flask / SQLite；
- 不重做前端 UI；
- 不修改 Persona、记忆语义或 Relationship Context；
- 不在同一 PR 内重构并新增大型功能；
- 不在无行为测试保护时移动关键链路；
- 不通过删除测试、告警或审计代码来制造“负行数”；
- 不以总代码行数为唯一质量指标。

---

## 5. 施工规则

### 5.1 每个 PR 必须声明

```text
允许影响：
禁止影响：
权威入口：
旧入口删除项：
数据库变化：
回滚方式：
验证命令：
```

### 5.2 一次只拆一个边界

禁止同车：

- Chat 回合重构 + Wake 重构；
- Provider 重构 + Internal State 算法变化；
- 路由抽离 + 数据库 schema 大迁移；
- 清理死代码 + 新增用户功能。

### 5.3 迁移步骤固定

```text
现状行为测试
→ 抽出新边界
→ 新旧结果对照
→ 切换权威
→ 观察
→ 删除旧路径
```

没有“删除旧路径”的迁移不算完成。

### 5.4 生产巨型文件行数棘轮

Phase C0 确认基线后：

- `app.py` 不得净增长；
- `gateway.py` 不得净增长；
- 新业务必须进入领域模块；
- tests、审计和独立模块可合理增长；
- 禁止新增 `*.bak`、`*.bak2`、带日期的源码副本。

---

## 6. 阶段总览

- [ ] **C0：只读普查与基线**
- [ ] **C1：架构护栏与无争议删除**
- [ ] **C2：`app.py` 路由领域化**
- [ ] **C3：Store / Service 边界**
- [ ] **C4：统一 Chat Turn 收尾**
- [ ] **C5：统一 Gateway / Provider 适配边界**
- [ ] **C6：Unified Heartbeat 后续旧 Wake 清场**
- [ ] **C7：异常与副作用治理**
- [ ] **C8：最终删除、文档与验收**

---

## 7. C0：只读普查与基线

目标：先知道缠在哪里，不急着剪。

### 7.1 统计

- `app.py` / `gateway.py` 当前行数；
- 路由数量与所属领域；
- 直接 `sqlite3` 调用位置；
- 模块 import 时执行的副作用；
- `except Exception: pass` 和宽泛吞错位置；
- 重复的 sys.path 修改、常量、DB helper；
- 同一业务动作的多个 persist/finalize 路径；
- `.bak`、旧脚本、旧路由、无调用函数候选；
- Chat、Wake、Internal State、Provider、Workspace 的依赖图；
- 定时任务、systemd、cron、HTTP route 与后台线程入口图。

### 7.2 输出

- `docs/architecture/backend-dependency-map.md`
- `docs/architecture/backend-cleanup-baseline.json`
- 每个候选项标注：
  - `safe_delete`
  - `needs_characterization_test`
  - `blocked_by_unified_heartbeat`
  - `blocked_by_internal_state`
  - `keep`

### 7.3 验收

- 只读，不修改生产代码；
- 可重复运行；
- 同一 commit 输出稳定；
- PR 0 指纹与 usage 口径可用于后续重构前后对比。

---

## 8. C1：架构护栏与无争议删除

### 8.1 先加护栏

- CI 禁止新增源码备份文件；
- CI 记录并限制 `app.py` / `gateway.py` 基线增长；
- 可选 import-boundary 检查：
  - routes 不直接 import provider 实现；
  - stores 不 import routes；
  - Internal State 不 import前端流程；
- 新增模块必须有领域归属。

### 8.2 无争议删除

优先删除：

- 已被 Git 历史替代的 `app.py.bak*` 等源码副本；
- 重复 sys.path 插入；
- 已无调用者的脚本/函数；
- 已退役且生产返回 404 的旧路由残留；
- 失效注释、重复常量与不可达分支。

### 8.3 验收

- 生产行为零变化；
- 全量 CI 通过；
- `git diff --check`；
- 服务重启与 smoke test 通过；
- 本阶段 PR 应以净删除为主。

---

## 9. C2：`app.py` 路由领域化

目标：只移动边界，不重写业务。

候选 blueprint：

```text
routes/chat.py
routes/ledger.py
routes/workspace.py
routes/brain.py
routes/self_triggers.py
routes/files.py
```

### 9.1 要求

- HTTP 路径、状态码、响应 JSON 保持不变；
- 数据库写入顺序保持不变；
- route 只做参数解析、权限、service 调用和响应映射；
- schema 初始化移入明确 bootstrap/migration；
- import app 不应隐式修改业务数据。

### 9.2 每次只搬一个领域

例如：

```text
C2-LEDGER
C2-WORKSPACE
C2-BRAIN-READ
C2-SELF-TRIGGER
C2-CHAT-BRANCHES
```

Chat 最后搬，因为风险最高。

### 9.3 验收

- 旧 API contract 测试通过；
- 同一输入的新旧 DB delta 一致；
- 不引入循环依赖；
- 每个子 PR 合并后立即删除 app.py 中对应实现。

---

## 10. C3：Store / Service 边界

目标：route 不再随处直接写数据库。

候选结构：

```text
chat/chat_store.py
chat/chat_turn_service.py
wake/wake_store.py
wake/heartbeat_service.py
workspace/workspace_service.py
brain/brain_query_service.py
```

### 10.1 Store 负责

- SQL；
- transaction；
- 行到领域对象的转换；
- 不决定业务流程；
- 不调用模型；
- 不 import Flask。

### 10.2 Service 负责

- 业务状态机；
- 幂等与身份；
- 事务边界；
- 调用 store / provider adapter；
- 发出明确领域事件。

### 10.3 验收

- routes 中直接 sqlite 调用持续下降；
- 同一数据表写入有明确 owner；
- 没有 Store ↔ Route 反向 import；
- DB 行为用 transaction-level tests 固定。

---

## 11. C4：统一 Chat Turn 收尾

这是最高风险阶段之一，必须在 PR 0 与 Shadow/Unified Heartbeat 稳定后进行。

### 11.1 当前要解决的问题

不同 provider / stream / persist 路径各自完成部分收尾，修复身份或评分时需要多处同步修改。

### 11.2 目标

所有模型完成后，统一进入一个收尾器：

```text
NormalizedProviderResult
→ persist assistant message
→ emit chat_turn_completed
→ trigger scoring
→ enqueue Internal State events
→ release chat lock
→ record usage / diagnostics
```

### 11.3 合同示例

```json
{
  "event": "chat_turn_completed",
  "identity_id": "fyodor-default",
  "conversation_id": "default",
  "user_message_id": 4208,
  "assistant_message_id": 4209,
  "provider_id": "claude_code",
  "provider_request_id": "..."
}
```

Provider 只负责生成 `NormalizedProviderResult`，不得自行复制业务收尾。

### 11.4 验收

- 正常 send / redo / edit / interrupted retry 全覆盖；
- 每个完整回合最多一次评分模型调用；
- `user_rule` / `user_scored` 身份一致；
- assistant 持久化 exactly-once；
- provider 切换不改变 message identity；
- 删除原三处或更多重复 finalize 路径。

---

## 12. C5：统一 Gateway / Provider 适配边界

目标：gateway 不再同时承担业务与 provider 细节。

### 12.1 Adapter 统一输出

```text
ProviderAdapter.send_turn()
→ normalized events
→ normalized final result
→ normalized usage
```

Claude Code、Codex、Relay、未来 Chimera 脑路由都遵循同一业务合同，但保留各自协议实现。

### 12.2 Gateway 只保留

- HTTP / SSE；
- auth / request validation；
- adapter selection；
- cancellation / timeout；
- normalized event 转发；
- 少量 lifecycle 协调。

### 12.3 验收

- system/tools/model/thinking 指纹在重构前后可解释；
- requestId 去重稳定；
- resident generation 与 provider session 不混淆；
- 冷启动、重生、自然 cache 失效分类不退化；
- gateway 中不再复制 Chat/Wake 业务规则。

---

## 13. C6：Unified Heartbeat 后续旧 Wake 清场

本阶段只能在 Unified Heartbeat 已完成对应模式迁移后执行。

### 13.1 删除对象

按实际迁移状态逐项删除：

- 旧 normal probability runner；
- 旧 normal 独立 CC Wake resident 路径；
- 重复 Wake prompt 装配；
- 已迁移模式的旧 bridge；
- 无使用者的 provider 分支；
- 临时兼容 feature flag。

morning / ritual / nightwatch / self-trigger 未迁移前，不得误删其仍在使用的路径。

### 13.2 最终目标

```text
Scheduler / Trigger
→ Fyodor Heartbeat Service
→ Provider Heartbeat Adapter
→ Wake Outcome / Visible Message Outbox
```

### 13.3 验收

- 每个逻辑 Wake 一个稳定 `wake_run_id`；
- 可见消息 exactly-once；
- Chat 优先与 preemption 不退化；
- 旧路径删除后仍有 24h 安全检查与 7d 稳定性证据。

---

## 14. C7：异常与副作用治理

### 14.1 宽泛吞错分类

每个 `except Exception` 必须归类：

- `best_effort_noncritical`：允许继续，但必须有结构化日志/指标；
- `fail_closed`：阻止结算并留下 incident；
- `retryable`：进入明确 outbox/backoff；
- `fatal`：终止当前请求或 resident；
- `legacy_unknown`：必须调查后归类。

关键链路禁止裸 `pass`。

### 14.2 Import 副作用治理

逐步迁移：

```text
import module
```

不应自动：

- 改 schema；
- 启动线程；
- 写业务表；
- 执行外部调用。

统一由：

```text
create_app()
bootstrap_schema()
start_runtime_services()
```

显式调用。

### 14.3 验收

- 关键失败有 requestId / message_id / wake_run_id；
- 没有“上游静默失败、下游才爆炸”的不可追踪路径；
- 重启不会重复启动后台线程或重复迁移业务数据。

---

## 15. C8：最终删除与验收

### 15.1 最终检查

- [ ] 所有迁移项旧路径已删除；
- [ ] 无 `.bak*` 源码副本；
- [ ] `app.py` / `gateway.py` 只剩接线职责；
- [ ] Chat finalize 只有一个权威入口；
- [ ] Wake outcome 只有一个权威入口；
- [ ] Internal State 不猜测缺失身份；
- [ ] routes / services / stores 依赖单向；
- [ ] 关键链路无裸吞错；
- [ ] 架构文档与部署文档同步。

### 15.2 生产验收

- API contract 回归；
- DB delta 回归；
- Chat send/redo/edit/interrupt；
- normal/morning/nightwatch/self-trigger/ritual；
- Provider cold/hot/restart/cancel；
- Shadow / Internal State 健康；
- systemd restart；
- cron；
- 24h 安全观察；
- 7d 错误率、成本、重复事件与延迟对比。

### 15.3 完成定义

不是“文件拆完”，而是：

```text
同一功能只有一个权威实现
＋ 旧实现已删除
＋ 影响范围有合同测试
＋ 重构前后生产指标可解释
```

---

## 16. 回滚策略

- 每个领域单独 PR；
- 行为变更与代码搬迁分开；
- 高风险切换使用短期 feature flag；
- flag 必须附删除日期/阶段；
- 数据库 schema 尽量 additive-first；
- 新路径异常立即切回旧权威，但保留诊断证据；
- 回滚不得删除 incident、outbox 或审计记录。

---

## 17. 当前决定

> **本计划现在只存档，不启动施工。**
>
> 当前优先级仍是：完成 P-SHADOW、PR 0 与 Unified Heartbeat 既定路线。当前工程稳定并完成旧路径退役后，再从 C0 只读普查开始，不允许一上来大拆 `app.py` / `gateway.py`。

届时第一步不是挥锤拆墙，而是给每一根线贴标签。
