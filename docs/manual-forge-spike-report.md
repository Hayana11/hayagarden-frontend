# P-CONTEXT-WINDOW-SPIKE-0C.2 报告

**Verdict:** NO-GO (`live_probe_status=NOT_RUN_NO_CREDENTIALS`)  
**Spike revision:** `P-CONTEXT-WINDOW-SPIKE-0C.2`
**Claude Code (pinned):** `@anthropic-ai/claude-code@2.1.220`  
**Touched production:** 否  
**ci_verified:** false（本地 structural-only；非 GitHub Actions 已验证结果）

> 机器可读结果见 `artifacts/spike-claude-forge-resume/results.json`。本轮只运行 structural-only 与 mocked integration；没有读取、复制或显示用户默认 Claude credentials，也没有执行 credentialed live。

## 执行命令

```bash
python3 -m unittest tests.test_claude_forge_spike tests.test_claude_forge_live_gate tests.test_claude_forge_integration -v
python3 scripts/spike_claude_forge_resume.py --structural-only
```

Credentialed live（仅本地 disposable 隔离目录，本轮未执行）：

```bash
python3 scripts/spike_claude_forge_resume.py \
  --allow-isolated-subscription-auth \
  --work-root <external-work-root> \
  --report <external-work-root>/live-results.json
```

## 隔离 Claude App 订阅登录（人工前置步骤）

Harness 不会执行 `auth login`、不会打开浏览器，也不会从默认 `~/.claude`、Windows 用户 Claude 配置或其他目录迁移 credentials。登录前先创建一次性外部 work-root，并始终使用其中同一个 `claude-home`。

PowerShell 示例：

```powershell
$spikeWorkRoot = 'D:\disposable\claude-forge-live1'
New-Item -ItemType Directory -Force -Path "$spikeWorkRoot\claude-home", "$spikeWorkRoot\isolated-project"
$env:CLAUDE_CONFIG_DIR = "$spikeWorkRoot\claude-home"
$env:DISABLE_AUTOUPDATER = '1'
npx --yes @anthropic-ai/claude-code@2.1.220 auth login
```

浏览器中必须选择 Claude App 的官方 Pro/Max 订阅登录；不得使用 `--console`，也不需要生成、复制或粘贴 OAuth token。登录完成后，在同一进程环境、同一 `CLAUDE_CONFIG_DIR` 与同一 work-root 下运行：

```powershell
python scripts/spike_claude_forge_resume.py `
  --allow-isolated-subscription-auth `
  --work-root $spikeWorkRoot `
  --report "$spikeWorkRoot\live-results.json"
```

未传 `--allow-isolated-subscription-auth` 时，认证规则保持不变，只有 `ANTHROPIC_API_KEY` 或 `CLAUDE_CODE_OAUTH_TOKEN` 可启用 live。传入开关但隔离登录无效时，Harness 只记录脱敏原因并保持 `NOT_RUN_NO_CREDENTIALS`。

## SPIKE-0C.2 变更

| 项 | 内容 |
|----|------|
| Auth opt-in | 新增 `--allow-isolated-subscription-auth`，默认关闭；显式环境凭证保持原优先级 |
| 初始化顺序 | 在认证判断前验证并创建外部 work-root、`isolated-project` 与 `claude-home` |
| Subscription gate | 固定运行 `npx --yes @anthropic-ai/claude-code@2.1.220 auth status`；仅接受 `loggedIn=true` 的 Pro/Max Claude App 登录 |
| Auth environment | `CLAUDE_CONFIG_DIR` 固定为 `<work-root>/claude-home`；status 与订阅 live 子进程移除 API key、token、base URL、Bedrock/Vertex override |
| Privacy | report 只记录认证来源或脱敏失败原因，不保存完整 status、邮箱、组织名、credential path 或文件列表 |
| Path boundary | `claude-home` 只能由已验证的外部 work-root 派生，拒绝 symlink，不接受任意配置目录参数 |
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

- `tested_commit_sha` 是运行测试与 structural harness 时的父提交；0C.2 提交前应为 `398ef33726b2992b4218c364acef416f7888c46d`。
- `tested_tree_sha` 是测试时已暂存的代码、测试和报告所组成的 Git tree，不包含随后由 harness 刷新的 `results.json`。
- `tested_diff_sha256` 是相对 `tested_commit_sha` 的被测变更摘要。
- `artifact_commit_sha` 在提交前明确记录为 `artifact_commit_pending`。刷新后的 `results.json` 与代码一起提交，因此最终 artifact commit 是随后产生的新提交，不能伪装成 `tested_commit_sha`。
- `ci_verified=false` 表示这些证据来自本地隔离 structural/mock 运行，不代表 GitHub Actions 或 credentialed live 已验证。

0C.2 完成后停止结构施工；下一阶段是本地隔离订阅登录与单次 credentialed live，本轮未执行。

## Verdict truth table

| 条件 | Verdict |
|------|---------|
| structural-only / NOT_RUN_NO_CREDENTIALS | NO-GO |
| CASE 0 或 1 live 失败 | NO-GO |
| CASE 2A 与 2B 均失败 | NO-GO |
| CASE 0+1 通过，其他必需 CASE 失败 | CONDITIONAL GO |
| 全部必需 CASE 通过 | GO |

**本轮不得因结构测试通过升级为 GO。**
