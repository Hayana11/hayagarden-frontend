# Internal State v3 Shadow 分阶段启用手册（1A-4b）

三道独立开关，默认全部关闭：

```text
INTERNAL_STATE_V3_SHADOW_ENABLED=0
INTERNAL_STATE_V3_SCORE_PROOF_ENABLED=0
INTERNAL_STATE_V3_USER_EVENTS_ENABLED=0
```

## 阶段 0：部署代码

合并后三个开关保持 `0`。生产行为与接线前一致。

## 阶段 1：准备表

```bash
python3 tools/internal_state_shadow_admin.py prepare-schema
python3 tools/internal_state_shadow_admin.py status
```

确认：

- `journal_mode` 未变
- `score_applied` / `internal_state_v3` / `internal_state_events` / `proof_health` 就绪
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

## 阶段 4：开启用户事件

```text
USER_EVENTS_ENABLED=1
```

（仍须 `SHADOW_ENABLED=1`，否则事件门禁不放行。）

从此 `user_rule` / `user_scored` 进入 Shadow。旧系统仍是唯一生产权威。

## 硬交接

- 评分事务开始前 proof 表必须已由 `prepare-schema` 建好
- `emotion_state` UPDATE 与 proof **同一** `BEGIN…COMMIT`
- 事务内禁止 `ensure_schema` / DDL
- `wake_outcome` 生产调用点仍为 0（留给后续 PR）
