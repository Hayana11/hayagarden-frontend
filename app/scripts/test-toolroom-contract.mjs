import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const root = process.cwd();
const read = (path) => readFileSync(join(root, path), 'utf8');

const app = read('src/App.tsx');
const navigation = read('src/navigation.ts');
const chat = read('src/screens/ChatScreen.tsx');
const screen = read('src/screens/ToolroomScreen.tsx');
const css = read('src/screens/ToolroomScreen.css');

assert.match(navigation, /toolroom:\s*'\/toolroom'/);
assert.match(navigation, /toolroom:\s*\{\s*chrome:\s*'fullscreen',\s*globalNav:\s*false\s*\}/);
assert.match(app, /<Route path=\{ROUTES\.toolroom\} element=\{<ToolroomScreen \/>\} \/>/);
assert.match(chat, /to=\{ROUTES\.toolroom\}/);
assert.match(chat, /真实工具清单 · 当前活动/);

const navBlock = navigation.slice(navigation.indexOf('export const NAV_ITEMS'));
assert.doesNotMatch(navBlock, /key:\s*'toolroom'/);
assert.match(screen, /http\.get<InventoryResponse>\('\/api\/tools\/inventory'\)/);
assert.match(screen, /realityPromptProjection/);
assert.match(screen, /本页只展示真实清单，不执行任何工具/);
assert.doesNotMatch(screen, /QQ · 群消息|淘宝闪购|com\.aion\.chat/);

assert.doesNotMatch(css, /\b100dvh\b/);
assert.doesNotMatch(css, /\bzoom\s*:/);
assert.doesNotMatch(css, /transform:\s*scale\(/);
assert.doesNotMatch(css, /\binset\s*:/);
assert.match(css, /\.toolroom-scroll\s*\{[\s\S]*?overflow-y:\s*auto/);
assert.match(css, /\.toolroom-scroll::[-]webkit-scrollbar/);
assert.match(css, /scrollbar-width:\s*none/);
assert.doesNotMatch(css, /\.toolroom-tool-divider/);
assert.match(css, /\.toolroom-detail-divider/);
assert.match(screen, /Prompt \/ Usage/);
assert.doesNotMatch(screen, /toolroom-tool-divider/);
assert.match(css, /\.toolroom-tool-detail\s*\{[\s\S]*?background:\s*rgba\(252, 242, 244, 0\.90\)/);
assert.match(css, /\.toolroom-detail-label\s*\{[\s\S]*?color:\s*#b7a29c;[\s\S]*?font-family:\s*'JetBrains Mono', monospace;[\s\S]*?font-size:\s*10px;/);
assert.match(css, /\.toolroom-detail-label\s*\{[\s\S]*?text-transform:\s*uppercase/);
assert.match(css, /\.toolroom-tool-detail dt\s*\{[\s\S]*?color:\s*#b76e79;[\s\S]*?font-family:\s*'JetBrains Mono', monospace;[\s\S]*?font-size:\s*11\.5px;/);
assert.match(css, /\.toolroom-tool-detail p\s*\{[\s\S]*?color:\s*#6b5a55;[\s\S]*?font-size:\s*12\.5px;/);
assert.match(screen, /GROUP_ICON_PATHS/);
assert.match(screen, /<ToolroomGroupIcon/);
assert.match(screen, /transportLabel/);
assert.match(screen, /return 'Canary'/);
assert.match(screen, /工具：\{group\.available\}\/\{group\.total\}/);
assert.match(screen, /toolroom-connection-badge/);
assert.match(screen, /toolroom-transport-badge/);
assert.match(css, /\.toolroom-transport-badge\s*\{[\s\S]*?color:\s*#5e7f98/);
assert.match(css, /\.toolroom-connection-badge::before\s*\{[\s\S]*?background:\s*#c7b9b5/);
assert.match(css, /\.toolroom-connection-badge\.is-live::before\s*\{[\s\S]*?background:\s*#8fbb86/);
assert.match(css, /\.toolroom-chevron\.is-open\s*\{[\s\S]*?scaleY\(0\.62\)/);
assert.match(css, /\.toolroom-header-wrap \.page-header__subtitle\s*\{[\s\S]*?left:\s*52px/);
assert.match(css, /\.toolroom-header-wrap \.page-header__subtitle\s*\{[\s\S]*?font-style:\s*normal/);
assert.match(css, /\.toolroom-header-wrap \.page-header__subtitle::before\s*\{[\s\S]*?display:\s*none/);
assert.doesNotMatch(screen, /🧠/);
assert.match(css, /\.toolroom-group-icon svg\s*\{[\s\S]*?stroke:\s*currentColor/);
assert.match(css, /\.toolroom-tool-copy > span\s*\{[\s\S]*?font-family:\s*'Noto Serif SC', serif;[\s\S]*?font-size:\s*11\.5px;[\s\S]*?line-height:\s*1\.2/);

assert.match(screen, /GROUP_ACCENT_COLORS/);
assert.match(screen, /'--toolroom-accent'/);
assert.match(screen, /DISABLED_TOOL_ACCENT/);
assert.match(css, /background:\s*var\(--toolroom-accent/);
assert.match(css, /border-left:\s*3px solid var\(--toolroom-accent/);

console.log('test-toolroom-contract: ok');
