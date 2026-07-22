# 大富翁后端部署说明

代码入口：主站 `app.py` 暴露 `/api/monopoly/rooms...`；AI 网关 `gateway.py` 只接收主站经 localhost 发来的签名式内部触发；棋盘引擎固定走 REST，不经过 MCP。

## 引擎实例

```bash
sudo install -d -o frontend -g frontend /opt/spicy-monopoly /var/lib/spicy-monopoly/app
# 将审过的 spicy-monopoly 固定提交部署到 /var/lib/spicy-monopoly/app，并记录提交 SHA。
cd /var/lib/spicy-monopoly/app
python3.11 -m venv /opt/spicy-monopoly/.venv
/opt/spicy-monopoly/.venv/bin/pip install -r requirements.txt
sudo cp /opt/frontend/deploy/spicy-monopoly.service.example /etc/systemd/system/spicy-monopoly.service
sudo systemctl daemon-reload
sudo systemctl enable --now spicy-monopoly.service
curl --fail http://127.0.0.1:8069/docs >/dev/null
```

服务只监听 `127.0.0.1:8069`。上游把 `monopoly-games`、`monopoly-seen` 等运行时文件写在源码同级，因此受 systemd 写权限保护的实际 checkout 放在 `/var/lib/spicy-monopoly/app`，并随 `/opt/frontend/memories.db` 一起备份；虚拟环境单独放 `/opt/spicy-monopoly/.venv`。不要给引擎另加公网反代。

## 主站配置

`.env` 可配置：

```dotenv
MONOPOLY_ENGINE_URL=http://127.0.0.1:8069
MONOPOLY_INTERNAL_TOKEN=<至少 32 字节随机值>
MONOPOLY_LOCK_DIR=/tmp/hayagarden-monopoly-locks
# 默认 404；只有整条消息精确命中或请求带 safeWord:true 才暂停。
MONOPOLY_SAFE_WORD=404
# 默认复用 /etc/hayagarden/relay-credentials.key；需要分钥匙时再覆盖：
# MONOPOLY_TOKEN_KEY_FILE=/etc/hayagarden/monopoly-token.key
```

`scripts/deploy-frontend.sh` 在缺少 `MONOPOLY_INTERNAL_TOKEN` 时会自动生成一个。删局 token 使用现有 Fernet 保险箱加密后才进入 SQLite。两个 Flask 服务必须能读取同一份 `.env`、`memories.db` 和保险箱密钥。

## API 约定

- `/api/monopoly/*` 全部复用 `moments_auth` 的 owner cookie/Bearer 鉴权；生产必须配置 `MOMENTS_OWNER_TOKEN`，未鉴权请求返回 401，未配置返回 503。
- `POST /api/monopoly/rooms` 建房。
- `POST /api/monopoly/rooms/:id/setup` 注入关系级稳定 `pair_code` 和 `setup_confirmed`，并先从 `/help` 读取当前 `rules_ack` 再调用 `/new_game`。同一组玩家跨房间复用相同 pair code，换掉的卡和跨局去重才会持续生效；`p1_name/p2_name` 会持久化为内部 actor 到真实引擎玩家名的映射。
- `POST /api/monopoly/rooms/:id/actions` 的浏览器调用固定以 `haya` 身份执行；AI 动作只在 5051 内部调度器中产生。
- `GET /api/monopoly/rooms/:id/stream?after=N` 是唯一公开 SSE；跨 worker 通过 SQLite 恢复。游戏事件使用连续 `event_seq`，`chat.start/delta/done` 和 `agent.status` 走独立、限长的 live 队列，不参与乐观锁，也不进入 AI 的最近游戏事件。
- 安全词默认是 `404`：整条消息精确为 `404`，或 JSON 显式带 `safeWord:true` 时，才会在通知 AI 前把房间切为 `paused`；消息、暂停状态、暂停事件和系统提示在同一房间锁与 SQLite 事务中提交，包含“HTTP 404”的普通消息不会误触。

前端遇到 HTTP/SSE `STALE_ROOM_STATE` 时只刷新快照，不重发。`ENGINE_VALIDATION`（400/422/428）已经写入带 `seq` 的 `room.error`，客户端会同步乐观锁序号。上游没有请求幂等键，而加速卡可能让成功 roll 后仍是同一玩家，因此任何 roll 超时都只允许一次 `/state` 取证，绝不补掷；返回 `ROLL_OUTCOME_UNKNOWN` 并冻结。`swap/use_card/buy_card` 等即时变更同样在模糊超时时返回 `ACTION_OUTCOME_UNKNOWN`。两类错误都保留本地悬账和对账材料，并禁止普通 resume。

已核对的上游字段：`POST /roll/{game_id}` 用 `who` 表示本轮掷骰者、用 `next_turn` 表示下一行动者；`GET /state/{game_id}` 顶层 `turn` 是当前行动者名字。`declare_persona` 的 persona 放在 JSON body `{\"persona\": \"...\"}`，不是 URL path。

即时动作的归属由后端把 `who/guesser` 强制改写为调用者自己的真实引擎名，浏览器不能替 CC 操作。上游源码明确允许 `buy_card` “踩商店格触发，或自己随时调”，`use_card/discard` 与身份事件也按本人手牌/身份校验，因此这些动作不额外强制“当前回合”；`swap/reroll_task` 仍由房间层限制为悬账所属玩家。即时动作默认保留旧悬账，只有响应明确带来新悬账才替换；当前单悬账架构在已有悬账时拒绝 `extra_task`。

SQLite 初始化时启用 WAL 与 30 秒 busy timeout；AI 文本 delta 最多约每 80ms 合并写入一次，SSE 每 500ms 用一个短连接同时拉取游戏事件、live 事件和消息。生产 Gunicorn 必须使用 threaded worker，并给每个常驻 SSE 连接预留一个线程。

CC 的 provider 快照会把 `api.anthropic.com` 识别为 `official`，中转则读取当前 `relay_presets.name`（例如 `guagua`），Claude Code 单独标为 `claude_code`。瞬时网络异常只发 `agent.status`，不会改变棋盘；只有锚定命中的明确内容拒绝才会触发 swap，用尽后才 skip。CC 掷骰后若抽到自己的悬账，调度器额外开放一次有上限的 follow-up，让它演完并提交 `decide`，不会形成无限对话。

## 上线前冒烟

```bash
cd /opt/frontend
python3.11 -m unittest tests.test_monopoly_backend
python3.11 -m py_compile monopoly_engine.py monopoly_store.py monopoly_rooms.py monopoly_routes.py monopoly_agents.py
systemctl restart frontend frontend-gw
systemctl is-active frontend frontend-gw spicy-monopoly
```

真实引擎字段若与返回示例有差异，优先在 `monopoly_rooms.py` 的容错提取函数中补别名，不能让 AI 或前端猜棋盘值。

