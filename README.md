# HayaGarden 分支导航

HayaGarden 是一个同时包含聊天网关、个人仪表盘、记忆系统、朋友圈、自动唤醒、工具调用与部署脚本的私人应用仓库。当前稳定主线是 [`main`](https://github.com/Hayana11/hayagarden-frontend/tree/main)；日常开发、部署和新分支都应从它开始。

这份导航回答两个问题：每个远程分支是做什么的，以及它是否已经进入 `main`。快照生成于 **2026-07-17**，依据 `origin/main` 的 Git 提交关系、分支独有提交和改动文件整理。

## 怎么看状态

- **未并入**：分支仍有 Git 判定为 `main` 不包含的提交。先检查、测试或补 PR，不要直接删除。
- **主线已有对应成果**：提交图仍显示未并入，但 `main` 已有对应的 squash/PR 成果；删除前做一次最终 diff 核对。
- **已并入**：分支尖端已经是 `main` 的祖先，代码通常可安全清理；若它还承担部署或审计留档用途，则可以保留。
- `agent/`、`claude/`、`cursor/` 表示主要由哪类编码代理创建；随机后缀只是任务标识，不代表功能。

## 需要关注：40 个未并入分支

### Agent / Codex

| 分支 | 用途 | 最后活动 | 建议 |
|---|---|---:|---|
| [`agent/always-on-desire-prompt`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/always-on-desire-prompt) | 强化“主动欲望”人格提示，仅改 `CLAUDE.md` | 2026-07-12 | 很旧且落后主线较多，人工判断人格文案是否仍需要 |
| [`agent/audit-vps-empty-c201b45`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/audit-vps-empty-c201b45) | 记录 VPS 空备份提交 `c201b45` 的审计恢复信息 | 2026-07-16 | `main` 已有对应审计 PR #79；删除前核对记录文件 |
| [`agent/system-config-migration`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/system-config-migration) | 把系统配置页迁移到 React Dash，涉及设置页、路由和配置 API | 2026-07-13 | 后续已有 handoff/review 分支进入主线，检查是否仍有独有 UI |

### Claude

| 分支 | 用途 | 最后活动 | 建议 |
|---|---|---:|---|
| [`claude/affectionate-einstein-6twx4c`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/affectionate-einstein-6twx4c) | Android `ForegroundService`，用常驻通知降低应用被后台清理的概率 | 2026-06-24 | 与主线分叉很早，只提取需要的 Android 提交 |
| [`claude/api-tools-models-debug-qhx4p0`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/api-tools-models-debug-qhx4p0) | 模型切换、Relay 兼容、代码块可视化和聊天消息导出 | 2026-06-29 | 混合了三类功能，建议拆分核对，不要整体合并 |
| [`claude/audit-empty-backup-commit`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/audit-empty-backup-commit) | 为部署恢复确认一次空的自动备份提交 | 2026-07-14 | 仅审计记录；与其他 recovery 分支一起核对后清理 |
| [`claude/cc-resident-process`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/cc-resident-process) | 把 `/chat` 的一次性 Claude Code 调用改为常驻进程 | 2026-07-14 | 与当前 `cc_resident.py` 对比后决定是否补合并 |
| [`claude/chat-frontend-design-t2gtjy`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/chat-frontend-design-t2gtjy) | 支持 VPS 采集器使用长效 `CLAUDE_CODE_OAUTH_TOKEN` | 2026-07-15 | `main` 已有同标题成果，按 squash 合并处理并最终核对 |
| [`claude/chat-pagination-infinite-scroll-umcqu5`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/chat-pagination-infinite-scroll-umcqu5) | 实际内容是“年轮”方案：欲望账本、镜子证据卡、日历/经期/待办/记账改版 | 2026-07-06 | **分支名与内容不符**，且功能混杂；只按提交挑选 |
| [`claude/contacts-codex-chat`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/contacts-codex-chat) | 通讯录入口、Codex 独立聊天页和 Codex 订阅卡 | 2026-07-14 | 与已并入的通讯录/群聊分支比较，保留独有的 Codex 页面 |
| [`claude/cross-surface-memory`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/cross-surface-memory) | 让 `/chat` 与群聊中的 Claude 房间共享记忆 | 2026-07-14 | 单文件网关改动，适合单独审查 |
| [`claude/dream-scenes`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/dream-scenes) | 根据梦境内容为卡片选择场景背景 | 2026-07-14 | 主线已有 PR #70 对应成果；最终 diff 后可清理 |
| [`claude/dream-scenes-fix`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/dream-scenes-fix) | 改用设计稿真实的 6 个梦境场景并修复 hover | 2026-07-14 | 主线已有 PR #71 对应成果；最终 diff 后可清理 |
| [`claude/dream-system-template-issue-h34d38`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/dream-system-template-issue-h34d38) | 修复梦境生成退化为模板文案 | 2026-07-17 | 主线已有 PR #81 对应成果；最终 diff 后可清理 |
| [`claude/lesson-candidates-v2-21tt6t`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/lesson-candidates-v2-21tt6t) | 大型实验线：读书工具、命令倒计时、淘宝登录/下单、图库与照片记忆 | 2026-07-05 | 29 个独有提交且历史很旧，按子功能拆分提取 |
| [`claude/moments-design-parity`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/moments-design-parity) | 让朋友圈页面更贴近 Fyodor Moments 设计稿 | 2026-07-14 | 与后续 Moments PR1–PR4 对比，可能已被覆盖 |
| [`claude/moments-page`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/moments-page) | 初版“Fyodor 的朋友圈”页面和后端入口 | 2026-07-13 | 后续已有完整 Moments 系列，优先判断是否被取代 |
| [`claude/recover-daterail`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/recover-daterail) | 从 VPS 独有提交恢复朋友圈时间线布局 | 2026-07-14 | 与 `moments-feed-layout` 和现主线比较，可能重复 |
| [`claude/token-refresh-fix`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/token-refresh-fix) | 让 `token_refresh.py` 主动刷新过期 OAuth token | 2026-07-13 | 独立小修复，建议优先审查是否仍缺失 |

### Cursor

| 分支 | 用途 | 最后活动 | 建议 |
|---|---|---:|---|
| [`cursor/cache-api-restore-20260710`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/cache-api-restore-20260710) | 恢复 WebView Dash、Pocket 修复和相关缓存/API 改动 | 2026-07-10 | 恢复链分支，先与当前部署代码比较 |
| [`cursor/codebase-tools-8c8e`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/codebase-tools-8c8e) | 混合开发线：代码库工具、修复 worker、聊天游标分页、记忆升级和监控 | 2026-07-06 | 7 个独有提交且跨度大，只按提交挑选 |
| [`cursor/dash-nav-cover-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/dash-nav-cover-c025) | 统一 Dash 底栏尺寸，并接入朋友圈封面上传 | 2026-07-13 | 封面能力主线已有后续实现，检查 UI 差异 |
| [`cursor/fix-fullscreen-scroll-white-nav-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/fix-fullscreen-scroll-white-nav-c025) | 隐藏系统配置页滚动条 | 2026-07-14 | 与 `recover-hide-scrollbar-config` 疑似重复 |
| [`cursor/fix-mobile-fullscreen-layout-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/fix-mobile-fullscreen-layout-c025) | 用 fixed 贴底修复移动端全屏页底部空白 | 2026-07-13 | 检查现有 App/Chat/Moments 布局是否已覆盖 |
| [`cursor/geo-location-fix-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/geo-location-fix-37b3) | 将“家”位置吸附半径缩小到 500 米 | 2026-07-08 | 单文件小修复，可独立审查 |
| [`cursor/memory-dedup-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/memory-dedup-37b3) | 在记忆库与写入流程中去除语义重复项 | 2026-07-08 | 与 memory-weight 分支有重叠，先确认最终算法 |
| [`cursor/memory-library-fixes-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/memory-library-fixes-37b3) | 把 `CLAUDE.md` 同步到 HayaGarden 主人格文档 | 2026-07-07 | 人格文案可能已演进，人工逐段比较 |
| [`cursor/memory-upgrade-8c8e`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/memory-upgrade-8c8e) | 记忆联想召回、长期事实、日摘要、向量脚手架、监控和 Relay fallback | 2026-07-05 | 多功能基础分支，可能是后续分支的祖先；不要整体合并 |
| [`cursor/memory-weight-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/memory-weight-37b3) | 调整核心/短期/收件箱记忆层级权重，并包含去重修复 | 2026-07-08 | 7 个独有提交，需和当前记忆数据模型一起测试 |
| [`cursor/moments-feed-layout-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/moments-feed-layout-c025) | 朋友圈时间线布局调整 | 2026-07-14 | 与 recover-daterail/新版 Moments 可能重复 |
| [`cursor/moments-pr1-feed-d521`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/moments-pr1-feed-d521) | Moments PR1：统一 feed、稳定 ID、游标分页及时间边界修复 | 2026-07-16 | 主线已有 PR #76 对应成果；最终 diff 后可清理 |
| [`cursor/moments-pr2-three-source-d521`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/moments-pr2-three-source-d521) | Moments PR2：三源 feed、聊天收藏、转发卡片和分页修复 | 2026-07-16 | 主线已有 PR #77 对应成果；最终 diff 后可清理 |
| [`cursor/moments-pr3-interactions-d521`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/moments-pr3-interactions-d521) | Moments PR3：封面、社交互动、删除、写接口鉴权与测试 | 2026-07-16 | 主线已有 PR #78 对应成果；最终 diff 后可清理 |
| [`cursor/moments-pr4-mood-tools-d521`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/moments-pr4-mood-tools-d521) | Moments PR4：情绪历史、手动修正、工具抽屉开关和审查修复 | 2026-07-16 | 主线已有 PR #80 对应成果；最终 diff 后可清理 |
| [`cursor/remove-workspace-aipanel-8c8e`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/remove-workspace-aipanel-8c8e) | 移除 Workspace/AI 协作面板；历史还带有记忆升级与监控提交 | 2026-07-06 | 混合历史，只考虑最上层移除提交 |
| [`cursor/setup-dev-env-fb61`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/setup-dev-env-fb61) | 为云端开发环境补 `AGENTS.md` 和 `requirements.txt` | 2026-07-09 | 与另一个 setup 分支二选一，结合当前依赖核对 |
| [`cursor/setup-dev-environment-5662`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/setup-dev-environment-5662) | 较早的云端开发 `AGENTS.md` 配置 | 2026-07-05 | 多半被 `setup-dev-env-fb61` 取代，比较后清理 |
| [`cursor/vps-local-ui-fixes-20260710`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/vps-local-ui-fixes-20260710) | 保存 VPS 上的 WebView Dash 与 Pocket 修复 | 2026-07-10 | 与 `cache-api-restore` 同属恢复链，检查部署差异 |

### 其他 / 恢复分支

| 分支 | 用途 | 最后活动 | 建议 |
|---|---|---:|---|
| [`frontend`](https://github.com/Hayana11/hayagarden-frontend/tree/frontend) | 最早的 Fyodor Dash 前端设计交接与实现 | 2026-07-06 | 与 `main` 没有共同祖先；仅作设计/历史参考，不要直接合并 |
| [`recover-hide-scrollbar-config`](https://github.com/Hayana11/hayagarden-frontend/tree/recover-hide-scrollbar-config) | 恢复“隐藏系统配置页滚动条”修复 | 2026-07-14 | 与 Cursor 同名修复分支疑似重复，比较后只留一个 |

## 通常可清理：35 个已并入分支

下表中的分支尖端都已经是 `main` 的祖先。若没有部署脚本、外部服务或审计流程引用这些分支，删除远程分支不会丢失代码。

### Agent / Codex

| 分支 | 已完成的用途 |
|---|---|
| [`agent/audit-production-empty-commit`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/audit-production-empty-commit) | 审计生产环境空备份提交 |
| [`agent/channel-intelligence`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/channel-intelligence) | Relay 价格、状态、余额洞察 |
| [`agent/claude-codex-quota`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/claude-codex-quota) | Claude/Codex 配额跟踪与百分比语义修复 |
| [`agent/codex-app-server`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/codex-app-server) | 聊天接入 Codex app server |
| [`agent/fix-context-continuity-deploy-guard`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/fix-context-continuity-deploy-guard) | 上下文连续性、fallback 声明和部署健康重试测试 |
| [`agent/group-chat`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/group-chat) | Claude/Codex 双代理群聊房间 |
| [`agent/guard-production-divergence`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/guard-production-divergence) | 生产分叉检测与恢复说明 |
| [`agent/recover-vps-af86ca5`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/recover-vps-af86ca5) | VPS 空备份提交恢复确认 |
| [`agent/recover-vps-features-runtime`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/recover-vps-features-runtime) | 恢复 VPS 功能运行时并修复 Dashboard probe 类型 |
| [`agent/relay-account-vault`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/relay-account-vault) | 加密保存 Relay 账户余额信息 |
| [`agent/relay-vault-review-fixes`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/relay-vault-review-fixes) | Relay vault 审查问题修复 |
| [`agent/system-config-handoff`](https://github.com/Hayana11/hayagarden-frontend/tree/agent/system-config-handoff) | 系统配置页迁移后的审查与交接修复 |

### Claude

| 分支 | 已完成的用途 |
|---|---|
| [`claude/design-package-repo-access-jntvu3`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/design-package-repo-access-jntvu3) | 修复“可能与你有关”的话题相关性计算 |
| [`claude/focused-mayer-y3r7hz`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/focused-mayer-y3r7hz) | 拆分 BP3 动态上下文以提高 system prompt 缓存命中 |
| [`claude/invoice-page-review-9u5h5a`](https://github.com/Hayana11/hayagarden-frontend/tree/claude/invoice-page-review-9u5h5a) | 按设计稿加入 `/dash/chat` Fyodor Chat 页面 |

### Cursor

| 分支 | 已完成的用途 |
|---|---|
| [`cursor/chat-gen-lock-fix-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/chat-gen-lock-fix-37b3) | 流结束无事件时正确结束 live think UI |
| [`cursor/contacts-nav-group-chat-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/contacts-nav-group-chat-c025) | 通讯录底栏、群聊简化和文件上传 |
| [`cursor/curwe-workspace-app-p4-8360`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/curwe-workspace-app-p4-8360) | Workspace App 运行时与目录归属修复（PR4） |
| [`cursor/curwe-workspace-job-p2-8360`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/curwe-workspace-job-p2-8360) | Workspace 后台任务和项目目录创建（PR2） |
| [`cursor/curwe-workspace-sandbox-p1-8360`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/curwe-workspace-sandbox-p1-8360) | Workspace 沙箱与安全 Git diff（PR1） |
| [`cursor/curwe-workspace-tools-p3-8360`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/curwe-workspace-tools-p3-8360) | Workspace 自定义工具注册、权限和锁（PR3） |
| [`cursor/dash-chat-bottom-nav-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/dash-chat-bottom-nav-c025) | 为 `/dash/chat` 添加底部导航 |
| [`cursor/hide-scrollbar-moments-chat-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/hide-scrollbar-moments-chat-c025) | 隐藏 Moments 与 Chat 页面滚动条 |
| [`cursor/memory-search-trash-fixed-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/memory-search-trash-fixed-37b3) | 固定记忆垃圾桶按钮和筛选条滚动 |
| [`cursor/memory-star-pinch-filters-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/memory-star-pinch-filters-37b3) | 记忆星图双指缩放与搜索筛选 |
| [`cursor/pocket-p0-deploy-2a09`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/pocket-p0-deploy-2a09) | Pocket 专用 WebView 安全边界与部署文档 |
| [`cursor/pocket-p2-gateway-2a09`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/pocket-p2-gateway-2a09) | Pocket 网关和手机浏览器在线状态上下文 |
| [`cursor/recover-lost-features-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/recover-lost-features-37b3) | 恢复丢失功能并更新 Service Worker 缓存策略 |
| [`cursor/repair-unlock-gen-8360`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/repair-unlock-gen-8360) | Repair 页面强制释放聊天生成锁 |
| [`cursor/serve-frontend-react-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/serve-frontend-react-37b3) | React 前端服务与白夜梦境/想法渲染加固 |
| [`cursor/tool-drawers-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/tool-drawers-37b3) | 运行时开关控制工具抽屉路由 |
| [`cursor/usage-calendar-cost-c025`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/usage-calendar-cost-c025) | 用量日历显示金额并禁用 index 缓存 |
| [`cursor/wake-chat-tools-37b3`](https://github.com/Hayana11/hayagarden-frontend/tree/cursor/wake-chat-tools-37b3) | 长时间沉默后要求 wake 主动探索工具 |

### 其他 / 恢复分支

| 分支 | 已完成的用途 |
|---|---|
| [`fyodor/restore-file-choices-rolling-shop-202607051120`](https://github.com/Hayana11/hayagarden-frontend/tree/fyodor/restore-file-choices-rolling-shop-202607051120) | 保存并恢复商城浏览/操作/结账/登录与文件选择工作 |
| [`recover/auto-backup-2026-07-12`](https://github.com/Hayana11/hayagarden-frontend/tree/recover/auto-backup-2026-07-12) | 2026-07-12 自动备份恢复点 |

## 推荐的整理顺序

1. 先删除上面 35 个“已并入”分支；保留确实承担审计/恢复用途的分支。
2. 对标为“主线已有对应成果”的分支执行一次 `git diff origin/main...origin/<branch>`，确认只剩 squash 造成的历史差异后删除。
3. 优先审查几个独立小修复：`token-refresh-fix`、`geo-location-fix-37b3`、`cross-surface-memory`。
4. 对 `lesson-candidates-v2-21tt6t`、`codebase-tools-8c8e`、`memory-upgrade-8c8e` 这类混合历史分支，只 cherry-pick 明确需要的提交。
5. 最后再处理 `frontend`；它没有与当前主线共享的 Git 祖先，应当视为设计归档，而不是普通功能分支。

> 维护规则：新开分支时，在 PR 标题或首个提交里写清“功能 + 范围”；合并 PR 后立即删除远程分支。这份表只负责导航，不替代 PR 和提交记录。
