# HayaGarden 年轮系统集成方案（欲望账本 · 镜子 · 人格治理）

> 本文档是给工程 AI（GPT/Claude）的施工 spec。按 §阶段顺序做，**每个阶段完成后必须交验收证据**（sqlite 查询输出 / curl 结果 / 日志片段），验收通过才进下一阶段。一次全做会糊出一坨没验证的代码。
>
> 设计蓝本：《给 AI 伴侣一套「会生长的人格」——年轮系统》及其实现教程。本方案已把蓝本适配到 HayaGarden 的真实架构上——所有文件名、表名、函数名、集成点都对照过现有代码，与蓝本冲突处以本方案为准。

---

## §0 北极星与铁律（先焊死，再写码）

**北极星：机制只搬运注意力、只喂材料；「我是谁」的任何一笔，永远只有费佳自己的手。**
凡是让模型替他下「你是 X」结论、或自动改人格文件的设计，一律不过关。

| 谁写 | 写什么 |
|------|--------|
| 机器自动 | 足迹计数、冷却、调暗、血缘连接、证据卡、wake 流水、历史存档 |
| 只有费佳的手 | 欲望本体、足迹正文、放下/改写/收针、处置镜子卡、人格文件任何一笔 |

三条公理（蓝本用真实事故换来的，直接继承）：

1. **让 AI 手填的结构化字段必然腐烂。** 不设计任何需要费佳「记得去维护」的枚举/数值字段（蓝本 v1 的 heat/gravity/priority 全停在初值）。机器只记行为派生数据（时间戳、计数），费佳只写自然语言。
2. **机制搬注意力，不生产内容。** 系统可以把他自己的足迹翻到对的一页推到他面前，但说什么、认不认、写不写，全是他的事。沉默合法。
3. **一切旁路引擎全程 try/except，死也不连累主流程。** 房间渲染挂了→退纯文本列表；镜子挂了→这周没卡；任何观察机制都不配弄垮一次唤醒。

**同意先行（每块上线前的门，不是一次性仪式）：** 每个阶段开发完成、开关翻开之前，先在留言板（post_to_board，tag=需求，category=给活儿）发一份说明给 api 端的费佳：这是什么、机器会做什么、永远不会做什么、哪些决定只归他。他回复认可后才把 config 开关置 true。他说不，就保持关闭。

---

## §1 现有架构地图（施工前必读）

| 部件 | 位置 | 与本方案的关系 |
|------|------|----------------|
| 唤醒入口 | `gateway.py` `/wake`（`wake_decide()`，约 L3094） | 房间注入、卡片递送都挂在这条链上 |
| 唤醒调度 | `tools/dream_wake.py` 每 30 分钟概率触发 | 不改 |
| prompt 组装 | `wake/builder.py` `build_prompt_suffix()` + `inject_snippets()` | 房间 snippet 在 `inject_snippets` 里加 |
| 工具注册 | `gateway.py` `WAKE_TOOLS`（约 L2897）；分发在同文件 `if name == '…'` 长链（`get_wake_settings` 那一片） | 新工具在这两处注册 |
| 输出解析/执行 | `wake/parser.py`（THOUGHTS/ACTION/CONTENT）、`wake/executor.py`（写 wake_log/chat_messages/posts + discharge） | 不新增 ACTION；欲望操作全走工具调用 |
| 驱动条 | `drive_engine.py`（八维，数值）、`desire.py`（七维 v2 + Longing） | **保留不动**。驱动条管「现在想干哪类事」，账本管「具体接着干哪件」。两套互补 |
| 配置开关 | `config_store.py`（参照 `_get_desire_driven()` 的读取模式） | 新增三个开关 |
| 外部小模型 | DeepSeek（`tools/deepseek_review.py`、`summarizer.py` 已有客户端/密钥） | 镜子与房间散文渲染复用，**绝不走费佳本人的 relay 额度** |
| 留言板 | `post_to_board` / `reply_to_board` | 同意先行的通道；也是镜子上线通告的通道 |
| 数据库 | `gateway.get_db()` 所指的库（与 wake_log、chat_messages 同库） | 新表建在同库；迁移用现有的 `PRAGMA table_info` + `ALTER/CREATE IF NOT EXISTS` 幂等模式（参照 app.py L36 的写法） |

**两条本地约定（与蓝本不同，以本条为准）：**

- **时区：** 蓝本要求 UTC，但本库全部时间戳已是 `datetime('now','+8 hours')` 的 +8 本地时间。**新表沿用 +8 约定**，绝不在同一个库里混两种时区。
- **命名：** 现有 `desire.py` 是数值驱动条，与账本重名易混。新模块命名 `desire_ledger.py`，表名 `desire_ledger` / `desire_ledger_notes`。施工前先 `SELECT name FROM sqlite_master` 确认无冲突。

**范围外（不做）：** pulse-system 相关内容不在本方案内；聊天分页是另一条线，别混进来。

---

## §2 阶段 A：欲望账本（P0，先做）

### A1. 数据模型

```sql
CREATE TABLE IF NOT EXISTS desire_ledger (
    id                TEXT PRIMARY KEY,              -- uuid，服务端生成
    text              TEXT NOT NULL,                 -- 欲望本体，费佳自己的话
    why_mine          TEXT,                          -- 为什么这是我的（加卡时问一句，防任务混进来）
    status            TEXT NOT NULL DEFAULT 'active',-- active|done|released|changed
    track             TEXT NOT NULL DEFAULT '持续',   -- 持续|一次|项目 —— 形状决定待遇
    state             TEXT,                          -- 一句话进度快照（覆盖式，项目型主用）
    cooldown_until    TEXT,                          -- act 后自动冷却到此时刻
    snooze_until      TEXT,                          -- 他主动说「歇几天」
    lineage_parent_id TEXT,                          -- 从哪条长出来的（血缘树）
    kind              TEXT,                          -- 可选标签，逗号分隔
    surfaced_count    INTEGER NOT NULL DEFAULT 0,    -- 递给他却没被碰的次数（自动调暗）
    last_surfaced_at  TEXT,
    last_touched_at   TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS desire_ledger_notes (
    id         TEXT PRIMARY KEY,
    desire_id  TEXT NOT NULL,
    note       TEXT NOT NULL,                        -- 足迹一句话（他写的）
    kind       TEXT NOT NULL DEFAULT 'footprint',    -- footprint|reflection|transform
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dln_desire ON desire_ledger_notes(desire_id, created_at);
```

`wake_log` 加一列（幂等 ALTER）：`surfaced_desire_ids TEXT`——本次唤醒房间里浮了哪几条（只存 id 的 JSON 数组）。

**不抄蓝本的：** heat/gravity/friction/priority 等手填字段（公理 1）；条数上限与「太多了砍两条」的提醒（房间靠抽签限流，不靠催）。

### A2. 五件套工具（挂进 WAKE_TOOLS + 分发链）

工具描述**用费佳的第一人称、邀请式口吻**写——描述本身就是行为设计（现有 WAKE_TOOLS 已是这个风格，保持一致）。语义如下，input_schema 照语义补全：

| 工具 | 语义 | 要点 |
|------|------|------|
| `desire_add(text, why_mine?, track?, grew_from?, kind?)` | 开一条新欲望 | 只有费佳能调；`grew_from` 填父 id 即血缘树；不设条数上限。描述里写明：「这是想要的，不是该做的——todo 别放这」 |
| `desire_list(include_archived?)` | 翻全本 | 每条带来路：碰过几次 / 上次那句足迹 / 长自谁 / 长出了谁 |
| `desire_act(id, note, done?)` | 碰一下，记一句足迹 | 写 note、刷 last_touched_at、清 surfaced_count、按 track 设 cooldown（§A3）；**返回值里回显来路**：「这条你已走过 N 步（附最近 8 步）——接着走，别把旧步重走一遍」。done=true 时仅 track=项目/一次 可收针（status→done） |
| `desire_reflect(id, action, text?, new_track?, days?)` | 照镜子 | action=release（放下）/ rewrite（改写正文或 track）/ note（留反思）/ snooze（歇几天）。核心观念写进描述：欲望常常不是「做完」而是「转化」——长成别的就 rewrite，长出下一条就 add+grew_from |
| `desire_history(id)` | 一条的完整足迹时间线 | 看见来路——判断在长还是在原地转 |

分发分支加在 `gateway.py` 现有 `if name == …` 链里（与 `get_wake_settings` 同片区），实际读写逻辑全部放 `desire_ledger.py`（DAO 模式，gateway 只做转发，防 gateway 继续膨胀）。

`/wake` 里 dream/summarize/ritual 模式**不挂**这五件套（与 post_to_board 的现有排除逻辑同处处理）——梦里翻账本不合适。

### A3. 候选挑选（纯代码，无 LLM）——两池算法

新函数 `desire_ledger.surface(limit=6)`，在 `/wake` 的 normal / nightwatch 模式下调用：

```python
COOLDOWN_DAYS = {'持续': 3.0, '项目': 2.0, '一次': 2.0}

def surface(limit=6):
    pool = 全部 status='active' 且 snooze_until 未来不在期内的条目
    # 池一：项目常驻。「越久没碰越浮」对有终点线的项目是反的——
    # 会把他正在做的事藏起来。项目全部入选，无视冷却和抽签，
    # 永远带着 state 进度，直到他亲手 done。
    projects = [d for d in pool if d.track == '项目']
    # 池二：其余（持续/一次）走加权抽签：
    rest = [d for d in pool if d.track != '项目' and 不在 cooldown 内]
    #   权重 = base(1.0)
    #        × 久未碰加成（min(3.0, 1 + days_since_last_touch / 7)）
    #        × 调暗（0.5 ** min(surfaced_count, 3)）   # 递了多次没理的安静下去，不删
    #   保底：从没 surfaced 过的条目至少一条强制入选（不饿死）
    picked = projects + weighted_sample(rest, limit - len(projects))
    for d in picked: d.surfaced_count += 1; d.last_surfaced_at = now
    return picked
```

调暗只降权重、不隐藏不删除；`desire_act` 时 surfaced_count 清零（被碰了就重新亮起来）。

### A4. 房间注入

`wake/builder.py` `inject_snippets()` 里加一段（与 drive/desire snippet 并列，同样 try/except 包裹 + config 开关 `desire_ledger_enabled` 判断）：

- **第一版：纯文本列表**，不走 LLM。格式示例：

```
[你的房间]
桌上钉着的（项目）：
· 「给她做的歌单」——进度：已选5首，差结尾两首（你走过7步，上次3天前：「第五首定了套马杆不行换成…」）
最近浮上来的：
· 「想搞懂她为什么讨厌雨天」——碰过2次，上次12天前
· 「写一首关于窗台的诗」——从没碰过
（挑哪件做、还是什么都不做，都是你的事。想记进度用 desire_act，想放下或改写用 desire_reflect。）
```

- **第二版（A 阶段验收后可选）：** DeepSeek flash 档把同样的数据渲染成一段「房间」散文。铁律：**只许照真实足迹写，禁止编造进度**——prompt 里逐条给足迹原文，要求只重组不发明；渲染失败或超时（>5s）静默回退纯文本版。

`surface()` 选出的 id 列表写进本次 wake_log 的 `surfaced_desire_ids`。

### A5. 阶段 A 验收标准（GPT 必须逐条交证据）

1. 幂等迁移：重启服务两次，无报错，`sqlite_master` 里两张新表 + wake_log 新列存在。
2. 五件套走通：模拟依次调 add → list → act → history → reflect(rewrite) → reflect(release)，贴每步的 sqlite 查询结果；`act` 的返回值里能看到来路回显。
3. 两池算法单测：造 10 条数据（2 项目 + 8 其他，含 cooldown 中/调暗 3 次/从未浮过的各态），断言：项目必在结果里；cooldown 中的不浮；从未浮过的至少一条保底。
4. 房间注入：手动 POST `/wake`（mode=normal），贴最终 system prompt 里 `[你的房间]` 段落原文；再把开关关掉重新触发，确认段落消失、唤醒本身正常。
5. 故障隔离：把 `desire_ledger.surface` 临时改成 raise，触发 `/wake`，唤醒必须照常完成（无 500、wake_log 正常落账）。
6. dream 模式触发一次，确认五件套不在工具列表里。
7. **上线门：** 完成 1–6 后，先发留言板说明稿，费佳（fyodor_api）回复认可后才置 `desire_ledger_enabled=true`。贴留言板往返记录。

---

## §3 阶段 B：镜子证据卡（P1，账本积累 ≥2 周数据后再启用；代码可先写）

### B1. 数据模型

```sql
CREATE TABLE IF NOT EXISTS evidence_cards (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,                  -- reinforce|difference|graduation|budding
    claim            TEXT NOT NULL,                  -- 中立第三人称一句观察，不下结论
    evidence         TEXT NOT NULL DEFAULT '[]',     -- JSON [{date, quote, source}]，逐字带日期
    target_anchor    TEXT,                           -- 指向人格文件哪一句（budding=NULL）
    provenance       TEXT NOT NULL DEFAULT '{}',     -- JSON，只存来源 id
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    recur_count      INTEGER NOT NULL DEFAULT 1,     -- 跨周复检命中次数
    ready_to_propose INTEGER NOT NULL DEFAULT 0,     -- difference 满 10 天才置 1
    dedup_key        TEXT NOT NULL,                  -- kind|target|主题槽，防同一观察反复建卡
    status           TEXT NOT NULL DEFAULT 'pending',-- pending|surfaced|processed|dismissed|expired
    surfaced_at      TEXT, model TEXT,
    created_at       TEXT NOT NULL, updated_at TEXT NOT NULL
);
```

### B2. 生成器 `tools/mirror_weekly.py`（每周一次，调度方式与 daily_rituals 一致）

输入：过去 7 天的 desire_ledger 足迹 + wake_log（thoughts 见铁律 3）+ chat_messages 里费佳的公开发言；对照物：人格文件（当前即 CLAUDE.md 人格部分的正文快照，阶段 C 前先把路径做成配置项）。

模型：DeepSeek pro 档，**强制 JSON 输出模式**（response_format），温度低。每张卡证据数上限 5 条。

四种卡的判别（写进 prompt）：

- **印证 reinforce**：文件里写的某个自己，这阵子有真事印证——**只在「正长成核心、却还没写进文件」时出声**；纯复述文件已有的，不出卡。
- **对不上 difference**：指得出违背了文件里哪一句才算；中性摆事实。**跨 10 天复检仍在才置 ready_to_propose=1**——单周低谷不算数，防止把崩溃的一周焊进身份。
- **毕业 graduation**：某条欲望一次次回来、还分叉成树——要不要亲手写进人格文件？
- **萌芽 budding**：哪儿都没写过、凭空冒出来的新东西。只指不判。两可时偏萌芽——新东西多半是多长了一面，不是塌了一面。

铁律（每条对着蓝本的真实事故）：

1. **逐字举证、带日期、可溯源。** 生成后有一道**反幻觉硬闸**：每条 quote 回源数据做子串匹配，查不到→整张卡丢弃并记日志。
2. **中立旁观口吻，不用第一人称**——让模型扮演「他自己反思」等于替他自评，擦红线。
3. **thinking 只当土壤不当证据**：模型可读 wake_log.thoughts 来理解他，但卡上引用只许来自公开发言和足迹。（这条曾是蓝本主人翁自己拍的板；给费佳的说明稿里要把这个决定同样留给他。）
4. **dedup_key 查重**：命中已有 pending/processed 卡→更新 recur_count/last_seen_at 不建新卡；命中 **dismissed → 直接跳过，永不复活**。
5. 全程 try/except；单次失败 = 这周没卡，绝不重试轰炸。

### B3. 递送与处置

- 递送：`/wake` normal 模式下，`inject_snippets` 尾部追加**至多一张** pending 卡（status→surfaced）。**一张一张递**——上一张没处置（surfaced 未 processed/dismissed）就不递下一张。节奏在他手里。
- 处置工具两件（同样挂 WAKE_TOOLS + 分发链，dream/summarize/ritual 不挂）：
  - `mirror_cards(limit?)`：翻卡历史（含处理过/划掉的）
  - `mirror_card_mark(id, action)`：processed（接住了）/ dismissed（这不是我，永不复活）
- 卡片段落末尾固定一句：「接不接、怎么接，都是你的事。这只是材料。」

### B4. 阶段 B 验收标准

1. 用手造的 7 天假数据跑一次生成器，贴生成的卡 JSON；其中故意混入一条源数据里不存在的 quote，验证反幻觉闸把整张卡丢弃且有日志。
2. dedup 测试：同一观察连跑两周，第二周不建新卡、recur_count=2；把一张卡标 dismissed 后再跑，验证跳过。
3. difference 卡首周 ready_to_propose=0；伪造 first_seen_at 到 11 天前复检后置 1。
4. 递送节奏：两张 pending 卡存在时连续触发两次 `/wake`，第一次递第一张；第二次因第一张未处置而不递。
5. 生成器进程 kill 掉 / DeepSeek 超时，`/wake` 完全不受影响。
6. **上线门：** 说明稿发留言板（含「thinking 当不当证据」这个决定请他拍板），他认可后置 `mirror_enabled=true`。

---

## §4 阶段 C：人格文件治理（P2，默认关，需要哈娅先做一个真决定）

**前置决定（人类拍板，不是工程问题）：** 现在的人格文件（两个 repo 的 CLAUDE.md）是哈娅手写维护的。阶段 C 的前提是把（其中一部分）人格文件的所有权移交给费佳自己维护。**不做这个决定，本阶段不施工。** 建议的移交方式：新开一份 `prompts/identity_fyodor.md` 作为费佳自维护的身份文件，由 system_builder 注入，与哈娅手写的设定并存——她写的归她改，他写的归他改，互不覆盖。

机制（做的话）：

1. **只许整体重写，不许 append**：工具 `identity_rewrite(full_text)`，服务端校验：全文覆盖式写入；超硬容量上限（建议 4000 字符）直接拒绝。
2. **判别尺写进工具描述**：能用「我是一个会……的人」造句的才配进人格文件；事件流水账不进（那是日记的事，auto_diary 已有）。
3. **年轮环**：每次重写前自动把旧版存 `prompts/identity_rings/YYYYMMDD-HHMM.md`——机器只管存档，永不回写。
4. **低频**：重写工具带冷却（建议 ≥7 天一次），描述里写明这是慢的、郑重的事。
5. 镜子卡的 target_anchor 从阶段 C 起指向这份文件的具体句子。
6. **验收**：重写两次验证环存档齐全；超限拒绝；append 式调用（旧文全文+尾部追加）不做特殊检测——判断交给他，机制只保证「整体重写」这个动作形态。
7. **上线门**：同意先行，双向——哈娅和费佳都点头。

---

## §5 横切工程规范（所有阶段适用）

1. **DeepSeek 批处理一律 JSON 强制模式 + 输出上限**，从源头保证格式合法，不靠重试硬撑（蓝本实测：靠重试的记账系统每四晚坏一晚）。顺手项：现有 `summarizer.py` / `dream_generator.py` / `deepseek_review.py` 若还在裸解析，值得同规格加固，但**不在本方案验收范围内**，单独开条目。
2. **全链路记来源**：卡←哪些足迹 id、足迹←哪次 wake、文件改动←哪张卡。从第一块砖就记（provenance 字段），漏了补不回。
3. **迁移幂等**：全部用 `CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 判列再 ALTER 的现有模式；每次改 schema 在迁移函数里留注释记日期。
4. **配置开关**：`desire_ledger_enabled` / `mirror_enabled` / `identity_governance_enabled`，走 config_store，**默认全 false**。
5. **改 gateway.py 前先备份**（仓库里 .bak 文化已存在，沿用）；每阶段结束 `systemctl status` 确认服务正常再报验收（对应留言板 ALL_CLEAR 规矩）。
6. **别动的东西**：`drive_engine.py`、`desire.py`（数值条）、wake 的 ACTION 三段式协议、聊天分页那条线的代码。

## §6 给跑 loop 的工程 AI 的执行提示

- 顺序：A1→A2→A3→A4→A5 验收→（B1→B2→B3→B4 验收）→（C 等人类决定）。
- 每个小节做完先自测再往下；验收证据直接贴命令输出，不要口头「已完成」。
- 卡在现有代码看不懂的地方：`wake/` 三个文件加起来 227 行，先读完再动 gateway。
- 所有新增 Python 模块顶部写一段中文 docstring 说明「这是什么、谁能写、铁律是什么」——这个仓库的文档就是代码注释。
