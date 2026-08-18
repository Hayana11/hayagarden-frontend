# Elpis Frontend Construction Contract

> **MANDATORY READ BEFORE ANY PAGE/UI CHANGE**  
> 适用仓库：`Hayana11/hayagarden-frontend`  
> 适用范围：`app/src/**` React SPA、`static/**` 旧静态页，以及任何会影响 `/dash`、`/read`、`/board`、WebView 真机显示的页面修改。

## 0. 这不是“设计建议”，而是施工契约

未来任何 AI / Agent 在修改页面前，必须先读本文。若本文与 execution-time 当前代码冲突，**当前代码为事实源，但必须先停下并报告冲突，不能自行“统一”或重构冻结层。**

页面施工的目标不是“现代浏览器看起来能跑”，而是同时满足：

1. 桌面浏览器正常；
2. Android WebView Chrome 78 正常；
3. React SPA 与旧静态页各走自己的兼容规则；
4. 不制造第二套缩放、路由、底栏或布局权威；
5. 真机不出现空白页、变形、点击失效、输入法/光标异常；
6. 改动保持窄、小、可验证、可回滚。

---

# 1. 开工前必须记住的 12 条硬规则

1. **唯一缩放权威**：只有 `legacyNativeCompat` 可以在旧 Android 原生 WebView 条件下设置 `body.style.zoom = '0.8'`。页面、AppShell、AppFrame 不得再加 `zoom`、`transform: scale(...)` 或第二套 viewport scaling。
2. **绝不恢复旧原生 `setTextZoom(90)`**。它已经被 App Shell Reset 淘汰，会和前端 scale authority 冲突。
3. React SPA 的 Vite `build.target` 必须保持 `chrome78`；它只解决**语法转译**，不会自动 polyfill 新 runtime API。
4. 任何新 npm 浏览器依赖都必须做 **Chrome78 final-bundle runtime audit**。`Object.hasOwn` 已经真实造成过“Chat 壳先出现、历史加载后整页消失”的真机事故。
5. Chrome78 **不支持 flex gap**。React 页面优先使用现有 `vstack-*` / `hstack-*` / wrap-gap fallback；CSS Grid 的 `gap` 可以用。
6. `/dash` React SPA 与 `/read`、`/board` 等静态页不是同一种产物。静态页没有 Vite 帮你转译，inline JS 必须直接满足 ES2019/Chrome78 解析能力。
7. SPA 内部路由写逻辑路径：`/chat`、`/contacts`；**不要手写 `/dash/chat` 作为 React Router `to`**。只有外部/原生 deep-link 才使用完整站点路径 `/dash/chat`。
8. `ROUTES` / `ROUTE_META` / `NAV_ITEMS` / `AppRoutes` 是 React shell 路由权威。不要在页面里另造一套路由或底栏判断。
9. Bottom-nav 的 **chat 按钮故意打开 Contacts**；`activePaths` 同时覆盖 Contacts + Chat。不要“顺手修成直接进 Chat”。
10. Fullscreen 页面使用现有 `dash-fullscreen-page` / AppFrame embedded shell；不要改成 `100dvh`、页面级缩放或新的 fixed shell。
11. 页面几何异常时，优先修 CSS geometry / fallback，**禁止用 zoom 把问题压下去**。
12. 新页面必须过 build + relevant focused tests + Chrome78 compatibility checks；涉及布局/输入/历史渲染的改动，最终还要 Huawei Chrome78 真机验收。

---

# 2. 页面家族：先判断你在改哪一种

## 2.1 React SPA：`/dash/*`

源码主要在：

- `app/src/App.tsx`
- `app/src/navigation.ts`
- `app/src/components/AppFrame.tsx`
- `app/src/components/AppShell.tsx`
- `app/src/screens/**`
- `app/src/index.css`

构建后由 Flask 在 `/dash` 与 `/dash/<path>` 提供 SPA fallback。

React SPA 可以写现代 TypeScript/JS 语法，因为 Vite 会以 `chrome78` target 构建；但是**运行时 API 仍需自己保证兼容**。

## 2.2 Legacy static pages：`/read`、`/board` 等

主要位于：

- `static/*.html`
- `static/*.js`
- `static/static-nav.css`
- `static/sw.js`

这类页面不是 Vite bundle。浏览器会直接解析其中的 JS/CSS，所以必须按 Chrome78 的原生能力写。

**静态页严禁假定：**“TypeScript/Vite 会帮我转译。”它不会。

---

# 3. 缩放与窗口：Elpis 的唯一 Scale Authority

## 3.1 当前唯一合法缩放

`app/src/lib/legacyNativeCompat.ts` 通过**能力探测**而不是 UA/设备名判断：

```text
Capacitor native shell
AND
runtime flex-gap probe fails
→ legacyNativeCompat = true
→ body.style.zoom = '0.8'
```

它还会设置：

```html
<body data-legacy-native-compat="true">
```

这就是旧 Huawei / Chrome78 WebView 的兼容 scale authority。

## 3.2 禁止的做法

任何页面都禁止新增：

```css
zoom: 0.8;
zoom: 1.07;
transform: scale(...);
transform-origin: ...; /* 用于页面整体缩放时 */
```

也禁止：

- 在 `AppShell` 再缩一次；
- 在 `AppFrame` 再缩一次；
- 在某个页面根节点“补偿” body zoom；
- Android 原生层重新 `setTextZoom(90)`；
- 用 viewport meta 做页面级补偿。

**理由：**双重缩放会同时破坏宽度、fixed 定位、触控坐标、输入框光标、底栏高度与 WebView 的字体/IME 行为。

## 3.3 当前几何权威

### Standard shell

```text
AppShell max-width = 452px
margin = auto
min-height = 100vh
```

### Fullscreen shell

```text
AppFrame fullscreen embedded max-width = 480px
position = fixed; top/right/bottom/left = 0
```

### Chat / Codex 顶部 toolbar

```text
max-width = 430px
padding = 10px 12px 9px
child spacing = 10px sibling margin
```

如果页面在 Huawei 上“显得大/小/挤”，**先检查它有没有违反这些 geometry contract**，不要加新的 zoom。

---

# 4. Chrome 78 兼容合同

## 4.1 React SPA：语法可以转译，runtime API 不会自动补

`app/vite.config.ts` 必须继续：

```ts
build: {
  target: 'chrome78'
}
```

这能把很多新语法转成 Chrome78 可解析版本，但不能自动补浏览器对象/方法。

### 新依赖或新代码出现这些 API 时必须审计

包括但不限于：

- `Object.hasOwn`
- `.at()`
- `replaceAll()`
- `Promise.any`
- `structuredClone`
- `crypto.randomUUID`
- `AbortSignal.timeout`
- `URLPattern`
- `WeakRef` / `FinalizationRegistry`
- 新版 Intl API
- 新 stream API

规则：

```text
source 看起来没问题 ≠ Chrome78 真机没问题
build PASS ≠ runtime PASS
Node smoke PASS ≠ WebView PASS
```

必须确认**实际 final browser bundle**中是否进入这些调用、是否位于可达路径、是否已有 feature-detected fallback。

### 历史事故：Markdown / Object.hasOwn

`react-markdown@10.1.0` 曾在首次 assistant Markdown render 时调用 Chrome78 不支持的 `Object.hasOwn`，导致：

```text
Chat 壳先出现
→ 历史 API 返回
→ assistant history 首次 Markdown render
→ runtime exception
→ React 内容消失，只剩背景
```

最终使用 feature-detected compat shim 修复。

**以后引入任何第三方 renderer / chart / editor / markdown / date / animation library，都要按同一标准检查。**

## 4.2 Static page：直接按 ES2019 写

旧静态 HTML 没有 Vite 转译。应避免直接使用 Chrome78 不支持的语法/特性，例如：

```text
?.
??
||=
&&=
??=
replaceAll
.at()
Promise.any
crypto.randomUUID
```

静态页的 JS 应能通过 ES2019 parser（例如 acorn `ecmaVersion: 2019`）再交付。

## 4.3 CSS：Chrome78 常见雷区

### Flex gap：禁止依赖

Chrome78 不支持 flex container `gap`。

错误：

```css
.row {
  display: flex;
  gap: 10px;
}
```

正确：优先使用已有 helper：

```html
<div class="hstack hstack-10">...</div>
<div class="vstack vstack-16">...</div>
```

或语义 class + sibling margin：

```css
.row > * + * { margin-left: 10px; }
```

**CSS Grid gap 可用**（Chrome78 支持）。

### `inset` shorthand

不要依赖：

```css
inset: 0;
```

兼容写法：

```css
top: 0;
right: 0;
bottom: 0;
left: 0;
```

项目已有：

- `.c78-fill-fixed`
- `.c78-fill-absolute`

### `aspect-ratio`

Chrome78 不支持。先写 padding/square fallback，再用 `@supports` 做现代增强。

### `min()` / `max()` / `clamp()`

不要把关键布局建立在这些 CSS 函数上。使用 `width + max-width/min-width` 等传统 geometry fallback。

### `color-mix()`

必须先给真实 fallback，再在 `@supports` 里增强。

### `100dvh`

Fullscreen 关键页面不要依赖 dynamic viewport unit。项目当前采用 fixed top/right/bottom/left + `height:100%` / flex `min-height:0` 契约。

---

# 5. Layout Geometry：不要让页面各自发明物理学

## 5.1 间距

优先复用：

```text
vstack-*     垂直 sibling margin
hstack-*     水平 sibling margin
flex-wrap-gap-*  flex-wrap fallback
```

不要为了“写起来短”重新换回 flex `gap`。

## 5.2 页面 Header

标准 PageHeader 当前关键几何：

```text
back button = 38 × 38
back → title = 14px
page title margin = 0
```

不要用 generic `.page-header > * + *` 偷懒改掉语义 spacing。

Chat / Codex 使用 `.page-header-toolbar`；Contacts 目前有自己冻结的 geometry，不要强行“统一组件”造成视觉漂移。

## 5.3 Fullscreen 页面

推荐结构：

```text
AppFrame fullscreen embedded
  ├─ page flex:1; min-height:0; overflow:hidden
  └─ GlobalBottomNav embedded
```

真正滚动的内容区域要：

```css
flex: 1;
min-height: 0;
overflow-y: auto;
-webkit-overflow-scrolling: touch;
```

**一个区域只应有一个主要 scroll owner。** 避免 body + page + inner list 三层同时滚。

## 5.4 Safe area / Bottom Nav

标准页面已有：

```css
.screen-stack {
  padding-bottom: calc(var(--global-nav-reserve-height) + env(safe-area-inset-bottom, 0px));
}
```

不要在每个页面再手写另一套“底栏留白”。

---

# 6. Routing 与 Bottom Nav 合同

## 6.1 BrowserRouter basename

React SPA 在 `/dash` 下工作，`App.tsx` 会使用：

```text
basename = /dash
```

因此 `navigation.ts` 里的 SPA 路由必须是逻辑路径：

```ts
ROUTES.chat = '/chat'
ROUTES.contacts = '/contacts'
```

### React 内部跳转

正确：

```tsx
<NavLink to={ROUTES.chat} />
```

错误：

```tsx
<NavLink to="/dash/chat" />
```

### 原生通知 / 站外 deep link

这是完整 URL，所以正确的是：

```text
https://love-style.xyz/dash/chat
```

## 6.2 新 React 页面至少要检查 3 个权威点

新增页面时通常至少需要：

1. `ROUTES`
2. `AppRoutes`
3. `ROUTE_META`

只有它确实属于全局底栏 surface 时才改 `NAV_ITEMS`。

## 6.3 Chat tab 的产品语义是冻结的

当前底栏：

```text
chat tab → ContactsScreen
active when /contacts OR /chat
```

这是产品设计，不是 bug。

## 6.4 Static nav 与 React nav 不是同一份

`NAV_ITEMS` 只控制 React `GlobalBottomNav`。

`/read`、`/board` 的静态 nav 仍由静态 HTML/CSS 自己控制。改 `NAV_ITEMS` 不代表旧静态页也跟着变。

---

# 7. Typography / Theme：尽量用现有语言，不制造局部宇宙

当前 canonical families：

```text
Chinese serif  → Noto Serif SC
Display serif  → Bodoni Moda
Mono/UI        → Space Grotesk
```

对应 CSS variables：

```text
--font-serif-cn
--font-serif-display
--font-mono
```

React 页面优先复用 `index.css` 的 color / radius / shadow / nav tokens，而不是每页复制一套近似颜色。

主要 tokens 包括：

```text
--color-bg
--color-card
--color-border
--color-primary
--color-text*
--color-rose*
--color-violet
--color-amber
--color-green*
--radius-card
--shadow-card
--shadow-fab
--nav-active
--nav-muted
```

如果设计稿要求新颜色，可以新增，但不要为了一个按钮写出十几个局部近似 hex 值。

---

# 8. Touch / IME / 输入框：真机稳定优先于“高级动效”

Huawei Android 10 + Chrome78 WebView 曾经重点验过中文输入、composition、选字、光标移动、键盘弹出后的布局。

因此涉及输入框时：

- 不要对输入框祖先做整体 `transform: scale(...)`；
- 不要在 keypress / composition 中反复 remount input；
- 不要因为 resize 就 reload/reset route；
- 不要用 JS 强行改 selection，除非功能确实需要；
- 注意 `compositionstart / compositionend`；
- 键盘弹出后不能把主要 CTA 永久遮住；
- hover-only 操作必须有 touch fallback。

对移动端隐藏操作按钮时，项目已有类似：

```css
@media (hover: none) { ... }
```

不要只设计桌面 hover。

---

# 9. Blank Screen 防线：页面能 build 不代表不会真机消失

React SPA 的 `app/index.html` 已经安装 `/api/client-error` 上报：

- `window.onerror`
- `unhandledrejection`
- resource load error
- dash probe

当 Huawei 真机出现“只剩背景 / root 消失 / 页面突然空白”时，优先：

1. 看 `/api/client-error` / server error log；
2. 对齐故障时序：mount、fetch、setState、第三方 render、effect；
3. 查 final bundle runtime API；
4. 不要先怪路由或用 CSS 掩盖 JS crash。

引入第三方 UI/render dependency 时，至少验证：

```text
import-time 是否安全
首次 render 是否安全
真实 history/data render 是否安全
Chrome78 runtime API 是否安全
```

Node `renderToStaticMarkup` 只能做逻辑 smoke，不代表 Android WebView PASS。

---

# 10. Service Worker / Cache：React 与静态页规则不同

当前 `static/sw.js` 对 `/dash` 明确放行网络，不走其静态缓存策略；因此 React SPA 修改通常不需要为了 `/dash` bump static SW cache。

但 `/read`、`/board` 等静态资源仍可能走 Service Worker cache。修改这类静态资源时，要检查 `static/sw.js` 当前缓存版本与策略；若旧缓存可能继续提供旧资源，按现行静态页契约 bump `CACHE` 版本。

**不要机械地每次 React 改动都 bump SW；也不要在 static 改动后忘记检查缓存。**

---

# 11. 新页面标准施工流程

## Phase A — Preflight

AI 必须先回答：

```text
PAGE_FAMILY = React SPA / Static legacy
ROUTE = ...
CHROME = standard / fullscreen
GLOBAL_NAV = fixed / embedded / none
SCROLL_OWNER = ...
USES_INPUT = yes/no
NEW_BROWSER_DEPENDENCY = yes/no
CHROME78_RUNTIME_RISK = ...
SCALE_CHANGE_REQUIRED = NO (default)
```

如果 `SCALE_CHANGE_REQUIRED = YES`，先停工，说明为什么现有 scale authority 不够，等待 Owner 授权。

## Phase B — Implementation

- 优先复用 `AppFrame` / `AppShell` / `PageHeader` / `GlobalBottomNav` / spacing helpers；
- 不复制 shell；
- 不复制 nav registry；
- 不新增 page zoom；
- 不顺手重构别的页面；
- 一个 PR 保持一个明确视觉/功能目标。

## Phase C — Compatibility review

检查：

```text
flex gap?
inset shorthand?
aspect-ratio only?
min/max/clamp critical geometry?
100dvh?
new runtime API?
new npm dependency?
static page modern JS syntax?
input/IME side effects?
```

## Phase D — Tests

基础：

```bash
cd app
npm run build
npm run lint
```

页面/shell 常用 focused tests：

```bash
npm run test:react-shell-architecture
npm run test:typography-semantics
npm run test:page-header-geometry
npm run test:chrome78-geometry-sweep
```

Chat 相关再加：

```bash
npm run test:chat-cold-start
npm run test:chat-warm-return
npm run test:chat-capacity-header
npm run test:chat-markdown
npm run test:legacy-transcript-window
```

Usage/特殊页面按现有 package scripts 补对应 focused test。

### Static page

除 repo 现有测试外，应做 ES2019 parse audit，并真机检查静态 nav / overlay / cache。

## Phase E — Owner true-device gate

涉及以下任一项，不能仅凭代码测试宣布 FROZEN：

- fullscreen geometry
- fixed bottom nav
- 输入框/IME
- 滚动恢复
- Chrome78 第三方 dependency
- notification deep-link 页面
- 大历史列表/复杂 renderer

最终至少在 Huawei Chrome78 上做对应操作路径。

---

# 12. 页面 PR 的标准报告格式

建议 Agent 最终只报告：

```text
BASE_SHA
HEAD_SHA
PAGE_FAMILY
FILES_CHANGED
ROUTE_META_CHANGED = yes/no
NAV_CHANGED = yes/no
SCALE_CHANGED = NO
NEW_DEPENDENCY = yes/no
CHROME78_PARSE_RISK = pass/fail
CHROME78_RUNTIME_RISK = pass/fail/none
BUILD = pass/fail
FOCUSED_TESTS = ...
STATIC_SW_CACHE_CHANGED = yes/no/n/a
TRUE_DEVICE_REQUIRED = yes/no
PRODUCTION_MUTATIONS = 0
VERDICT = CODE_PASS / CODE_FAIL
```

不要把“build 成功”写成“真机通过”。

---

# 13. 明确禁止“顺手修”的冻结层

页面施工默认不得顺手改：

- `legacyNativeCompat` 的 0.8 scale authority；
- `AppShell` / `AppFrame` 的 viewport scale 语义；
- `BrowserRouter basename`；
- `ROUTES / NAV_ITEMS / ROUTE_META` 的既有产品语义；
- chat tab → Contacts 的行为；
- Chat toolbar `max-width: 430px`；
- old native `setTextZoom`；
- 原生 App WebView scale；
- backend/Wake/Resident/tool capability；
- static page 与 SPA 的边界。

如果任务确实要求改其中之一，必须把它升级成**独立架构任务**，而不是夹在一个页面美化 PR 里。

---

# 14. 推荐加入 AGENTS.md 的强制门

为了让以后 AI 真正“必须读”，建议在仓库根 `AGENTS.md` 增加：

```md
## Frontend construction gate — mandatory

Before modifying any UI/page code under `app/src/**`, `app/index.html`, or `static/**`,
MUST read `docs/FRONTEND_CONSTRUCTION_GUIDE.md` first.

The guide defines the frozen viewport/scale authority, Chrome 78 compatibility contract,
React-vs-static page boundary, routing/nav authority, layout geometry, and required focused tests.
Do not introduce page-level zoom/scale, a second shell/nav authority, or new browser runtime
requirements without explicitly auditing and reporting them.

If current code conflicts with the guide, STOP and report the drift before editing either side.
```

推荐 repo 路径：

```text
docs/FRONTEND_CONSTRUCTION_GUIDE.md
```

这样 Cursor / Claude Code / Codex 等只要先读根 `AGENTS.md`，就会被明确引导到这份契约。

---

# 15. Authority Source Map

未来维护本指南时，优先重新核对：

```text
AGENTS.md
app/vite.config.ts
app/src/App.tsx
app/src/navigation.ts
app/src/lib/legacyNativeCompat.ts
app/src/components/AppFrame.tsx
app/src/components/AppShell.tsx
app/src/components/GlobalBottomNav.tsx
app/src/index.css
app/index.html
app/package.json
app/scripts/test-react-shell-architecture.mjs
app/scripts/test-page-header-geometry.mjs
app/scripts/test-chrome78-geometry-sweep.mjs
static/sw.js
```

本文不应该变成“比代码更古老的传说”。当冻结层正式改变时，**同一个 PR/阶段必须同步更新本指南**。

---

# 最后一句

> **Elpis 页面不是按“现代网页默认值”施工，而是按“桌面浏览器 + Huawei Android 10 / Chrome78 WebView 共存”的产品现实施工。**  
> 遇到兼容问题，修 geometry / runtime contract；不要用第二层 zoom、隐藏异常或重建 shell 来绕过去。
