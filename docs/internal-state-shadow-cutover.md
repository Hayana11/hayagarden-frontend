# Internal State v3 Shadow 分阶段启用手册（1A-4b）

三道独立开关，默认全部关闭：

```text
INTERNAL_STATE_V3_SHADOW_ENABLED=0
INTERNAL_STATE_V3_SCORE_PROOF_ENABLED=0
INTERNAL_STATE_V3_USER_EVENTS_ENABLED=0
```

`USER_EVENTS` 必须三者同开。`SCORE_PROOF=0, SHADOW=1, USER_EVENTS=1` 会被拒绝（避免只有 `user_rule`、永远没有 `user_scored` 的半套事件史）。

## 阶段 0：部署代码

合并后三个开关保持 `0`。生产行为与接线前一致。

## 阶段 1：准备表

```bash
python3 tools/internal_state_shadow_admin.py prepare-schema
python3 tools/internal_state_shadow_admin.py status
```

确认：

- `journal_mode` 未变
- `score_applied` / `internal_state_v3` / `internal_state_events` / `proof_health` / `outbox` 就绪
- 若曾有 proof-gap sidecar，已迁入 health（`proof_gap` 可能为 true——不得洗白）
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
- `last_error=None`
- `proof_gap=False`
- `watermark_lag=False`

## 阶段 4：开启用户事件

```text
SCORE_PROOF_ENABLED=1
SHADOW_ENABLED=1
USER_EVENTS_ENABLED=1
```

从此 `user_rule` / `user_scored` 经 **durable outbox** 进入 Shadow：

- 与权威写入同事务入队
- commit 后立即 drain
- 投递失败行保留，可 `drain-outbox` 重放
- v3 `event_key` 幂等去重

旧系统仍是唯一生产权威。

## Proof gap 恢复（受审计）

```bash
python3 tools/internal_state_shadow_admin.py inspect-gap
# 人工核对 / 必要时补齐 outbox 后：
python3 tools/internal_state_shadow_admin.py ack-gap --message-id N --reason '...'
```

禁止无证据地调用内部 `clear_proof_gap()`。

## 硬交接

- 评分事务开始前 proof / outbox 表必须已由 `prepare-schema` 建好
- `emotion_state` UPDATE 与 proof（及 scored outbox）**同一** `BEGIN…COMMIT`
- `chat_messages` INSERT 与 user_rule outbox **同一**事务
- 事务内禁止 `ensure_schema` / DDL
- schema 缺失时的 gap 写入 sidecar；`prepare-schema` 迁入 health
- `wake_outcome` 生产调用点仍为 0（留给后续 PR）
