# Review Core（PR 待开；Candidate → Kernel）

`tools/memory_review.py` 逐条审核 `memory_candidates` 里 `status='pending'` 的 Candidate，决定它变成什么。单进程、没有队列、租约或 worker 池。

相关 PR：#689 邮件采集（Mail Dehydrator）；#690 reader / implementer 代理定义；#691 守卫写入范围热修（formal-guard write-scope hotfix）；Review Core（PR 待开）。

```
memory_candidates(pending) → Review → memory_candidate_reviews（每条的判定）
                                   └→ 通过的：Memory Kernel（一个事务）
```

- Candidate 行不会被改写，只有 `status` 列会变。判定、依据和决定人都写在单独的 `memory_candidate_reviews`。
- `python3 -m tools.memory_review [--dry-run] [--limit N] [--status]`。`--limit` 默认 50；`--status` 只打印健康信息。
- **`--dry-run`**：跑完整条流水线，写 `dry_run=1` 的判定行，**不写 Kernel，不改 Candidate 状态**。定时任务目前只跑 dry-run（`deploy/cron.d/hayagarden-memory-review`，20:15 UTC = 北京 04:15，在 19:15 和 19:45 两个脱水任务之后）。dry-run 的判定行会被之后的正式运行覆盖；已经正式判定的行不会被 dry-run 改写。
- 顺序：priority 高的先，同级按创建时间从早到晚。

## 流水线

1. **恢复**：Kernel 里已经有这条 Candidate 的 State（上一次在提交 Kernel 之后、记录判定之前中断）→ 直接记为 `committed_new`（原因 `recovered_existing_state`），不重复写，也不再调模型。
2. **精确重复**：`find_head(content)`（归一化后的整句精确匹配）。`producer=memory.write` 命中 → `already_committed`（过渡路径：memory.write 目前还会直接写 Kernel）；其他来源命中 → `exact_duplicate`。都记 `matched_lineage_id`。这一步先于取证和模型，因为它不依赖证据（与需求列出的顺序不同，只为省模型调用）。
3. **证据可解析**：每个 `source_ref` 都必须还能读到。
   - `chat_messages:<id>`：聊天库里的行。
   - `media_asset:<id>`：`gallery.db` 的 `media_assets` 行，且证据文件还在。
   - `gmail_message:<id>`：邮件上游只读接口 `GET /readonly/messages/<id>`（凭证与 Mail Dehydrator 相同）。
   - 解析器可注入。读不到、暂时不可用、没有对应解析器、或 Candidate 本身没有可靠来源（`provenance_status≠resolved` / 无 refs）→ `needs_review`，原因写明，**不会自动提交**。
4. **曾被人拒绝**：与人工拒绝过的 Candidate（`decision='rejected'` 且 `decision_by` 为 `fedya` / `haya`）归一化后精确相同 → `suppressed_rejected`。策略自动拒绝的（`decision_by='policy'`）不算。
5. **证据支持检查**（一次模型调用，走正式 Chat 背景生成的 authority，与日采集同一条保护）：原文是否支持这条 Candidate 的说法。返回 `{supported, reason}`。不支持 → `rejected_unsupported`（`decision_by='policy'`）。
6. **Ombre 分析**：`analyze(content)`，结果原样存入 `analysis_json`，之后写进 Kernel State 供 Ombre 投影使用（不调用 digest）。第 6、7 步的所有 Ombre 调用（analyze / search / judge_same_event）一旦抛错或返回形状不对，该条一律 `needs_review`（原因 `ombre_failed:<op>:...`），**绝不自动提交**；Kernel 写入错误不在此列。
7. **搜索与同事件判断**：
   - 先与最近 10 条人工拒绝的 Candidate 做同事件判断：置信度 ≥ 0.85 → `suppressed_rejected`；0.60–0.85 → `needs_review`（`uncertain`）。
   - 再用 Ombre 语义搜索取前 5 个已有谱系（Kernel 里没有对应 head 的桶忽略），逐个判断同事件，取最高分：

   | 最高置信度 | 结果 |
   |---|---|
   | ≥ 0.85（`SAME_EVENT_REVIEW`） | `needs_review`，建议动作 `revise` |
   | 0.60 – 0.85（`SAME_EVENT_UNCERTAIN`） | `needs_review`，建议动作 `uncertain` |
   | < 0.60 或没有命中 | 继续 |
8. **归属保护**：邮件 Candidate 的 `attribution='penpal'` 且内容提到费奥多尔 / 费佳 / 哈娅 / 哈雅娜（或 Fedya / Haya）→ `needs_review`，不自动提交。
9. **自动创建**：以上都通过 → `committed_new`（`decision_by='policy'`），写入 Kernel。

## 判定与状态

| outcome | decision / decision_by | Candidate.status（正式运行） |
|---|---|---|
| `committed_new` | accepted / policy | `committed` |
| `already_committed` | duplicate / policy | `duplicate` |
| `exact_duplicate` | duplicate / policy | `duplicate` |
| `suppressed_rejected` | rejected / policy | `rejected` |
| `rejected_unsupported` | rejected / policy | `rejected` |
| `needs_review` | NULL / NULL（等待人工） | `needs_review` |
| `error`（模型或解析失败） | — | 保持 `pending`，下轮重试 |

`decision_by` 在决定前为 NULL，取值只会是 `policy` / `fedya` / `haya`。`needs_review` 的人工界面和决定写入不在本 PR。

## 写入 Kernel（`commit_candidate`）

- 每个 `source_ref` 一条 Evidence（`source_type` 取 ref 前缀，`source_ref` 原样，`content` 是读到的原文，媒体另带 `content_ref`，`origin_kind=source`），然后 `create_state` 带全部 Evidence **在同一个 Kernel 事务里**提交，失败整体回滚。
- ID 由 `candidate_id` 的哈希确定：`lin:rev:<h>`、`st:rev:<h>:v1`、`dl:rev:<h>:formation`、`ev:rev:<h>:<ref 哈希>`。重复调用发现 State 已存在就什么都不做。
- State 的 `structured_value` 与 `remember()` 同形：`title`（Ombre 的 suggested_name）、`domain`、`tags`、`importance`、`valence`、`arousal`、`memory`，另带 `review`（candidate_id、producer、attribution、why_remembered、support_reason），所以投影到 Ombre 时用的是真实分析值而不是默认值。Delta 的 `review_status='accepted_by_review'`。
- 归属对应 `epistemic_status`：`fedya`→`asserted_by_companion`（沿用现有“同伴自述”取值）；`penpal`→`asserted_by_correspondent`；`mutual`→`mutually_confirmed`；无归属→`derived_from_evidence`。`participants` 取证据作者与 Candidate 的 counterparts（`fedya` / `mutual` 加 `Fedya`）。
- 不经过 `memory_tool.save_memory`。写入 Kernel 之后的 Ombre 投影仍由现有定时 `sync()` 负责。
- 预留钩子：`commit_candidate(..., media_link=fn)` 在提交后以 `(state_id, 媒体 refs)` 调用；默认不连接，**不做任何跨库写入**，留给以后的 memory_media。

## 健康记录

`memory_review_runs` 每次运行一行：开始 / 结束、dry-run、状态（`running` / `completed` / `partial_failure` / `aborted` / `failed`）、待审总数、各 outcome 计数、失败列表、耗时。进程被杀会留下一直是 `running` 的行；启动前就失败（Ombre 未配置、authority 取不到）也会写一行 `failed`。`--status` 或 `review_health()` 给出最近一次运行、最近一次正式成功、超过 6 小时未结束的运行和当前待审数。“当晚能否跑完”看 `remaining` 与 `duration_ms`。

## 关于 Ombre：已验证 / 未验证

本仓库里没有 Ombre 源码，运行时由服务器上的 `OMBRE_RUNTIME_SRC` 指定。以下按仓库里能看到的证据分类。

**可以从仓库确认的**
- 投影 worker 现有的 op：`capabilities`、`get`、`create_if_absent`、`update`、`touch`、`list`、`search`。
- `BucketManager.search(query, limit=)` 返回含 `id` / `metadata` / `content` 的桶字典，这是 `tools/ombre_adapter.py` 现有调用方式。worker 里的只读 op `search` 据此实现。**真实 Ombre 上是否可用未验证**：worker 里的 `BucketManager(load_config())` 与 adapter 用的 `bucket_mgr` 是否等价，尚未实测。
- `utils.load_config()` 与 `bucket_manager.BucketManager`：worker 现有代码已在使用。
- worker 的 `capabilities` 返回 `ops`，Review 用它做启动前检查；缺 op 时中止（`ombre_review_ops_missing:...`）。

**接线方式**

`analyze` 与 `judge_same_event` 两个 worker op 在 Ombre 自己的解释器里运行（`OMBRE_RUNTIME_SRC` 在 `sys.path` 上），用同一份 `load_config()` 的结果构造 Ombre 自己的 dehydrator 对象，再调用它的方法。不调用 `digest`（有测试守住）。所有未验证的名字都集中在 `tools/ombre_projection_worker.py` 顶部的常量里，只在那里使用。

**未验证（仓库里找不到名字和签名，均为假设）**
- dehydrator 的模块名与类名：假定 `dehydrator.Dehydrator`，构造签名 `Dehydrator(config)`。构造或导入失败 → 该请求失败（`Incompatible`）。
- 方法名与签名：`analyze(text)`、`judge_same_event(old, new)`，同步或协程均可（worker 会 await）。
- `analyze` 返回 dict，期望含 `domain` / `valence` / `arousal` / `tags` / `suggested_name` / `importance` / `why_remembered`。worker 只检查“非空 dict”，缺失字段不补默认值，写 Kernel 时 `title` 取空、数值取 `None`，由投影层按其原有默认值处理。
- `judge_same_event` 返回 dict，含布尔 `same_event` 和 0..1 的 `confidence`（可带 `reason`）。形状不符 → worker 报错。有效分数 = `confidence`；明确 `same_event=false` 时按 0 处理（假设，需和真实语义核对）。
- 阈值 0.85 / 0.60 来自计划，不是 Ombre 的原生阈值；本模块用独立常量（`SAME_EVENT_REVIEW` / `SAME_EVENT_UNCERTAIN`）。
- 任何一项不成立时的行为：对应请求失败 → 该 Candidate `needs_review`（原因写明），不会自动提交，也不会猜测结果。要启用，需在服务器上确认 Ombre 的真实名字并只改 worker 顶部常量。

## 运维注意

- `_formal_config_read_guard` 会把 `/opt/frontend/memories.db` 在整个守卫期间强制只读。候选和判定都写在这个库里，所以 Review 只在**每一次模型调用**的瞬间进入守卫（authority 先冻结一次），不把整轮包进去。这段逻辑现在是共享函数 `tools.dayblock_scheduled_runner.formal_background_generation(chat_db_path)`，日采集、邮件采集和 Review 共用；`tests/test_formal_guard_write_scope.py` 守住“守卫内不写库”。
- 非 dry-run 需要 Kernel 库已存在，不会替你创建。
- 正式提交不受 `memory.kernel_write` 开关限制；是否切到非 dry-run 由运维手动决定，定时任务目前只跑 `--dry-run`。

## 不在本 PR

- `revise` 的提交（只会停在 `needs_review`）。
- Review 界面与人工决定的写入（`fedya` / `haya`）。
- memory.write 切换为先入 Candidate（等夜间 Review 证明能当晚跑完后另开 PR）。
- SELF / HAYA 归类。
- memory_media（图片与记忆的关联）。
