# 主动轮冷启动：wake 回执与热证明设计

状态：设计定稿（2026-11-16，哈娅批准 (a) 方案、2a/2b 拆分、stdin 前临时 binding）。
上游：`docs/proactive-cold-start-design.md`（其中 self_trigger 退休、每日上限、S1 shadow 三处已作废，以本文为准）。
已落地：`85b1da4`（history anchor 与可空 `user_message_id` 拆分）、`e623361`（历史排除跟身份走、截止跟 anchor 走）。

预算：2a ≤ 350 行生产代码，2b ≤ 350 行；项目生产代码总量 ≤ 800（已用 85）；测试总量 ≤ 600。超任一即停。

---

## 0. 术语

| 名称 | 含义 |
|---|---|
| H | history anchor：主动轮 prepare 时 `SELECT MAX(id) FROM chat_messages`，只决定历史读到哪（含 H） |
| rid | `wake_run_id`，主动轮唯一身份 |
| A | 主动轮成功后 executor 落库的 canonical wake 行 id（assistant DB 锚点） |
| Chat 尾 | 回执 current_request = `message:U`，尾巴是 (U → A) 一问一答 |
| Wake 尾 | 回执 current_request = `wake_request:<rid>`，尾巴是 assistant-only 的 A |

`wake:<N>`（N 为消息 id）已经是历史 wake 行的 source_ref 写法，**不得复用**；主动轮请求身份一律用 `wake_request:<rid>`。

## 1. 身份：两种请求，一个分发点

`DailyTurnPlan` 增加三个字段：`turn_mode: str = 'chat'`、`wake_run_id: Optional[str] = None`、`request_content_sha256: str = ''`。

新增 `_request_identity(plan) -> tuple[str, str]`：
- Chat：`('message', 'message:%d' % user_message_id)`
- 主动轮：`('wake', 'wake_request:%s' % wake_run_id)`；`turn_mode != 'wake'` 或 rid 为空即抛 `request_identity_missing`。

提交一的 `_require_request_message_id` 保留给**只有 Chat 才合法**的点（一问一答配对、hidden flow lease、`MappingPassRequest`）；凡是“两种请求都合法、只是形态不同”的点改调 `_request_identity` 分发。**不按 `turn_kind` 分支，只按请求身份分支。**

## 2. 2a：主动轮冷启动入口与路由（≤ 350 行）

### 2.1 `prepare_proactive_cold_turn`
把 `prepare_daily_turn` 的主体抽成私有 `_prepare_turn_core(request, ...)`，Chat 与主动轮各一个薄入口，**不复制 270 行**。差异只有：

| 步骤 | Chat | 主动轮 |
|---|---|---|
| 取请求 | `_fetch_user_message(U)` | 触发文本 + `now` |
| origin_day / turn_started_at | 消息时间 | `now` |
| history anchor | U | H（含 H） |
| lease | `turn_mode='chat'`，owner=req_id | `turn_mode='wake'`，`turn_id='uh-a1-cold:'+rid`，owner=`'wake:'+rid` |
| `claim_daily_resident_turn(request_message_id=)` | U | H（只是数字列；身份靠 owner/rid） |
| `record_daily_message_context(role='user')` | 记 | **不记** |
| memory surface | 照旧 | 跳过，manifest `memory_recall_mode='skipped_wake'` |
| hidden flow | 照旧 | `eligible=False` |
| `build_reality_context` 时间锚 | 消息行 | `now` |

manual window 的 `switch_in_progress` / `first-turn finalize pending` 两道门照旧，遇到即 pre-model 跳过。

### 2.2 ContextPlan 原生接受主动轮（生产 `CONTEXT_PLAN_CONSUMER_ENABLED=1`，禁止旧拼装）
- `build_daily_continuity_shadow_plan` 增加可选 `current_request` 值对象（`source_ref`、`source_revision=rid`、`content_hash=sha256(触发文本)`、`history_cut=H`）。给了它就不再用 `current_user_message_id` 去查 `daily_message_contexts`，scope 直接取 plan 的 context_id/epoch，历史读到 H（含）。
- `_validate_production_context_install`（current_request 校验与 Capacity 分支）按 `_request_identity` 分发：wake 身份要求恰好一段 `current_request`，`source_ref == wake_request:<rid>`，`content_hash == plan.request_content_sha256`。**不得出现 `message:<H>` 的 current_request。**
- `_conditionally_seal_cold_bridge`、`_project_context_plan_history`、`capacity_swap_runtime` 的“当前用户不得进入候选”断言：wake 身份下没有当前用户行，断言为空真，不读 `user_message_id`。
- `CONTEXT_PLAN_CONSUMER_ENABLED != '1'` 时主动轮冷启动不启用（路由层就不进入）。

### 2.3 reprepare 三条链
`reprepare_after_capacity_swap`、`reprepare_after_registered_session_change`、`reprepare_after_hot_cold_mismatch` 统一改调 `_reprepare_same_request(plan, **kw)`：按 `plan.turn_mode` 回到 `prepare_daily_turn` 或 `prepare_proactive_cold_turn`，带上 rid、触发文本、H。

### 2.4 gateway 路由（只在边界判断一次）
`_run_unified_normal_main_chat_turn` 的 `RESIDENT_CHECK_1` 失败处：

```
if _proactive_cold_start_allowed(mode, reason):
    return _run_proactive_cold_wake_turn(...)
raise UnifiedNormalWakeSharedUnavailable(reason)   # 原行为
```

`_proactive_cold_start_allowed` 三条全满足才放行：
1. `mode ∈ config PROACTIVE_COLD_START_MODES ∩ {normal, morning, nightwatch, self_trigger}`（默认空 = 关）；
2. `reason ∈ {no_local_binding, resident_not_hot}` 或以 `resident_stale:` 开头；
3. `CONTEXT_PLAN_CONSUMER_ENABLED == '1'`。

结构性异常（`context_*`、`*_generation_mismatch`、`owner_*`、tool_profile 不匹配）仍跳过。`RESIDENT_CHECK_2` 与流内的 guarded probe 是热路径专属，冷路径不调用。

`_run_proactive_cold_wake_turn`：持 `_gen_acquire_or_wait(wait_timeout=0)`（拿不到 → `generation_lock_unavailable` 跳过）→ 用 Chat 路由同一套 `build_cc_daily_static_parts` / one-shot / display thinking → `prepare_proactive_cold_turn` → `stream_daily_resident_turn`（内部即 `ensure_resident_and_stream`）。事件累积循环从热路径抽成共用 helper，冷热两路共用，返回值与热路径同形（`text/silent/thinking/display_segments/tool_calls/cache_info/native_transcript_binding/_shared_delivery_fence`）。`cache_info` 额外记 `proactive_cold_start=true`、`history_anchor_message_id=H`、`wake_request_ref`。

**禁止**走 `_cc_resident_stream_gen` 的冷分支（旧拼装）。

2a 合入后到 2b 合入前，开关必须保持关闭。

## 3. stdin 前临时 binding（哈娅批准）

`ensure_resident_and_stream` 在 stdin 前写 binding/owner，主动轮照旧复用，不另造。失败处理：
- **flush 前失败**：复用 Chat 冷启动未发送失败的现有清理（`is_unsent_daily_turn` 一族）：关掉本轮拉起的进程、`set_local_binding(None)`、释放 lease、不推进 cursor、不写回执，回到冷启动前状态。抛 `UnifiedProactivePreModelFailed`（self_trigger 的 release 语义沿用 #622）。
- **flush 后失败**：按现有 post-flush 规则 kill resident、清 binding、不写回执、不推进 cursor；她下一句照常冷启动。

## 4. 2b：assistant-only 收尾（≤ 350 行，与第 5、6 节合计）

### 4.1 非静默
在 executor 现有事务内（`wake/executor.py` 已写 A 并调用 `commit_shared_wake_native_binding`）：
1. native binding：watermark 由 plan 构造（起点 = `plan.transcript_start_offset`，身份取自 plan 的 context/epoch/generation/process_generation/session/transcript_path），先核对 `plan_shared_wake_native_event_mappings` 对“冷启动上下文 + 触发 + 回复”判定为**单轮**；不是单轮就停工报告，不要放宽它。
2. `daily_message_contexts(A, role='assistant')` 记到本 context/epoch/generation（若 executor 已记则只断言）。
3. resident 游标 CAS：`cursor_before → A`，复用 Chat 收尾的游标 CAS 函数；owner 游标同步；事务提交后 binding 的 `bound_cursor_message_id = A`。

事务提交后：
4. `note_same_context_last_good(transcript_end_offset=E)`，E = binding 的 end_offset。
5. 填 `plan.manifest`：`transcript_mapping_status='MAPPED'`、`cursor_after=A`、`cursor_cas_success=True`、`assistant_message_id=A`、`context_receipt_last_good_proven`；然后**原样调用** `_commit_production_context_receipt(plan, assistant_message_id=A)`。members 来自 `_context_receipt_members(plan)`，其中 current_request 成员即 `wake_request:<rid>`。**不复制 finalizer。**
6. 回执写失败 → `REPAIR_REQUIRED`（现有语义），下一句 Chat 自然 RESPAWN，不另设回滚。

### 4.2 静默
写 `wake_log` 并 `commit_shared_transcript_watermark` 记录 skip（审计用）；没有 A 就没有 assistant 锚点，热证明无法成立 → **退休 resident**（`retire_shared_resident_after_failed_internal_turn` 同款），不写回执。热回合静默 Wake 不受影响。

## 5. 2b：Wake 尾热证明

`_hot_native_tail_proof` 开头按回执 current_request 成员的 source_ref 前缀分发：`message:` 走现有 Chat 证明（一行不改）；`wake_request:` 走新 `_hot_wake_tail_proof`；其它前缀 → `ambiguous`。

`_hot_wake_tail_proof` 运行在 `_hot_live_identity_decision` 已 PASS 之后。后者已经核对 epoch token、binding、process generation、session、model、provider、**系统指纹 sha256**、persona、registry、owner、回执 identity，以及**binding/owner/DB 三处 cursor == 回执 watermark**，Wake 尾沿用，不重写。新函数在此之上逐条核对，任何一条不满足即 `missing`（→ RESPAWN）或 `ambiguous`（→ BLOCKED）：

| # | 核对 | 失败 reason |
|---|---|---|
| a | 回执恰好一个 current_request 成员，`wake_request:<rid>`，rid 非空 | `wake_request_proof_missing` / `_ambiguous` |
| b | W = 回执 `installed_source_watermark` > 0，且 W < 本轮 `_history_anchor(plan)` | `wake_tail_is_not_previous` |
| c | **assistant DB 锚点**：`chat_messages` 行 W 存在，cache_info 为 `source=wake`、`wake_run_id=rid`、`unified_chat_resident=true`、`canonical_chat_history=true`、`proactive_cold_start=true`；`daily_message_contexts(W)` 的 context/epoch/generation 与 plan 一致、role=assistant | `wake_tail_anchor_mismatch` |
| d | `wake_log` 有 rid 行，context/epoch/generation 一致 | `wake_tail_log_missing` |
| e | 本 context/epoch/generation 内 (H, W) 区间**没有**其它消息行（H 取自 W 的 cache_info） | `wake_tail_foreign_rows` |
| f | **native binding**：`chat_message_claude_events` 中 message_id=W 至少一条 provider_input、至少一条 assistant；全部 session == registry session，context/epoch/generation 一致，offset 为整数、uuid 唯一、按 offset 有序，provider_input < 全部 assistant，末个 assistant offset < E | `wake_tail_mapping_*` |
| g | last_good 的 context/epoch/generation/session 与 registry 一致，E = last_good_end > 0；registry `scan_offset == E`，或由 `_hot_wake_skip_chain_end(E)` 精确桥接 | `native_tail_offset_mismatch`（沿用） |
| h | 尾巴成员：用现有 continuity 派生得到 W 对应的历史 wake 成员（`wake:<W>`），恰好一个 | `wake_tail_member_*` |

返回与 Chat 证明同形：`{'status':'pass','source_member':…,'assistant_message_id':W,'transcript_end_offset':E,'wake_skip_count':…}`。之后 `_hot_canonical_source_scope` 与 `_hot_historical_compatibility` **不改**：尾巴必须是 desired plan 最后一个、raw 表示。

## 6. 2b：已有缺陷——非静默热 Wake 之后下一句也在 RESPAWN（代码推断，生产待验证）

热回合非静默 Wake 用 `commit_shared_wake_native_binding` 把 registry `scan_offset` 推过 last_good_end，但 `wake_log` 只记 `transcript_skip`（静默）与 `observation_transcript_skip`，不记 native binding 区间。于是 `_hot_wake_skip_chain_end` 接不上，下一句 Chat 判 `native_tail_offset_mismatch` → RESPAWN。

修法（≤ 30 行）：executor 在非静默热 Wake 成功时把 binding 的 `{context_id, context_epoch, resident_generation, start_offset, end_offset, message_id}` 写进 cache_info 的 `native_transcript_range`；`_hot_wake_skip_chain_end` 把它当作链上一段，前提是 `chat_message_claude_events` 里该 message_id 的映射存在且 offset 落在区间内。先写一个红测试证明现状会 RESPAWN，再修。

## 7. 验收（自动化，fake resident 驱动）

1. 开关关：冷 resident 仍 `NORMAL_WAKE_SHARED_UNAVAILABLE_SKIP`；提交一 golden 不变。
2. gateway 刚重启（binding None）、开关开：四个 mode 各完成一轮冷启动 Wake，ContextPlan current_request 为 `wake_request:<rid>`。
3. 数据库只新增 assistant 行；user 行数不变。
4. 冷启动 Wake 后下一句 Chat：`hot_decision == 'NO_OP'`，context_id/epoch/resident_generation 不变，cursor 从 A 继续，不重不漏。
5. 热证明逐条负面测试：表 5 的 a–h 每条各篡改一处 → 不 PASS；`turn_kind`/`turn_mode` 伪装成 wake 但回执是 `message:` → 走 Chat 证明，不得绕过。
6. 静默冷 Wake → resident 退休，无回执，下一句冷启动。
7. relay 与 `claude -p` 两种 `build_cc_provider_env` 环境规则一致。
8. 结构性 reason 或 ContextPlan 开关为 0 → 仍跳过。
9. flush 前失败回到冷启动前状态（binding None、无回执、cursor 不动）；flush 后失败 kill + 清 binding。
10. reprepare 三条链在主动轮下各一测试。
11. 第 6 节：先红后绿。
12. **生产验收（不可由测试替代）**：重启 gateway → 打开开关 → 触发一轮 Wake → 核对 trace 与 SQL → 她发一句 Chat 确认 `NO_OP`。
