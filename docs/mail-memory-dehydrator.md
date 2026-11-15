# Mail Dehydrator（#688 Gmail → Candidate）

`tools/mail_memory_dehydrator.py` 只读上游 `ai-penpal-mail` 的本地存档，按 Gmail thread 脱水成 `memory_candidates`。不复制邮件库，不写 Kernel。

## 读什么

- `/var/lib/ai-penpal-mail/mail-archive.json`（`--archive`）：上游 `mail-archive.mjs` v1 格式。`initialComplete` 不为 true 时整轮跳过，不给半截结果。
- `/var/lib/ai-penpal-mail/mail-contacts.json`（`--contacts`）：只用来把邮箱地址换成笔友备注名。地址不进 prompt，也不进 Candidate。
- 部署前请对照 `/etc/ai-penpal-mail/.env` 里的 `MAIL_ARCHIVE_FILE` / `MAIL_CONTACTS_FILE` 确认这两个路径。

## 规则

- `source_type=gmail_message`，`source_refs` 精确到 `gmail_message:<id>`，`derived_refs` 只放 `gmail_thread:<threadId>`。
- 每条 Candidate 带 `payload.attribution`：`fedya`（必须引用至少一封 outgoing）、`penpal`（至少一封 incoming）、`mutual`（两个方向都要引用）。方向对不上的候选直接丢弃。笔友对费奥多尔的描述只能以 `penpal` 落为“笔友说”。
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
