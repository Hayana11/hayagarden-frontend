import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const root = process.cwd();
const read = (path) => readFileSync(join(root, path), 'utf8');

const app = read('src/App.tsx');
const navigation = read('src/navigation.ts');
const chat = read('src/screens/ChatScreen.tsx');
const screen = read('src/screens/ContextCompressionScreen.tsx');
const css = read('src/screens/ContextCompressionScreen.css');
const settings = read('src/screens/contextCompression/CompressionSettings.tsx');
const settingsCss = read('src/screens/contextCompression/CompressionSettings.css');
const client = read('src/lib/contextCompression.ts');
const settingsClient = read('src/lib/contextCompressionSettings.ts');

assert.match(navigation, /contextCompression:\s*'\/context-compression'/);
assert.match(
  navigation,
  /contextCompression:\s*\{\s*chrome:\s*'fullscreen',\s*globalNav:\s*false\s*\}/,
);
assert.match(app, /<Route path=\{ROUTES\.contextCompression\} element=\{<ContextCompressionScreen \/>\} \/>/);
assert.match(app, /CONTEXT_COMPRESSION_PREVIEW_PATH/);
assert.match(chat, /to=\{ROUTES\.contextCompression\}/);
assert.match(chat, /上下文压缩/);
assert.match(chat, /压缩历史 · 当前块/);
assert.doesNotMatch(chat, /to=["']\/dash\/context-compression["']/);

const navBlock = navigation.slice(navigation.indexOf('export const NAV_ITEMS'));
assert.doesNotMatch(navBlock, /key:\s*'contextCompression'/);
assert.doesNotMatch(screen, /GlobalBottomNav|BottomNav/);
assert.match(screen, /className="dash-fullscreen-page cc-page"/);
assert.match(screen, /<PageHeader title="上下文压缩"/);
assert.match(css, /\.cc-timeline\s*\{[\s\S]*?overflow-y:\s*auto/);
assert.doesNotMatch(css, /\b100dvh\b/);
assert.doesNotMatch(css, /\bzoom\s*:/);
assert.doesNotMatch(css, /transform:\s*scale\(/);
assert.doesNotMatch(css, /\binset\s*:/);
assert.doesNotMatch(css, /\.cc-current-state\s*\{[^}]*\bgap\s*:/);
assert.match(css, /\.cc-current-state > \* \+ \* \{[\s\S]*?margin-top:\s*3px/);
assert.doesNotMatch(css, /\.cc-current-state--promoted/);
assert.doesNotMatch(screen, /新设置已生效|当前块已绑定/);
assert.match(screen, /约再聊/);
assert.match(screen, /当前引擎/);
assert.match(screen, /function engineLabel/);
assert.match(client, /__continuity\//);
assert.match(settingsClient, /__continuity\//);
assert.match(settings, /PRODUCT_PROVIDER_NAMES = \['claude', 'gpt', 'deepseek'\]/);
assert.match(settings, /暂未接入压缩任务/);
assert.match(settings, /恢复默认设置/);
assert.match(settings, /cc-settings-star-rule/);
assert.ok(
  settings.indexOf('cc-settings-reset') < settings.indexOf('cc-settings-star-rule'),
  'restore-default must sit above the prompt divider',
);
assert.doesNotMatch(settingsCss, /\b100dvh\b/);
assert.doesNotMatch(settingsCss, /\bzoom\s*:/);
assert.doesNotMatch(settingsCss, /transform:\s*scale\(/);

console.log('test-context-compression-page: ok');
