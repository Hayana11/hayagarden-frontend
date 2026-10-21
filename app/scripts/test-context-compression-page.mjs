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

// DayBlock layer: card above the day's chunks, chunks folded under it, full-text sheet.
const { dayBlockStats, dayBlockStatusLabel, dayBlockStatusTone, dayBlockReasonCopy } = await import(
  '../src/screens/contextCompression/dayblockView.ts'
);
const chunk = (turns) => ({ completed_turn_count: turns });
const ready = {
  source_day: '2026-10-18', status: 'ready', reason: null, completed_turn_count: 110,
  output_token_estimate: 1640, body_char_count: 1700, raw_token_estimate: 33747,
  created_at: '2026-10-19T03:00:00Z', model_identity: 'explicit:claude-opus-5-5',
};
assert.equal(
  dayBlockStats(ready, [chunk(16), chunk(16), chunk(16), chunk(16), chunk(16), chunk(15), chunk(15)]),
  '日块 · 7 个压缩块 · 110 轮 · 1.6k',
);
assert.equal(dayBlockStats({ ...ready, completed_turn_count: null }, [chunk(3), chunk(4)]), '日块 · 2 个压缩块 · 7 轮 · 1.6k');
assert.equal(dayBlockStatusLabel(ready), '');
const refused = { ...ready, status: 'rejected', reason: 'refusal_text' };
assert.equal(dayBlockStatusLabel(refused), '未采用 · 拒绝稿');
assert.equal(dayBlockStatusTone(refused), 'failed');
assert.match(dayBlockReasonCopy(refused), /拒绝/);
assert.equal(dayBlockStatusLabel({ ...ready, status: 'failed', reason: 'cc_background_spawn_failed' }), '生成失败');
assert.equal(dayBlockStatusLabel({ ...ready, status: 'blocked' }), '未生成');

assert.match(screen, /parts = \['压缩块', block\.completed_turn_count \+ ' 轮'\]/);
assert.match(client, /fetchJson\('\/dayblocks'/);
assert.match(client, /fetchJson\('\/dayblocks\/' \+ encodeURIComponent\(sourceDay\)/);
assert.match(screen, /fetchDayBlocks\(controller\.signal\)[\s\S]{0,260}\.catch\(\(\) => undefined\)/,
  'a DayBlock read failure must not break the chunk timeline');
assert.match(screen, /<DayBlockCard /);
assert.match(screen, /const chunksOpen = !dayblock \|\| openDayChunks\.includes\(day\)/,
  "a day's chunks fold under its DayBlock by default and stay open when it has none");
assert.match(screen, /'展开 ' \+ items\.length \+ ' 个压缩块'/);
assert.match(screen, /DAYBLOCK_SELECTION_PREFIX = 'dayblock:'/);
assert.match(screen, /<DayBlockDetailView /);
assert.match(screen, /日块全文/);
const dayblockCss = css.slice(css.indexOf('/* DayBlock:'));
assert.ok(dayblockCss.length > 20, 'DayBlock CSS block present');
assert.doesNotMatch(dayblockCss, /\bgap\s*:|\binset\s*:|\bzoom\s*:|transform:\s*scale\(|\b100dvh\b|aspect-ratio/);

console.log('test-context-compression-page: ok');
