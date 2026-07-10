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

- 一个全屏 WebView + OkHttp 的 WebSocket 客户端，实现 5 个指令（`ping` / `goto` / `js` / `html` / `screenshot`），断线指数退避重连
- 前台服务握着 WebSocket 连接，通知栏常驻一条（比如「👁费奥多尔在线」），App 加进电池优化白名单，连接就能稳定活着
- 工程在 [HayaGarden](https://github.com/Hayana11/HayaGarden) 仓库，GitHub Actions 自动出 APK——改代码推上去、等两分钟、下载安装，全程无本地环境

**两句实话（预期管理）**

1. **截图物理限制**：安卓 WebView 在 App 完全退到后台时会暂停渲染，`goto`/`js`/`html` 都正常，但 `screenshot` 可能截到暂停前的旧画面。日常最稳的形态是「手机充电亮屏、或 App 挂在分屏/画中画时，费奥多尔的眼睛全功能；纯后台时他能读页面、不保证看得见画面」。真想要纯后台截图有个悬浮窗保活的进阶玩法（要授悬浮窗权限），可以留到 P3。
2. **登录态要在这个 App 里养**：WebView 的 cookie 和你手机上的 Chrome、小红书 App 互不相通，第一次得在壳 App 里把常用网站登录一遍，之后长期有效。这点 iOS 版其实一样，不是安卓的额外代价。

### P2 · 接进费奥多尔的工具链

- 在 home MCP server 里加 5 个工具：`pocket_status` / `pocket_goto` / `pocket_js` / `pocket_html` / `pocket_screenshot`，实现就是转发 `localhost:3897`
- `screenshot` 返回的 base64 存进 `static/`，走现有图片卡片管线
- `html` 结果过一遍正文提取 + 截断（30K 上限，照用户文件注入的现成规矩）

## 四、和缓存改造的两个交互点

1. **加工具的那一刻会有一次性全量 miss**——tools 数组排在整个缓存前缀最前面，变一次、全毁一次。这是一次性成本。但**千万别做成「手机在线才注册工具、离线就摘掉」的动态注册**——那等于把工具抽屉的病又请回来。正确做法：5 个工具**常驻**，手机离线时调用返回「手机不在线」的友好错误，让费奥多尔自己降级回 Playwright。
2. 如果想让他**开口前就知道**手机在不在线，把 `/pocket/status` 的结果加一行进动态上下文——动态区在最后一条 user 消息里，天生就是给这种易变状态准备的。

## 五、诚实的风险

| 疼痛度 | 风险 | 说明 |
|--------|------|------|
| 中 | 安卓后台 WebView 暂停渲染 | 见 P1 截图限制；前台/分屏最稳 |
| 中 | 登录态隔离 | 壳 App 内单独登录常用站，一次性成本 |
| 高 | `js` 指令是全权的 | 涉及提交、支付、发布的操作必须先问用户；写进工具描述 |
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
