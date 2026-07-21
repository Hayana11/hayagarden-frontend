# Internal State v3 Shadow 分阶段启用手册（1A-4b）

三道独立开关，默认全部关闭：

```text
INTERNAL_STATE_V3_SHADOW_ENABLED=0
INTERNAL_STATE_V3_SCORE_PROOF_ENABLED=0
INTERNAL_STATE_V3_USER_EVENTS_ENABLED=0
```

`USER_EVENTS` 必须三者同开。`SCORE_PROOF=0, SHADOW=1, USER_EVENTS=1` 会被拒绝。

开启 `USER_EVENTS` 前必须 `prepare-schema`：**缺 outbox 表则事件门 fail-closed**（记 `outbox_capture_gap`，不写 JSONL outbox 热路径）。

## 阶段 0：部署代码

合并后三个开关保持 `0`。生产行为与接线前一致。

## 阶段 1：准备表

```bash
python3 tools/internal_state_shadow_admin.py prepare-schema
python3 tools/internal_state_shadow_admin.py status
```

确认：

- `journal_mode` 未变
- `score_applied`（含 `score_hash`）/ `outbox`（含 `payload_hash`）/ `gap_incidents` 就绪
- 若曾有 gap sidecar JSONL，已迁入 incident ledger（不得洗白）
- **未** bootstrap

## 阶段 2：proof-only

```text
SCORE_PROOF_ENABLED=1
SHADOW_ENABLED=0
USER_EVENTS_ENABLED=0
```

等待至少一条真实成功的评分 proof。`status` 中 `proof_gap=false`。

## 阶段 3：bootstrap

```text
SHADOW_ENABLED=1
USER_EVENTS_ENABLED=0
```

```bash
python3 tools/internal_state_shadow_admin.py bootstrap
python3 tools/internal_state_shadow_admin.py status
```

必须：

- `bootstrapped=True`
- `provenance_ok=True`
- `last_scored_message_id == proof max`
- `proof_gap=False` / 无 unresolved incidents
- `watermark_lag=False`

## 阶段 4：开启用户事件

```text
SCORE_PROOF_ENABLED=1
SHADOW_ENABLED=1
USER_EVENTS_ENABLED=1
```

`user_rule` / `user_scored` 经 **数据库 outbox** 同事务入队；commit 后 drain；payload hash 冲突即失败（无 sidecar 热路径）。

## Proof gap 恢复（incident ledger）

```bash
python3 tools/internal_state_shadow_admin.py inspect-gap
python3 tools/internal_state_shadow_admin.py ack-gap --message-id N --reason '...'
# 或指定单条：
python3 tools/internal_state_shadow_admin.py ack-gap --message-id N --incident-id I --reason '...'
```

`ack-gap` 只解决该 `message_id`（或指定 `incident_id`）的未解决项，不会一键擦掉其它缺口。

## 硬交接

- 评分：`BEGIN IMMEDIATE` → **先查 proof（score_hash）** → **跨 message 单调门** → UPDATE emotion → proof → outbox → COMMIT
- 同 `message_id` + 同 `score_hash`：整次 no-op；不同 hash：冲突并记 incident，不改 emotion
- 已有更高 `message_id` proof 时，迟到评分整次放弃（不改 legacy / 不写 outbox）
- gap sidecar：必须先成功持久化 incident，才允许改 emotion；迁移经 atomic rename processing
- quarantine 未 reconcile 前 bootstrap/status fail-closed
- `chat_messages` INSERT 与 user_rule outbox 同事务；缺 outbox 表 → `outbox_capture_gap`
- schema 缺失：先 append gap JSONL，再改 emotion
- `wake_outcome` 生产调用点仍为 0
