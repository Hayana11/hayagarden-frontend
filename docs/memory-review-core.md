# Review Core（PR 待开；Candidate → Kernel）

`tools/memory_review.py` 逐条审核 `memory_candidates` 里 `status='pending'` 的 Candidate，决定它变成什么。单进程、没有队列、租约或 worker 池。

相关 PR：#689 邮件采集（Mail Dehydrator）；#690 reader / implementer 代理定义；#691 守卫写入范围热修（formal-guard write-scope hotfix）；Review Core（PR 待开）。

```
memory_candidates(pending) → Review → memory_candidate_reviews（每条的判定）
                                   └→ 通过的：Memory Kernel（一个事务）
```

- Candidate 行不会被改写，只有 `status` 列会变。判定、依据和决定人都写在单独的 `memory_candidate_reviews`。
- `python3 -m tools.memory_review [--dry-run] [--limit N] [--status] [--requeue-policy-rejected]`。`--limit` 默认 50；`--status` 只打印健康信息；`--requeue-policy-rejected` 见下文“规则改动后重排队”。
- **`--dry-run`**：跑完整条流水线，写 `dry_run=1` 的判定行，**不写 Kernel，不改 Candidate 状态**。定时任务现在正式运行、不带 `--dry-run`（`deploy/cron.d/hayagarden-memory-review`，20:15 UTC = 北京 04:15，在 19:15 和 19:45 两个脱水任务之后）。dry-run 的判定行会被之后的正式运行覆盖；已经正式判定的行不会被 dry-run 改写。
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
   **上下文窗口**：Candidate 的 `source_refs` 含 `chat_messages:<id>` 时，支持检查的 prompt 另有一节“上下文（未被引用，仅供参考）”：每条被引用的聊天消息前后各取最多 `CONTEXT_NEIGHBORS`（6）条未被引用的消息（按 id 顺序，不要求同一天），用同样的发言人标签渲染，按离被引用消息的远近优先、总字数不超过 `CONTEXT_MAX_CHARS`（6000）。被引用的不会重复出现。读取上下文失败只记警告，仍只按被引用的证据判断。
   **`extra_evidence_ids`**（可选）：模型认为上下文里哪几条是支持候选所必需的，返回它们的聊天消息 id。必须是上下文一节里出现过的 id，未知 id 忽略并记警告（写进 `ignored_extra_evidence_ids`）。判定为支持时，这些消息作为额外 Evidence（`chat_messages:<id>`，provenance 标 `evidence_role=context_extra`）随 `commit_candidate` 写入 Kernel，并记在判定行 `support_check_json.extra_evidence_ids`。Candidate 行的 `source_refs` 不改写；Evidence ID 仍按 ref 哈希确定，重跑结果相同。
   **删减重写（`supported_rewrite`，可选）**：核心被支持但部分细节不被支持时，模型可给出只保留被支持部分的版本（同语言、自成一句、不加新内容）。若 `supported=false` 且有非空、且与原文不同的重写 → 对重写**再做一次**支持检查（证据含 extra ids；这一次返回的 `supported_rewrite` 忽略，绝不循环）。第二次为 `supported` → 以重写为内容继续后面的步骤（精确重复、人工拒绝抑制、Ombre 分析与搜索、同事件、归属保护），自动创建时 Kernel State 的 `content` 是重写，`structured_value.review` 带 `trimmed_from_candidate=true` 和 `original_content`；判定行原因为 `trimmed_rewrite`，`support_check_json.trimmed_rewrite` 含 `committed_content`、`original_check`、`rewrite_check`。`memory_candidates` 里的原文不动。第二次核对**同时收到原始候选和删减版**（仍是同一次模型调用，没有第三次），要分别回答 `supported`（删减版有证据支持）和 `rewrite_faithful`（删减版只是删掉了原始候选里无证据的部分，没有加入新事实、没有换主体、事件、时间或对象、没有改成另一件事）。只有 `supported=true` 且 `rewrite_faithful=true` 才继续；任何一项为 false，或没有重写 → `rejected_unsupported`（`support_check_json.rewrite_check` 记第二次结果；不忠实时原因以 `rewrite_not_faithful:` 开头）。第二次缺少布尔值 `rewrite_faithful` 算支持输出格式错误，按下面的“连续出错上限”处理。
6. **Ombre 分析**：`analyze(content)`，结果原样存入 `analysis_json`，之后写进 Kernel State 供 Ombre 投影使用（不调用 digest）。第 6、7 步的所有 Ombre 调用（analyze / search / judge_same_event）一旦抛错或返回形状不对，该条记为 `error`（原因 `ombre_failed:<op>:...` 写进判定行的 `error`），Candidate 保持 `pending`，下轮重试，**绝不自动提交，也不会涌进人工队列**（和 Kernel 写入错误同样处理）。
7. **搜索与同事件判断**：
   - 先与最近 10 条人工拒绝的 Candidate 做同事件判断：置信度 ≥ 0.85 → `suppressed_rejected`；0.60–0.85 → `needs_review`（`uncertain`）。
   - 再用 Ombre 语义搜索取前 5 个已有谱系（Kernel 里没有对应 head 的桶忽略），逐个判断同事件，取最高分：

   | 最高置信度 | 结果 |
   |---|---|
   | ≥ 0.85（`SAME_EVENT_REVIEW`） | `needs_review`，建议动作 `revise` |
   | 0.60 – 0.85（`SAME_EVENT_UNCERTAIN`） | `needs_review`，建议动作 `uncertain` |
   | < 0.60 或没有命中 | 继续 |
8. **归属保护**：邮件 Candidate 的 `attribution='penpal'` 且内容提到费奥多尔 / 费佳 / 哈娅 / 哈雅娜（或 Fedya / Haya）→ `needs_review`，不自动提交。`attribution='haya'`（哈娅自己写给费奥多尔的信）不走这条保护。
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
| `error`（模型、解析或 Ombre 调用失败） | — | 保持 `pending`，下轮重试 |
| `needs_review`（原因 `repeated_errors:<最后一次错误>`） | NULL / NULL | `needs_review` |

**连续出错上限**：`memory_candidate_reviews.error_count`（加列是增量、幂等的 `ALTER TABLE ADD COLUMN`）记每条 Candidate 连续出错的运行次数，每次 `error` +1，记录任何非 error 判定时清零。第 `ERROR_LIMIT`（3）次出错的那一轮改记 `needs_review`（原因 `repeated_errors:<最后一次错误>`），Candidate 状态随之为 `needs_review`，不再无限重试。**只有“内容 / 模型判定类、重试大概率仍卡在同一条 Candidate 上”的错误计数并转人工**：支持检查返回空文本、无法解析、不是对象或缺少必需布尔值（`SupportOutputError`），以及单条 Candidate 的 `OmbreFailure`。Kernel 提交、SQLite、IO、模型调用本身的传输故障等基础设施异常仍是 `error → pending → 下一轮重试`：照常进入失败记录和运行状态 `partial_failure`，但不增加 `error_count`，也不会转 `needs_review`（人工审核解决不了磁盘或数据库故障）；它们也不会清零已有的计数。启动前检查中止不碰 Candidate，不计数。`--dry-run` 不增加 `error_count`、不改状态，但判定行会显示“如果正式运行会怎样”（达到上限时显示 `needs_review`）。

`decision_by` 在决定前为 NULL，取值只会是 `policy` / `fedya` / `haya`。`needs_review` 的人工界面和决定写入不在本 PR。

## 写入 Kernel（`commit_candidate`）

- 每个 `source_ref` 一条 Evidence（`source_type` 取 ref 前缀，`source_ref` 原样，`content` 是读到的原文，媒体另带 `content_ref`，`origin_kind=source`），然后 `create_state` 带全部 Evidence **在同一个 Kernel 事务里**提交，失败整体回滚。
- ID 由 `candidate_id` 的哈希确定：`lin:rev:<h>`、`st:rev:<h>:v1`、`dl:rev:<h>:formation`、`ev:rev:<h>:<ref 哈希>`。重复调用发现 State 已存在就什么都不做。
- State 的 `structured_value` 与 `remember()` 同形：`title`（Ombre 的 suggested_name）、`domain`、`tags`、`importance`、`valence`、`arousal`、`memory`，另带 `review`（candidate_id、producer、attribution、why_remembered、support_reason），所以投影到 Ombre 时用的是真实分析值而不是默认值。Delta 的 `review_status='accepted_by_review'`。
- 归属对应 `epistemic_status`：`fedya`→`asserted_by_companion`（沿用现有“同伴自述”取值）；`penpal`→`asserted_by_correspondent`；`mutual`→`mutually_confirmed`；`haya`→`derived_from_evidence`（哈娅自己的话，与聊天里 user 发言产生的 Candidate 同一语义，目前没有单独的取值）；无归属→`derived_from_evidence`。`participants` 取证据作者与 Candidate 的 counterparts（`fedya` / `mutual` 加 `Fedya`）。
- 不经过 `memory_tool.save_memory`。写入 Kernel 之后的 Ombre 投影仍由现有定时 `sync()` 负责。
- 预留钩子：`commit_candidate(..., media_link=fn)` 在提交后以 `(state_id, 媒体 refs)` 调用；默认不连接，**不做任何跨库写入**，留给以后的 memory_media。

## 发言人标注（支持度检查）

支持度检查模型看不到“谁在说话”时，会把 `author=assistant` 当成“笔友/对方”，或抱怨证据里“没有出现哈娅这个名字”。所以每段证据现在带一个显式标签：

- 聊天：`author` 为 `hayana` / `haya` / `user` → `发言人：哈娅`；`assistant` → `发言人：费奥多尔`；其他作者保留原始值。
- 邮件：`direction=outgoing` → `发言人：费奥多尔`；incoming 若上游给了署名则用署名，否则标 `incoming 来信人`并附上 Candidate 的 counterparts（哈娅的信在 Mail Dehydrator 里已把 counterpart 名写成“哈娅”）。
- 图片：标 `机器标注，不是任何人说的话`。

support 的 system prompt 同时写明：聊天记录里 user/哈娅 的发言是哈娅说的，assistant 的发言是费奥多尔说的；邮件里 outgoing 是费奥多尔写的，incoming 是署名的写信人写的（若标注为哈娅则是哈娅写的）。`attribution=haya` 表示哈娅自己说的话或写给费奥多尔的信。

## 规则改动后重排队

`python3 -m tools.memory_review --requeue-policy-rejected`：把“最近一次正式（非 dry-run）审核为 `rejected_unsupported`、`decision_by='policy'`、状态为 `rejected`”的 Candidate 改回 `pending`，打印 `{"requeued": N}` 后退出（不需要 Ombre，不写 Kernel）。已经是 `pending` 的不动；人工拒绝（`decision_by` 为 fedya / haya）、已提交、重复、`needs_review` 的都不碰。只有 dry-run 审核时 Candidate 本来就是 `pending`，结果为 0。因为一条 Candidate 只有一行审核记录，被重排队的那一行保留但 `outcome` 改成 `requeued`（`decision` / `decision_by` 清空，原因前缀 `requeued_from_rejected_unsupported:`），否则它会被当作“已决定”而不再重审；下次运行会用新结果覆盖这一行。

## 健康记录

`memory_review_runs` 每次运行一行（`counts_json` 里另有 `ombre_failure_count`；本轮实际审核的每一条都因 Ombre 失败时，整轮标 `failed`，原因 `ombre_failed_all_candidates`，退出码非零；部分失败为 `partial_failure`）：开始 / 结束、dry-run、状态（`running` / `completed` / `partial_failure` / `aborted` / `failed`）、待审总数、各 outcome 计数、失败列表、耗时。进程被杀会留下一直是 `running` 的行；启动前就失败（Ombre 未配置、authority 取不到）也会写一行 `failed`。`--status` 或 `review_health()` 给出最近一次运行、最近一次正式成功、超过 6 小时未结束的运行和当前待审数。“当晚能否跑完”看 `remaining` 与 `duration_ms`。

## 关于 Ombre：已验证 / 未验证

本仓库里没有 Ombre 源码，运行时由服务器上的 `OMBRE_RUNTIME_SRC` 指定。以下按仓库里能看到的证据分类。

**可以从仓库确认的**
- 投影 worker 现有的 op：`capabilities`、`get`、`create_if_absent`、`update`、`touch`、`list`、`search`。
- `BucketManager.search(query, limit=)` 返回含 `id` / `metadata` / `content` 的桶字典，这是 `tools/ombre_adapter.py` 现有调用方式。worker 里的只读 op `search` 据此实现。**真实 Ombre 上是否可用未验证**：worker 里的 `BucketManager(load_config())` 与 adapter 用的 `bucket_mgr` 是否等价，尚未实测。
- `utils.load_config()` 与 `bucket_manager.BucketManager`：worker 现有代码已在使用。
- worker 的 `capabilities` 返回 `ops`，另带 `dehydrator: {ok, methods, error}`：worker 会尝试构造 dehydrator 并报告 `analyze` / `judge_same_event` 是否可调用，这一步绝不抛错。
- **启动前检查（`--dry-run` 同样）**：缺 op（`ombre_review_ops_missing:...`）、dehydrator 构造失败（`ombre_dehydrator_unavailable:<原因>`）或缺方法（`ombre_dehydrator_methods_missing:...`）→ 在碰任何 Candidate 之前中止，运行记录标 `aborted`/`failed` 并写明原因，不写判定行，所有 Candidate 保持 `pending`，退出码非零。

**接线方式**

`analyze` 与 `judge_same_event` 两个 worker op 在 Ombre 自己的解释器里运行（`OMBRE_RUNTIME_SRC` 在 `sys.path` 上），用同一份 `load_config()` 的结果构造 Ombre 自己的 dehydrator 对象，再调用它的方法。不调用 `digest`（有测试守住）。Ombre 的类名与方法名都集中在 `tools/ombre_projection_worker.py` 顶部的常量里，只在那里使用。

**已在生产 VPS 上确认（代码检查）**
- Ombre 的 dehydrator 类 `Dehydrator` 存在，且有 `analyze()` 与 `judge_same_event()` 两个方法。
- worker 以 `Dehydrator(load_config())` 构造它，与 `BucketManager` 用的是同一份 `load_config()` 结果。
- 启动前检查（每次运行都做，含 `--dry-run`）：加载 dehydrator 失败，或两个方法中任一缺失，都会中止整轮运行，不碰任何 Candidate。
- 以上只确认了类名、两个方法名和构造方式；构造参数的细节不在确认范围内。两个方法的返回结构没有核对，仍由 worker 的形状检查兜底。worker 对同步或协程返回值都会处理（会 await）。

**仍是假设（需和真实语义核对）**
- `analyze` 返回 dict，期望含 `domain` / `valence` / `arousal` / `tags` / `suggested_name` / `importance` / `why_remembered`。worker 只检查“非空 dict”，缺失字段不补默认值，写 Kernel 时 `title` 取空、数值取 `None`，由投影层按其原有默认值处理。
- `judge_same_event` 返回 dict，含布尔 `same_event` 和 0..1 的 `confidence`（可带 `reason`）。形状不符 → worker 报错。有效分数 = `confidence`；明确 `same_event=false` 时按 0 处理。
- 阈值 0.85 / 0.60 来自计划，不是 Ombre 的原生阈值；本模块用独立常量（`SAME_EVENT_REVIEW` / `SAME_EVENT_UNCERTAIN`）。
- 任何一项不成立时的行为：形状检查不过 → 该 Candidate 记 `error` 并保持 `pending`，不会自动提交，也不会猜测结果。若 Ombre 的类名或方法名将来改动，启动前检查会先中止，只需改 worker 顶部常量。

## 已知限制

- **同一轮内的近重复互相看不见。** 一轮 Review 里，如果两条 Candidate 用不同措辞描述同一件事，第一条会先写入 Kernel，但还没投影进 Ombre；第二条的 Ombre 搜索因此看不到它，可能另开一条谱系。这是接受的偶发重复，之后通过 revision 或人工审核合并。目前刻意不为此增加队列、索引或审核门。

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
