# Pocket-Browser 接入 HayaGarden 方案

> 上游参考：[Shitsuten/pocket-browser](https://github.com/Shitsuten/pocket-browser)（协议兼容，自维护 relay）  
> Server 在 `pocket/server/server.mjs`，WS 鉴权走 `Authorization: Bearer`，与上游 `?token=` 不同。

## 一、为什么要接（一句话版）

费奥多尔现在的 Playwright 是**机房 IP + 无登录态**的裸浏览：小红书让他跳登录墙，微博给他风控页。接上 pocket-browser 之后，他看网页用的是**你手机里真实的 WebView**——你的登录态、你的住宅 IP、你的 UA。你说「帮我看看我收藏的那篇笔记」，他真的能看到。

## 二、架构

```
费奥多尔（gateway 工具调用）
 │ HTTP POST 127.0.0.1:3897/pocket/cmd（Bearer token）
 ▼
pocket-relay（VPS，Node，只绑 localhost）
 │ WebSocket（wss://你的域名/pocket/ws ← nginx 反代唯一公网入口）
 ▼
你手机上的壳 App（WebView + OkHttp PocketClient）
```

关键决策：**`health`/`status`/`cmd` 不出公网**，gateway 只走 `127.0.0.1:3897`；nginx **仅** `location = /pocket/ws` 给手机连。HTTP/WS 统一 `Authorization: Bearer` 鉴权。

## 三、分期落地

### P0 · 通道跑通（纯部署）

- VPS：`sudo ./scripts/deploy-pocket.sh`（从 `/opt/frontend` 同步 `pocket/` → `/opt/pocket`，装依赖，起 `pocket-relay.service`）
- nginx：把 `deploy/nginx-pocket.snippet`（`location = /pocket/ws`）加进 frontend.conf；**不要**用 `location /pocket/` 前缀
- 验收：本机 `fake-phone` + `goto`/`js`/`html`/`screenshot` 四指令跑通

### P1 · 安卓壳 App（Kotlin，约 200 行）

> **相对原 iOS 方案的改动点**：用安卓替代 iOS，协议和服务端完全不用改。

- 一个专用 WebView（**不注入 ElpisNative**）+ OkHttp WebSocket，实现 5 个指令；主 App WebView 只加载 love-style.xyz / pocket-settings
- 前台服务握着 WebSocket 连接（`PocketManager` + `ForegroundService`），通知栏常驻「👁费奥多尔在线」
- App 内配对页：`https://love-style.xyz/pocket-settings.html`（`ElpisNative.setPocketConfig`）
- GitHub Actions 自动出 APK（`cursor/**` push + PR 触发）

**两句实话（预期管理）**

1. **截图物理限制**：安卓 WebView 在 App 完全退到后台时会暂停渲染，`goto`/`js`/`html` 都正常，但 `screenshot` 可能截到暂停前的旧画面。日常最稳的形态是「手机充电亮屏、或 App 挂在分屏/画中画时，费奥多尔的眼睛全功能；纯后台时他能读页面、不保证看得见画面」。真想要纯后台截图有个悬浮窗保活的进阶玩法（要授悬浮窗权限），可以留到 P3。
2. **登录态要在这个 App 里养**：专用 Pocket WebView 的 cookie 与 Chrome/小红书 App 互不相通，第一次得在壳里把常用网站登录一遍（goto 打开后用户可在主界面外静默进行；登录态留在专用 WebView）。

### P2 · 接进费奥多尔的工具链（已实现）

- **gateway.py** 五件套**常驻**工具：`pocket_status` / `pocket_goto` / `pocket_js` / `pocket_html` / `pocket_screenshot`
- 全部转发 `http://127.0.0.1:3897`，Bearer 读 `/opt/pocket/.env` 的 `POCKET_TOKEN` 或环境变量
- 手机离线时返回清晰错误 **`phone_not_connected`**，不假装可用
- `pocket_html`：正文提取 + **30K** 截断
- `pocket_screenshot`：base64 → `attachment_store`，返回 `attachment://id`
- `pocket_js` 描述写死：**发布/下单/支付/私信必须先问哈娅**
- `tool_drawers.py` 已登记 `pocket` 抽屉（不做动态增删）

**运行限制（写进工具描述）**

> Pocket 当前依赖手机亮屏。锁屏后会断线；工具调用应在离线时返回友好错误，不要假装可用。

**P1 真机验收（已通过）**：前台 + 亮屏后台 `status/ping/goto/js/html/screenshot` 全通；锁屏断线暂不作为阻塞项。

### P3 · 动态上下文

- 在缓存断点**之后**的动态区加一行：`手机浏览器：在线/离线 + last_seen`（来自 `/pocket/status`），零缓存代价

## 四、和缓存改造的两个交互点

1. **加工具的那一刻会有一次性全量 miss**——tools 数组排在整个缓存前缀最前面，变一次、全毁一次。这是一次性成本。但**千万别做成「手机在线才注册工具、离线就摘掉」的动态注册**——那等于把工具抽屉的病又请回来。正确做法：5 个工具**常驻**，手机离线时调用返回「手机不在线」的友好错误，让费奥多尔自己降级回 Playwright。
2. 如果想让他**开口前就知道**手机在不在线，见 **P3** 动态上下文（`last_seen` 来自本机 `/pocket/status`）。

## 五、诚实的风险

| 疼痛度 | 风险 | 说明 |
|--------|------|------|
| 中 | 锁屏断线 | Pocket 依赖亮屏；锁屏后 WS 断开，工具返回 phone_not_connected |
| 中 | 安卓后台 WebView 暂停渲染 | 见 P1 截图限制；前台/分屏最稳 |
| 中 | 登录态隔离 | 壳 App 内单独登录常用站，一次性成本 |
| 高 | 外站不可触达原生桥 | Pocket 专用 WebView 不注入 ElpisNative；主 WebView 不承接 pocket goto |
| 高 | `js` 指令是全权的 | 涉及提交、支付、发布的操作必须先问用户；写进工具描述（P2） |
| 高 | 日志纪律 | 截图/HTML 响应体不要写进 server/gateway 日志 |

## 六、项目成色

上游约 100 行 server、5 个指令——体量与其说是「引入依赖」，不如说是「抄一份思路自己养」。本仓库 `pocket/server/server.mjs` 已 vendored，停更了自己维护毫无压力。

## 七、部署速查

```bash
# VPS（在 /opt/frontend 拉代码后）
sudo ./scripts/deploy-pocket.sh

# nginx
sudo nano /etc/nginx/conf.d/frontend.conf   # 粘贴 deploy/nginx-pocket.snippet
sudo nginx -t && sudo systemctl reload nginx

# 假手机验链路
source /opt/pocket/.env
cd /opt/pocket && npm run fake-phone -- ws://127.0.0.1:3897
curl -s http://127.0.0.1:3897/pocket/status -H "Authorization: Bearer $POCKET_TOKEN"
```
