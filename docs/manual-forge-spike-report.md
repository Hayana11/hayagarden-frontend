# P-CONTEXT-WINDOW-SPIKE-0C.1 报告

**Verdict:** NO-GO (`live_probe_status=NOT_RUN_NO_CREDENTIALS`)  
**Spike revision:** `P-CONTEXT-WINDOW-SPIKE-0C.1`
**Claude Code (pinned):** `@anthropic-ai/claude-code@2.1.220`  
**Touched production:** 否  
**ci_verified:** false（本地 structural-only；非 GitHub Actions 已验证结果）

> 机器可读结果见 `artifacts/spike-claude-forge-resume/results.json`。本轮只运行 structural-only 与 mocked integration；没有读取或使用真实 Claude 凭证。

## 执行命令

```bash
python3 -m unittest tests.test_claude_forge_spike tests.test_claude_forge_live_gate tests.test_claude_forge_integration -v
python3 scripts/spike_claude_forge_resume.py --structural-only
```

Credentialed live（仅隔离机，本轮未执行）：

```bash
python3 scripts/spike_claude_forge_resume.py
```

## SPIKE-0C.1 变更

| 项 | 内容 |
|----|------|
| CASE 0 | create 前生成 canary；stdout 与原生 JSONL 均严格匹配 canary；session 文件名匹配 stdout session ID；create 与 resume 之间不改写 JSONL |
| Raw event 分类 | 每条 JSON object 明确分类为 conversation node、assistant usage observation、metadata 或 malformed conversation event |
| Usage observation | usage-only assistant 保留在 raw append，不参与 UUID/parent 链或 forge validator；重复/冲突 requestId 仅 warning |
| Malformed event | 显式 user/assistant 若不满足 conversation 或 usage observation 契约，必须以 `malformed_conversation_event` 失败 |
| Raw-file gate | before bytes 保持完整前缀；after 只追加合法 JSON object；metadata 可穿插 |
| Conversation projection | 仅投影 user/assistant；对投影执行 sessionId、UUID、parent、validator、tool pairing 与旧 UUID 扫描 |
| Metadata | 支持 `file-history-snapshot`、`queue-operation`、`agent-name`、`custom-title`、`progress`、`system/turn_duration`；未知类型仅 warning |
| CASE 5B/6 | `prepare_history_for_live_gate` 依据最后一个对话事件处理 assistant/user 尾，忽略 raw metadata 尾 |
| Parser | `stream_event` 嵌套 delta；`saw_text_delta` 与 `assistant_text` 分离 |
| Runner | `time.monotonic()` 单一绝对 deadline 覆盖 pipe readers 与 `proc.wait()`；超时终止进程组 |
| Path guard | `verify_work_root` 在 resolve 前拒绝 symlink |
| 证据 | commit/tree/diff、unit/harness 命令与退出码、artifact pending、generated_at、ci_verified |

## 机器证据语义

- `tested_commit_sha` 是运行测试与 structural harness 时的父提交；0C.1 提交前应为 `e730293b3f878563b7df20f943c15f540b1fb088`。
- `tested_tree_sha` 是测试时已暂存的代码、测试和报告所组成的 Git tree，不包含随后由 harness 刷新的 `results.json`。
- `tested_diff_sha256` 是相对 `tested_commit_sha` 的被测变更摘要。
- `artifact_commit_sha` 在提交前明确记录为 `artifact_commit_pending`。刷新后的 `results.json` 与代码一起提交，因此最终 artifact commit 是随后产生的新提交，不能伪装成 `tested_commit_sha`。
- `ci_verified=false` 表示这些证据来自本地隔离 structural/mock 运行，不代表 GitHub Actions 或 credentialed live 已验证。

0C.1 完成后停止结构施工；下一阶段是隔离 credentialed live 验证，本轮未执行。

## Verdict truth table

| 条件 | Verdict |
|------|---------|
| structural-only / NOT_RUN_NO_CREDENTIALS | NO-GO |
| CASE 0 或 1 live 失败 | NO-GO |
| CASE 2A 与 2B 均失败 | NO-GO |
| CASE 0+1 通过，其他必需 CASE 失败 | CONDITIONAL GO |
| 全部必需 CASE 通过 | GO |

**本轮不得因结构测试通过升级为 GO。**
