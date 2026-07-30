# P-CONTEXT-WINDOW-SPIKE-0B 报告

**Verdict:** NO-GO (`live_probe_status=NOT_RUN_NO_CREDENTIALS`)  
**Spike revision:** `P-CONTEXT-WINDOW-SPIKE-0B`  
**Claude Code (pinned):** `@anthropic-ai/claude-code@2.1.220`  
**Touched production:** 否  
**ci_verified:** false（本地 structural-only；非 GitHub Actions 已验证结果）

> 机器可读结果见 `artifacts/spike-claude-forge-resume/results.json`（含 `tested_tree_sha`、`tested_diff_sha256`、`generated_at`）。

## 执行命令

```bash
python3 -m unittest tests.test_claude_forge_spike tests.test_claude_forge_live_gate tests.test_claude_forge_integration -v
python3 scripts/spike_claude_forge_resume.py --structural-only
```

Credentialed live（仅隔离机，本轮未执行）：

```bash
python3 scripts/spike_claude_forge_resume.py
```

## SPIKE-0B 变更

| 项 | 内容 |
|----|------|
| CASE 0 | 真实 native session 创建 + canary + `--resume` 严格 gate |
| CASE 5B/6 | `prepare_history_for_live_gate` 为 user 尾历史追加 assistant canary 证明回合 |
| Append 校验 | sessionId、UUID 链、live prompt、落盘 canary、全量 validator |
| Parser | `stream_event` 嵌套 delta；`saw_text_delta` 与 `assistant_text` 分离 |
| Runner | 唯一 `run_subprocess_with_timeout` + 进程组 kill |
| Path guard | `verify_work_root` 在 resolve 前拒绝 symlink |
| 证据 | `tested_tree_sha` / `tested_diff_sha256` / `generated_at` |

## Verdict truth table

| 条件 | Verdict |
|------|---------|
| structural-only / NOT_RUN_NO_CREDENTIALS | NO-GO |
| CASE 0 或 1 live 失败 | NO-GO |
| CASE 2A 与 2B 均失败 | NO-GO |
| CASE 0+1 通过，其他必需 CASE 失败 | CONDITIONAL GO |
| 全部必需 CASE 通过 | GO |

**本轮不得因结构测试通过升级为 GO。**
