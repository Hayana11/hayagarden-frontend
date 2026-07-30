# P-CONTEXT-WINDOW-SPIKE-0C 最终报告

**Verdict:** `LIVE_PASS`  
**Spike revision:** `P-CONTEXT-WINDOW-SPIKE-0C`（含 PR #156 元数据过滤窄修）  
**Claude Code (pinned):** `@anthropic-ai/claude-code@2.1.220`  
**Touched production:** 否  
**ci_verified:** false（本地/VPS 隔离验证；非 GitHub Actions 已验证结果）

> 机器可读结果见 `artifacts/spike-claude-forge-resume/results.json`。本轮仅做证据收口；不部署、不接入正式换窗。

## 最终结论

| 项 | 结果 |
|----|------|
| Structural tests | **84/84 pass** |
| 真实 JSONL 元数据过滤 | pass（`queue-operation`、`last-prompt` 等在 Forge 前丢弃） |
| 官方 Claude Code forged resume | **LIVE_PASS** |
| Spike 状态 | **已关闭** |
| 生产接入 | 未包含；下一主线为无缝换窗 Forge 接入 |

## 结构验证

```bash
python3 -m unittest tests.test_claude_forge_spike tests.test_claude_forge_live_gate tests.test_claude_forge_integration -v
```

- 84 tests，exit 0
- 含 PR #156 回归测试 `test_real_metadata_filtered_from_kept_events`

## 元数据过滤窄修（PR #156）

`forge_transcript()` 收集 `kept` 事件时使用现有 `_is_conversational()`：

- 保留：`user`、`assistant`、`system`
- 丢弃：`summary`、`queue-operation`、`last-prompt`、`result`、`file-history-snapshot` 及其他非对话记账行
- 未扩展 validator allowlist；未修改生产窗口、数据库或 Shadow

## 真实源 JSONL（Clean Shadow，脱敏）

| 项 | 值 |
|----|-----|
| 行数 | 10 |
| SHA-256 | `8256e0d62f2115aa7bb92d571684206abc0d4bf6cd1e4fc728723ffcb4c92d8d` |
| 类型分布 | `queue-operation×4`, `user×2`, `assistant×3`, `last-prompt×1` |

Forge 后仅保留 5 条对话事件；`validate_forged_transcript` 通过。

## LIVE_PASS 证据

| 项 | 值 |
|----|-----|
| Forged session ID | `8d38d14b-1256-4e2d-82e6-23d23829938f` |
| 结构校验 | pass（首行 `user`，5 条对话事件） |
| SessionStart | 识别 forged session ID |
| 追加 user | 「测试消息 C」 |
| 追加 assistant | 「测试回复 C」 |
| content delta | 出现（`测` → `试回复 C`） |
| result | `subtype: success`，`is_error: false` |
| exit code | 0 |

**正确 resume 调用要求：** prompt 必须紧跟 `-p`，不得放在所有选项末尾。

```bash
npx --yes @anthropic-ai/claude-code@2.1.220 \
  -p "测试消息 C。请只回复：测试回复 C" \
  --resume "<forged-session-id>" \
  --output-format stream-json \
  --verbose \
  --include-partial-messages \
  --max-turns 1 \
  --tools ""
```

## 早前失败重新归类

将 prompt 置于选项末尾的首次 resume 尝试记为 **`LIVE_INVOCATION_FAIL`**（运行时未收到非空 continuation prompt；JSONL 未追加「测试消息 C」）。**不是 Forge 或 validator 缺陷。**

## Spike 范围与废止说明

以下旧结论已废止，不再适用：

- `NO-GO`（仅 structural-only、无 credentialed live 时的中间态）
- `NOT_RUN_NO_CREDENTIALS`（credentialed live 已完成）
- 「下一阶段进入 credentialed live」
- 「继续保持 Draft、不 merge」

**Forge Spike 正式关闭。** 不进入 CASE 2B、3A 或其他 live。下一项目步骤：无缝换窗 Forge 接入（独立项目，不在本 Spike PR 范围内）。
