# Mail Dehydrator（#689 Gmail → Candidate）

`tools/mail_memory_dehydrator.py` 通过上游 `ai-penpal-mail` 的只读 HTTP 接口读邮件，按 Gmail thread 脱水成 `memory_candidates`。不读上游的 JSON 文件，不复制邮件库，不写 Kernel。

```
ai-penpal-mail → /readonly 接口 → Mail Dehydrator → Candidate
```

## 读什么

- `GET /readonly/archive/status`：`ready` 与 `initialComplete` 都为 true 才继续，否则整轮跳过，不给半截结果。
- `GET /readonly/conversations`：笔友（联系人）列表，取 `id` 与备注名。
- `GET /readonly/conversations/<id>`：逐页读该笔友的信。上游已把正文、标题、附件名里的邮箱地址换成联系人备注名，所以这里不再处理地址，也不读联系人库。
- thread 一律用上游消息里的 `threadId`，缺失时直接报错，不在这边推断。上游需要包含「`publicArchiveMessage` 输出 `threadId`」这一处改动。
- 同一封信同时出现在两个联系人名下时合并为一封，counterpart 记两个人。

## 凭证与地址

只读接口需要上游的 `MAIL_READ_TOKEN`。先取进程环境变量 `MAIL_READ_TOKEN`，没有则读上游正式配置 `/etc/ai-penpal-mail/.env`（`--mail-env`）里的同名项；都没有则整轮跳过。服务地址默认 `http://127.0.0.1:3457`（`--mail-url`）。上游未配置 `MAIL_READ_TOKEN` 时没有只读接口，需先在上游 `.env` 中配置并重启 `ai-penpal-mail`。

发件人识别为通知类（备注名含 no-reply / notifications / mailer-daemon，或以 Google / Gmail 开头）的 thread 不调用模型。上游不再给出邮箱地址，所以这里按备注名判断。

## 规则

- `source_type=gmail_message`，`source_refs` 精确到 `gmail_message:<id>`，`derived_refs` 只放 `gmail_thread:<threadId>`。
- 每条 Candidate 带 `payload.attribution`：`fedya`（必须引用至少一封 outgoing）、`penpal`（至少一封来自非哈娅联系人的 incoming）、`mutual`（两个方向都要引用）、`haya`（见下节，仅在配置了哈娅的联系人时可用）。方向对不上的候选直接丢弃。笔友对费奥多尔的描述只能以 `penpal` 落为“笔友说”。
- 回信里引用的旧信（`>` 行、`On … wrote:`、`在 … 写道：`、原始邮件/转发分隔线之后）在送模型前去掉，避免旧话被算到回信人头上。
- 附件只给文件名和类型，不读内容。
- 发件人全是 no-reply / 通知类地址的 thread 不调用模型，直接记为已处理。

## 增量

`mail_dehydrator_progress` 记录处理过的 message id（与 `memory_candidates` 同库）。thread 后来又来了新信，只把新信标为 `role=new`，最近的旧信作为 `role=context` 给模型理解，候选必须至少引用一封新信。模型失败的 thread 不标记，下一轮重试；Candidate ID 由 thread、内容、归属和证据稳定生成，重跑不重复。

每轮最多处理 `--max-threads`（默认 20）个 thread，按最早的新信先处理；首次回填可以用 `--since YYYY-MM-DD` 限定范围，再多跑几轮。

## 运行

```bash
python3 -m tools.mail_memory_dehydrator --max-threads 5
```

定时任务：`deploy/cron.d/hayagarden-mail-memory-dehydrator`，每天 19:45（在日采集 19:15 之后）。模型沿用正式 Chat authority，与日采集同一条读取保护。

## 哈娅自己的来信

哈娅本人也会给费奥多尔的 Gmail 写信，不应被当成笔友。配置哈娅在上游的联系人 id（逗号分隔）：

- `runtime_config` 键 `mail.haya_contact_ids`，例如 `c_xxx,c_yyy`。读取方式：以 `mode=ro` + `query_only` 只读打开 `--db`（正式库），在任何写入和模型调用之前读一次；库、表或键不存在、值为空都视为“未配置”，行为与以前完全一致（prompt 也不变）。
- 命令行 `--haya-contact-ids c_xxx,c_yyy` 覆盖 runtime_config。

配置后：

- 来自这些联系人的 incoming 信，speaker 与 counterpart 都写“哈娅”（不是联系人名字）；Candidate 的 `payload.counterparts` 保留 `contact_id`，`name` 写“哈娅”。哈娅的 thread 不会因联系人名字像通知邮件而被当成自动邮件跳过。
- 新增 `attribution=haya`：哈娅写给费奥多尔的信，证据必须包含至少一封哈娅写来的 incoming。
- `penpal` 的证据必须包含至少一封非哈娅联系人的 incoming，只引用哈娅来信的 `penpal` 会被丢弃。
- 费奥多尔回给哈娅的信仍是 `fedya`；双方确认的约定仍是 `mutual`（outgoing + incoming）。
- Review Core 对 `haya` 不走“笔友提到费奥多尔/哈娅”的保护，Kernel 的 `epistemic_status` 为 `derived_from_evidence`（与聊天里哈娅的发言同义）。

改规则后想让被判“无依据”的 Candidate 重新审核，见 `docs/memory-review-core.md` 的 `--requeue-policy-rejected`。
