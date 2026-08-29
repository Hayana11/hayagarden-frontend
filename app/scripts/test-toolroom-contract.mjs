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
assert.match(css, /\.toolroom-tool-divider/);
assert.match(css, /\.toolroom-detail-divider/);
assert.match(screen, /Prompt \/ Usage/);

console.log('test-toolroom-contract: ok');
