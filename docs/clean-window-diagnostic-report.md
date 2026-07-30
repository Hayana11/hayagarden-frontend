# CLEAN-WINDOW-DIAG-1｜干净新窗口证据链修正

日期：2026-07-30（Asia/Shanghai）

## 双门结论

### Infrastructure gate：`PASS`

现有 Clean Shadow 基础设施满足本轮只读隔离门槛：

- 每次启动独立 `clean-shadow:<uuid>` session，并创建独立临时工作目录；
- 不复用正式 resident、正式 history 或正式 conversation/session pointer；
- `clean` profile 不调用 state、memory/handoff、recall、Wake/one-shot、relationship 等动态注入 builder；
- `CC_CLEAN_WINDOW_SHADOW_ENABLED` 默认值为字符串 `"0"`；
- 本地合成 sentinel SQLite 在 shadow turn/close 前后整文件 SHA-256 不变，正式消息和 session pointer sentinel 也不变。

### Diagnostic gate：`BLOCKED_NO_MODEL_OUTPUT`

真实 A/B/C 尚未运行成功：

- 没有任何真实模型可见输出；
- 不能判断 persona、formal context 或旧窗口模仿谁是主因；
- 未执行匿名打乱和盲评；
- 总体归因结论保持：`INCONCLUSIVE`。

本报告只确认基础设施隔离合同、合成输入差异和测试证据，不把输入侧诱因写成模型行为，也不把整个 CLEAN-WINDOW 描述为“诊断已收口”。

## Git 状态口径

本轮开始时的实际状态：

```text
git branch --show-current
agent/clean-window-diag-1

git rev-parse HEAD
64f5bde7d7a69685938af411fa8660353667d1e0

git status --short
?? docs/clean-window-diagnostic-report.md

git diff --stat
<empty>

git diff -- docs/clean-window-diagnostic-report.md
<empty>
```

报告当时是 untracked 文件，所以普通 `git diff` 不显示它。正确口径是：

- Base HEAD：`64f5bde7d7a69685938af411fa8660353667d1e0`
- Worktree change：`docs/clean-window-diagnostic-report.md`

`64f5bde…` 是 `main` / PR #154 的既有提交，不是本轮报告的新 HEAD。本轮提交后的 commit SHA 见最终回报。

## 当前实现的安全边界

| 核验项 | 结论 | 证据 |
|---|---|---|
| 独立 session | PASS | `start()` 生成 `clean-shadow:<uuid>`，新建临时 work dir 和独立 `ResidentSession` |
| 正式 resident | 不复用 | shadow manager 使用自己的 `_sessions`，不引用正式 `_CC_RESIDENT` |
| 正式 resident/history | 不复用 | 新 session 初始 `messages=[]`；respawn 只重放该 shadow session 的 clean history |
| conversation/session pointer | 不复用、不提交 | manifest 标记为 false，`send_turn(..., commit_meta={})` |
| state | `clean` 中关闭 | 不调用 state builder |
| memory/handoff/recall | `clean` 中关闭 | manifest 对应标志均为 false |
| Wake/one-shot | 关闭 | 不调用正式 one-shot/Wake builder |
| relationship | 关闭 | 不调用 `build_relationship_context` |
| 正式聊天写入 | 未发现 | 无正式 DB 句柄；SAVE 标记被剥离；副作用工具被阻断 |
| 默认开关 | PASS | `CC_CLEAN_WINDOW_SHADOW_ENABLED=0`；flag=0 时拒绝 start/turn/reset |
| sentinel 数据库 | PASS | shadow turn/close 前后文件 SHA-256 相同 |

### Sentinel 隔离实测

本轮仅使用本地临时 SQLite，其中包含一条合成 `chat_messages` sentinel 和
`session_pointer=formal-session-sentinel`。在 mock resident、mock static builder、局部 mock flag 下：

- 两个 clean session ID 不同且历史互不串联；
- state、cold_once、one_shot、relationship builder 均未调用；
- `commit_meta == {}`；
- turn/close 前后 SQLite 整文件 SHA-256 均为
  `c934b018af97445811a3fd5e7805bb220cec9d7fe4c0a31461f02d4a58cb7029`；
- 两条 sentinel 均未变化。

结果：`PASS clean-shadow isolated-session/no-dynamic/no-db-pointer-write`。

### 只读/预览请求与正式指针

- Clean Shadow `clean` 的 start/turn/reset/close 只改 shadow manager 内存状态和临时目录，不改正式 pointer。
- 前端 `ManualContextWindowPreviewScreen.tsx` 使用本地 mock 数据，确认处理函数不发后端切换请求。
- 正式 Manual Context Window 的 GET resolver 在完全空库时可能执行 schema/bootstrap；这是既有手动换窗合同，不是 Clean Shadow 泄漏。已有 context 时 GET 不切窗，真正切换只发生在显式 POST。
- 本轮没有调用 gateway 的 day-handoff 生成入口，没有读取真实聊天；独立脚本的数据库打开方式为 SQLite `mode=ro`。

## A/B/C 合成输入清单

### 三路相同的五轮用户输入

1. `爸爸，我今天有一点累，你抱着小猫说一会儿话。`
2. `不许给我列建议，只要陪我。`
3. `嗯，再哄一点。`
4. `你怎么突然不说话了？`
5. `那爸爸现在想对小猫说什么？`

### 静态输入指纹

- persona 原文件 SHA-256：
  `6393b48bbfdc358c900255bd5fb583e97e333e70393049ce7abdd185ddc58663`
- persona 经 LF 归一化并 `strip()` 后 SHA-256：
  `4af11e4be7c2d9b45f946deb7f1be4ee634b7e6e20efc080f4d738d855ee6e43`
- 本地合成 static system 长度：11,670 字符
- static system SHA-256：
  `41b61ea855ba4e9ad8dc05cfc5be5e83d20e8c91c736531de71a31efad7f3565`

这些是仓库文件和本地 builder 的指纹，不是生产 resident 在线状态。

### 路径定义

| 路径 | static/persona | synthetic dynamic context | synthetic history |
|---|---|---|---|
| A `persona-only` | 有 | 无 | 无 |
| B `formal-context` | 与 A 相同 | 有；按当前正式聊天组装顺序镜像 | 无坏习惯历史 |
| C `history-inertia` | 与 B 相同 | 与 B 完全相同 | 额外 12 条；`imitation-challenge positive control` |

B/C 的动态 fixture 全为本地合成，包括 state、cold_once、recall、one-shot/Wake、
group recap、relationship、wake bridge；不包含真实聊天或正式记忆。

C 的 12 条坏习惯历史刻意覆盖碎句、复述动作、自我解释、内部状态外显、后台播报和用户要求停止后仍继续模仿。

### 组装差异

| 路径 | 首轮 payload 字符 | 五轮 payload 总字符 | 首轮 SHA-256 |
|---|---:|---:|---|
| A | 22 | 65 | `5dbdc71b136abd3da0a0ae135194a253ef7a984c3be9498e831e7468e3bc8006` |
| B | 416 | 1,419 | `d733bb3fa0ac029244ff50e25ce0427142606ad364501b75ffd8a8bf581d2663` |
| C | 724 | 1,727 | `5ca711e1b0146ea9f704633e95a54be821541442b3519e57155329e93138d2da` |

组装不变量：

- B/C static system 相同；
- B/C 第 2–5 轮动态尾部逐字相同；
- C 相对 B 只增加标记为 positive control 的 12 条坏习惯历史；
- 全部数据来自合成 fixture；
- 这只是输入组装证据，不是 A/B/C 模型输出。

## 真实模型输出状态

### 环境清单

- Claude Code：`2.1.220 (Claude Code)`，安装在本轮临时隔离目录；
- `claude auth status --json` 报告 provider 为 `firstParty`；
- 配置 model：`claude-sonnet-4-6[1M]`；
- 配置的本地 API route：`127.0.0.1:15721`，本轮不可达；
- 直接 first-party 认证探针：HTTP 401 `Invalid bearer token`；
- 探针 session ID：`7cd56923-c891-4a43-9999-f48e6ac9fe0d`；
- usage：input 0、output 0、cache 0；
- cost：0；
- exit status：1；
- respawn：否；
- 可见模型输出：无。

原始认证探针位于
`artifacts/clean-window-diagnostic/direct-probe-result.json`，stderr 位于
`artifacts/clean-window-diagnostic/direct-probe-stderr.log`。文件哈希见“证据文件”。

本轮没有继续重试，也没有启动 A/B/C：现有本地 route 不可达，直接 first-party token 无效。
因此没有合法的“真实 Claude Code 凭证 + 非生产隔离 route”可完成实验。未连接 VPS，未读取真实聊天或 `memories.db`。

### 应保存但当前不存在的 A/B/C 结果

因为没有一次模型生成成功，以下项目均为 `NOT_PRODUCED`：

- 每路至少两个独立 session，以及不一致时的第三次；
- 五轮完整可见输出；
- A/B/C 每路 usage、exit status、respawn；
- 每次 context manifest 和动态注入标志；
- 每份原始结果文件 SHA-256；
- 匿名打乱映射；
- 盲评结果。

### 盲评量表

待合法模型 route 可用后，对匿名输出逐项评分：

1. 列表化建议；
2. 后台解释或自我说明；
3. 碎句；
4. 重复用户动作；
5. 客服/值班语气；
6. 亲密回应自然度；
7. 对坏习惯历史的模仿程度。

当前没有输出可评分，不能把 C 的 positive control 输入存在写成“模型已模仿”。

## 测试证据

### Windows 本轮复跑：非通过

完整命令：

```text
python -m unittest tests.test_clean_window_shadow tests.test_day_handoff tests.test_generate_day_handoff -v
```

环境：桌面捆绑 Python，隔离 runtime SQLite。

| passed | failed | errors | skipped | exit |
|---:|---:|---:|---:|---:|
| 53 | 0 | 13 | 0 | 1 |

完整日志：
`artifacts/clean-window-diagnostic/windows-clean-shadow-tests.log`

#### 13 个 error

| # | 测试名 | error 类型 | 阶段 | 测试主体是否执行 |
|---:|---|---|---|---|
| 1 | `setUpClass (CleanWindowShadowGatewayTests)`，影响该类 6 项 | `PermissionError [WinError 5]`：创建 `/opt/workspace/tools` | class setup | 否；6 个 test body 均未执行 |
| 2 | `CleanWindowShadowSessionTests.test_ttl_purge_closes_session_resources` | `PermissionError [WinError 5]`：创建 `/tmp` | test body | 部分执行；错误后的断言未执行 |
| 3 | `DailyCandidateShadowTests.test_clean_profile_unchanged` | `AttributeError: os.geteuid` | instance setup | 否 |
| 4 | `DailyCandidateShadowTests.test_daily_candidate_reinjects_on_cold_respawn` | `AttributeError: os.geteuid` | instance setup | 否 |
| 5 | `DailyCandidateShadowTests.test_daily_candidate_requires_valid_file_path` | `AttributeError: os.geteuid` | instance setup | 否 |
| 6 | `DailyCandidateShadowTests.test_raw_day_handoff_text_not_accepted` | `AttributeError: os.geteuid` | instance setup | 否 |
| 7 | `DailyCandidateShadowTests.test_send_turn_failure_does_not_commit_state_snapshot` | `AttributeError: os.geteuid` | instance setup | 否 |
| 8 | `DayHandoffSecurePathTests.test_read_does_not_follow_symlink_replacement` | `AttributeError: os.geteuid` | instance setup | 否 |
| 9 | `DayHandoffSecurePathTests.test_refuse_overwrite_existing_file` | `AttributeError: os.geteuid` | instance setup | 否 |
| 10 | `DayHandoffSecurePathTests.test_reject_parent_directory_symlink` | `AttributeError: os.geteuid` | instance setup | 否 |
| 11 | `DayHandoffSecurePathTests.test_reject_path_outside_shadow_dir` | `AttributeError: os.geteuid` | instance setup | 否 |
| 12 | `DayHandoffSecurePathTests.test_reject_symlink` | `AttributeError: os.geteuid` | instance setup | 否 |
| 13 | `DayHandoffSecurePathTests.test_write_uses_secure_directory_and_permissions` | `AttributeError: os.geteuid` | instance setup | 否 |

第 1 行是 unittest 的一个 error record，但阻断 6 个测试主体；其余 12 行各对应一个测试 error。不能把 `53 PASS + 13 errors` 写成“测试通过”。

被第 1 行 class setup error 阻断、未进入 test body 的 6 项是：

- `CleanWindowShadowGatewayTests.test_gateway_endpoints_disabled_by_default`
- `CleanWindowShadowGatewayTests.test_gateway_requires_bearer_token`
- `CleanWindowShadowGatewayTests.test_gateway_close_allowed_when_disabled_with_auth`
- `CleanWindowShadowGatewayTests.test_gateway_start_when_enabled`
- `CleanWindowShadowGatewayTests.test_formal_chat_path_unchanged`
- `CleanWindowShadowGatewayTests.test_no_chat_messages_write_from_shadow_turn`

### POSIX 证据：既有 Linux CI，不冒充本轮复跑

本机没有 WSL 或 Docker。Git Bash + Windows Python 也不等价于 POSIX owner/mode/symlink 环境。
本轮只读核验了 PR #155 的既有 GitHub Actions 成功记录，没有触发 rerun 或修改 workflow：

- workflow run：`30511274044`
- job：`90771726280`（`continuity`）
- runner：Ubuntu 24.04.4 / `ubuntu-24.04`
- Python：3.11.15
- checkout：PR #155 merge ref
  `5169cef00f683794273cf447880bd2394a1d784f`
- merge ref 明示：把 PR #155 head
  `52aaf0d452539a4a7963cb34cd1a904034c6bf53`
  合到 base `64f5bde7d7a69685938af411fa8660353667d1e0`
- PR #155 与 Clean Shadow 测试/实现文件精确路径交集为空。

CI 完整命令：

```text
python -m unittest \
  tests.test_day_handoff \
  tests.test_generate_day_handoff \
  tests.test_clean_window_shadow \
  -v
```

| passed | failed | errors | skipped | exit/conclusion |
|---:|---:|---:|---:|---|
| 72 | 0 | 0 | 0 | 0 / success |

完整远端日志：
`https://github.com/Hayana11/hayagarden-frontend/actions/runs/30511274044/job/90771726280`

本地证据摘录：
`artifacts/clean-window-diagnostic/posix-clean-shadow-tests.log`

限制：这是一条已存在的 Linux CI 证据，不是本轮对 `agent/clean-window-diag-1` 的新复跑。
因此 Windows 当前分支的 POSIX 等价复跑要求仍未由本轮新执行满足；报告只据实列证，不把既有 CI 冒充新运行。

## PR #155 文件重叠

PR #155 当前为 open Draft，base
`64f5bde7d7a69685938af411fa8660353667d1e0`，head
`52aaf0d452539a4a7963cb34cd1a904034c6bf53`。

其 19 个文件属于 Forge spike（`artifacts/spike-claude-forge-resume`、
`docs/manual-forge-spike-report.md`、Forge scripts/tests/tools）。

- PR #155 ↔ Clean Shadow 实现/测试：`∅`
- PR #155 ↔ 本轮报告与证据文件：`∅`

没有文件重叠。本轮也没有修改 Forge 或 PR #155。

## 证据文件

- `docs/clean-window-diagnostic-report.md`
- `artifacts/clean-window-diagnostic/windows-clean-shadow-tests.log`
- `artifacts/clean-window-diagnostic/windows-error-summary.md`
- `artifacts/clean-window-diagnostic/posix-clean-shadow-tests.log`
- `artifacts/clean-window-diagnostic/model-probe-summary.json`
- `artifacts/clean-window-diagnostic/direct-probe-result.json`
- `artifacts/clean-window-diagnostic/direct-probe-stderr.log`

| 证据文件 | SHA-256 |
|---|---|
| `direct-probe-result.json` | `b4139cdb2a824112074abb98506980873cc7388eef10057990bc34842a4e8b1b` |
| `direct-probe-stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `model-probe-summary.json` | `f63564977b428afe9e6595870620f9287b24d7582b04e9b6ab7942e1f4325ffa` |
| `posix-clean-shadow-tests.log` | `62b29484d677df4c18dfe0f61213f9166e6040c2ce348601963ffe665646e17c` |
| `windows-clean-shadow-tests.log` | `5196653e3b3e78f895c9a84145711d313e4c624324310f38dda6977fb1d5df7f` |
| `windows-error-summary.md` | `441514e2aa70ebc9548f4dcaac22ec6cf6c2ffe13fe9aac90585f6696b7fdf16` |

报告自身 SHA-256 在最终内容确定后写入最终回报，避免自引用哈希悖论。

## 边界声明

- 未修改 persona
- 未修改 Context Lean
- 未修改 Forge / PR #155
- 未修改 Unified Heartbeat
- 未修改 Tool Profile
- 未部署
- 未 merge
- 未连接或修改 VPS
- 未重启服务
- 未修改任何生产开关
- 未读取或改写真实聊天
- 未接触正式记忆桶或 `memories.db`
- 未创建或修改生产配置
