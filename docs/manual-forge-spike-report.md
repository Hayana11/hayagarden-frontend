# P-CONTEXT-WINDOW-SPIKE-0: Manual Forge Resume Feasibility

**Status:** Isolation spike only — **not** authorized for production implementation.  
**Verdict:** **NO-GO** (`live_probe_status=NOT_RUN_NO_CREDENTIALS`)  
**Spike revision:** `P-CONTEXT-WINDOW-SPIKE-0A` (Live Gate 假阳性窄修)  
**Date:** 2026-07-30  
**Claude Code (pinned):** `@anthropic-ai/claude-code@2.1.220`  
**Touched production:** **否**

> **证据归属：** 本报告记录的是**本地 structural-only 运行结果**。`tested_source_sha` 以 harness 运行时 `git rev-parse HEAD` 为准；**非** GitHub Actions 已验证结果（`ci_verified=false`）。

---

## SPIKE-0A 变更摘要

| 项 | 修复 |
|----|------|
| Canary | 历史随机 `CANARY-<hex>`；live prompt 不含 canary |
| Live gate | 9 项同时满足才 `API_ACCEPTED_FIRST_DELTA` |
| 超时 | `readline` 改线程 + `kill` 回收，禁止挂死 |
| 认证 | 仅 `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`；`--structural-only` 忽略宿主机凭证 |
| 版本 | 锁定 `@2.1.220` + `DISABLE_AUTOUPDATER=1` |
| Verdict | CASE 0/1 失败 → NO-GO；2A+2B 均失败 → NO-GO；缺 live CASE → 最多 CONDITIONAL GO |
| 路径 | 写前 symlink/root 检查 + 原子 `os.replace` |
| UUID/Tool | 全树旧 UUID 扫描；tool 顺序/格式/配对校验 |

### Verdict truth table（修复后）

| 条件 | Verdict |
|------|---------|
| `structural-only` 或 `NOT_RUN_NO_CREDENTIALS` | **NO-GO** |
| CASE 0 或 CASE 1 live 失败 | **NO-GO** |
| CASE 2A 与 2B 均 live 失败 | **NO-GO**（不得 GO） |
| CASE 0+1 通过，其他必需 CASE 有失败 | **CONDITIONAL GO** |
| 全部必需 live CASE 通过严格 gate | **GO** |

必需 live CASE：`0, 1, 2A, 2B, 3A, 5B, 6, 7`

---

## 1. 只读环境清单

### 1.1 Git（本地隔离环境，未改生产）

| 项 | 值 |
|----|-----|
| branch | `cursor/spike-claude-forge-resume-e370` |
| HEAD SHA | `64f5bde7d7a69685938af411fa8660353667d1e0` |
| origin/main SHA | `64f5bde7d7a69685938af411fa8660353667d1e0` |
| git status | 仅 Spike 新增文件（未 merge / deploy） |

### 1.2 Claude Code CLI（本地 npx，未触碰 VPS）

```
2.1.220 (Claude Code)
```

相关 `--help` 摘录（`npx @anthropic-ai/claude-code --help`）：

| 参数 | 说明 |
|------|------|
| `-p` / `--print` | 非交互单次调用 |
| `-r` / `--resume [sessionId]` | 按 session ID 恢复（仅配合 `--print`） |
| `--input-format stream-json` | stdin 行式 JSON（仅 `--print`） |
| `--output-format stream-json` | stdout 行式 JSON（需 `--verbose`） |
| `--system-prompt` | 覆盖系统提示 |
| `--append-system-prompt` | 追加系统提示 |
| `--mcp-config` | MCP 配置 |
| `--allowedTools` | 工具白名单 |

**隔离试验凭证：** `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` **均未配置** → 无法执行真实 API resume 硬门。

### 1.3 正式聊天路径（只读代码审查，未改 runtime）

| 项 | 当前状态 |
|----|----------|
| `daily_runtime.DAILY_TOOL_PROFILE` | `cc_resident.TOOL_PROFILE_TEXT_ONLY` |
| `DailyWindowToolFencePending` | **仍存在**；`stream_daily_resident_turn` 在 `tool_use` 事件时抛错 |
| 小猫侧「很多工具」来源 | **非** daily formal 路径：`_CC_RESIDENT`（`gateway.py`）在 legacy 路径使用 `TOOL_PROFILE_LEGACY` + MCP；daily formal 经 `prepare_daily_turn` 强制 text_only |
| 生产开关 | 本轮 **未读取/未修改** `DAILY_SOFT_WINDOW_ENABLED`、VPS、systemd |

### 1.4 `cc_resident` 启动参数（`cc_resident.py::_spawn`）

与计划中的 Forge resume 探测对齐的基线：

```text
claude -p
  --input-format stream-json
  --output-format stream-json
  --verbose
  --include-partial-messages
  --system-prompt <text>
  --max-turns 5
  --tools ''
  --thinking-display summarized
  --exclude-dynamic-system-prompt-sections
  [--allowedTools '' | --mcp-config ... --strict-mcp-config --allowedTools <list>]
```

- Daily formal：`TOOL_PROFILE_TEXT_ONLY` → `--allowedTools ''`
- Legacy resident：`--mcp-config <cc-tools.json>` + 长 MCP 白名单

### 1.5 Transcript 路径算法

`tools/cc_jsonl_usage.py`:

```text
~/.claude/projects/<claude_project_slug(cwd)>/<sessionId>.jsonl
```

`claude_project_slug(cwd)` = `re.sub(r'[^A-Za-z0-9]', '-', abspath(cwd))`

`session_id` 获取：`cc_resident.ResidentSession._maybe_set_session_id` 从 stdout `session_id` / `sessionId` 字段捕获；JSONL 回放用 `snapshot_session_jsonl(cwd, session_id)`。

Spike 使用 **独立** `CLAUDE_CONFIG_DIR` + 临时 `cwd`，与生产 `/opt/cc-gw` 项目目录隔离。

---

## 2. 交付物

| 路径 | 用途 |
|------|------|
| `tools/claude_forge_core.py` | Forge 纯函数（UUID 重映射、sidechain 排除、注入剥离） |
| `tools/claude_forge_validator.py` | 结构校验器原型（23 项规则子集） |
| `scripts/spike_claude_forge_resume.py` | 隔离 harness + CASE 运行器 |
| `scripts/build_claude_forge_fixtures.py` | 合成 fixture 生成 |
| `tests/fixtures/claude_forge_spike/` | 合成 JSONL（无隐私） |
| `tests/test_claude_forge_spike.py` | 结构单测（7 passed） |
| `artifacts/spike-claude-forge-resume/results.json` | 本轮机器可读结果 |

---

## 3. 执行命令

```bash
# 生成合成 fixture
python3 scripts/build_claude_forge_fixtures.py

# 结构单测
python3 -m unittest tests.test_claude_forge_spike -v

# Spike（本轮为 --structural-only；有 API 凭证时去掉该 flag 跑 live resume）
python3 scripts/spike_claude_forge_resume.py --structural-only
```

Live resume 探测（有凭证时）等效命令：

```bash
CLAUDE_CONFIG_DIR=/tmp/claude-forge-spike-XXX/claude-home \
npx @anthropic-ai/claude-code -p --resume <new-session-id> \
  --input-format stream-json --output-format stream-json --verbose \
  --include-partial-messages --system-prompt '...' --max-turns 3 \
  --tools '' --allowedTools ''
# stdin: {"type":"user","message":{"role":"user","content":"请只回复两个字：收到"}}
```

---

## 4. CASE 结果表

| CASE | 名称 | 结构校验 | 首次 delta | result OK | JSONL 增长 | 状态 |
|------|------|----------|------------|-----------|------------|------|
| 0 | 控制组 native resume | — | ❌ | ❌ | ❌ | `BLOCKED_MISSING_CREDENTIALS` |
| 1 | 最小纯文本 Forge | ✅ | ❌ | ❌ | ❌ | `STRUCTURE_ONLY` |
| 2A | signed thinking 原样保留 | ✅ | ❌ | ❌ | ❌ | `STRUCTURE_ONLY` |
| 2B | signed thinking 整块删除 | ✅ | ❌ | ❌ | ❌ | `STRUCTURE_ONLY` |
| 3A | 工具回合成功 | ✅ | ❌ | ❌ | ❌ | `STRUCTURE_ONLY` |
| 3B–D | 失败/多 tool/链式 | ⏭ | — | — | — | **未实现**（需 live + 合成 session 捕获） |
| 4A–E | Tool Primer | ⏭ | — | — | — | **未实现**（primer 逻辑在 core 有 hook，未跑 live） |
| 5A | 树压平（应拒绝） | ✅ 单测可拒 | — | — | — | 校验器可捕获坏 parent |
| 5B | sidechain 整体排除 | ✅ | ❌ | ❌ | ❌ | `STRUCTURE_ONLY` |
| 6 | summary + UUID 扫描 | ✅ | ❌ | ❌ | ❌ | `STRUCTURE_ONLY` |
| 7 | 旧系统注入剥离 | ✅ | ❌ | ❌ | ❌ | `STRUCTURE_ONLY` |
| 8 | 负例（空 thinking / 孤立 tool） | ❌ 预期 | — | — | — | `STRUCTURE_REJECT` ✅ |

**关键证据（结构层）：**

- CASE 1 源 SHA `a152185b…` → 锻造后 `a6c163b7…`，4 事件，首 `user` 尾 `assistant`
- CASE 8 负例错误码：`FORGE_THINKING_INVALID:empty_thinking`、`FORGE_TOOL_PAIR:orphan_tool_use:…`

**关键证据（live 层）：** 无。CASE 0 未启动进程（缺 API 凭证）。

---

## 5. 分项结论

### 5.1 signed thinking

- **结构：** 2A 保留 `thinking`+`signature` 块可通过校验；2B 整块删除后仅剩 `text` 块，亦可通过。
- **API：** **未验证**。合成 `signature` 非真实 API 产物；不能推断原样保留是否在 `--resume` 后被 400 拒绝。
- **策略候选：** 若 live 2A 失败、2B 成功 → 可评 **CONDITIONAL GO**（正式实现默认剥 thinking）。

### 5.2 工具 / Primer

- **结构：** 单工具回合锻造后 `tool_use.id` ↔ `tool_result.tool_use_id` 配对通过校验。
- **生产围栏：** daily formal 仍为 `text_only`；Forge Core **未**解除围栏。
- **Primer：** 未跑 live；`ForgeOptions.primer_events` + `primer_separator_meta`（system 分隔）仅代码占位。

### 5.3 sidechain

- **5B：** `exclude_sidechain=True` 时输出无 `isSidechain=true`，主链仅保留「主链消息」单 user 事件。
- **限制：** 未验证真实 Task/subagent JSONL；仅合成 fixture。

### 5.4 UUID 引用

- `summary` 行整体删除；`leafUuid` 不进入输出。
- 全文件旧 UUID 扫描：`scan_unknown_uuid_strings`（已知引用路径白名单）。

### 5.5 旧系统注入剥离（CASE 7）

- 对齐键：`claude_event_uuid → app DB canonical user text`（fixture `app_db_canonical_messages.json`）。
- 锻造后 user 正文 = `今天天气怎么样`（不含 handoff/carryover/渐变脑标记）。
- **风险：** 生产需可靠 `message_id ↔ claude uuid` 映射；缺失 → `mapping_missing` / 多候选 → `mapping_ambiguous`（正式实现待补）。

### 5.6 首 delta 时间线

本轮无 live 数据。推荐状态机（已实现于 harness 注释）：

`PROCESS_STARTED → API_ACCEPTED_FIRST_DELTA → RESULT_OK → (落库/cursor 正式路径外)`

---

## 6. 风险与限制

1. **硬门未过：** 无真实 `--resume` + 首条 API 成功证据 → **不能 GO**。
2. **云 Agent 无凭证：** 不得在 production VPS 补跑（任务安全边界 #9）。
3. **CASE 3B–D、4A–E 未覆盖 live。**
4. **合成 signed thinking** 与真实 Claude Code 产物可能不一致。
5. **Tool Primer 话题污染** 未验证；禁止假设普通 user 文本可安全分隔。
6. **未使用** `mtime`、`--continue`、真实 transcript 覆写。

---

## 7. 决策门

### 最终结论：**NO-GO**

**理由：**

- CASE 0 控制组未执行（`BLOCKED_MISSING_CREDENTIALS`）
- CASE 1 最小纯文本 Forge **未**完成 live resume
- 按任务规则：「没有真实 resume + 第一请求成功证据，只能判定未通过」

### 下一阶段是否值得施工？

**值得继续 Spike，但不值得进入正式 Manual Forge 施工。**

建议下一步（仍在隔离环境）：

1. 在 **有 Claude API/OAuth 凭证的隔离机**（非生产 VPS）重跑：
   `python3 scripts/spike_claude_forge_resume.py`（无 `--structural-only`）
2. 用 **当前 Claude Code 真实生成** 的 session 替换 CASE 2 thinking、CASE 3 工具 fixture。
3. 补全 CASE 4 Primer live 与 3B–D。
4. 仅当 CASE 0+1 live 通过后，再评 CONDITIONAL GO / GO。

---

## 8. 合规确认

- ✅ 未修改生产 VPS / systemd / runtime_config / memories.db
- ✅ 未部署、未 merge
- ✅ 未读取或输出真实私密聊天
- ✅ 试验文件仅在 `/tmp/claude-forge-spike-*` 与 `tests/fixtures/claude_forge_spike/`
- ✅ 日志/artifact 已脱敏（无 token、无完整 tool 参数）

**未 merge、未 deploy、未修改生产开关。**
