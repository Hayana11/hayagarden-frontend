import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = path.join(root, 'src');
const screensDir = path.join(src, 'screens');

const CJK_RE = /[\u3400-\u9FFF\uF900-\uFAFF]/;
const DISPLAY_RE = /fontFamily:\s*(?:DISPLAY|FONT_DISPLAY|'Bodoni Moda'|var\(--font-serif-display\))/;
const CN_SPLIT_RE = /fontFamily:\s*(?:FONT_CN|SERIF|'Noto Serif SC'|var\(--font-serif-cn\))/;
const SCAN_AHEAD = 3;

function read(rel) {
  return fs.readFileSync(path.join(src, rel), 'utf8');
}

function readFromApp(rel) {
  return fs.readFileSync(path.join(root, rel), 'utf8');
}

function listTsxFiles(dir) {
  return fs.readdirSync(dir).filter((f) => f.endsWith('.tsx'));
}

function stripTitleAttrs(line) {
  return line.replace(/title=\{?["'][^"']*["']\}?/g, '');
}

// 1. Canonical typography module exists
{
  const typo = read('lib/typography.ts');
  assert.match(typo, /export const FONT_CN = 'var\(--font-serif-cn\)'/);
  assert.match(typo, /export const FONT_DISPLAY = 'var\(--font-serif-display\)'/);
  assert.match(typo, /export function fontFamilyForText/);
  assert.match(typo, /export const FONT_MONO = 'ui-monospace, Menlo, monospace'/);
}

// 2. Global CSS utility aliases exist
{
  const css = readFromApp('src/index.css');
  assert.match(css, /\.font-cn\s*\{/);
  assert.match(css, /\.font-display\s*\{/);
  assert.match(css, /font-family: var\(--font-serif-cn\)/);
}

// 3. Contacts must not put 葡萄海大富翁 on DISPLAY
{
  const contacts = read('screens/ContactsScreen.tsx');
  assert.doesNotMatch(contacts, /葡萄海大富翁[\s\S]{0,120}FONT_DISPLAY/);
  assert.doesNotMatch(contacts, /葡萄海大富翁[\s\S]{0,120}DISPLAY/);
  assert.match(contacts, /from '\.\.\/lib\/typography'/);
  assert.doesNotMatch(contacts, /BottomNav/);
}

// 4. Moments DateRail must assign CN to 今天/昨天
{
  const moments = read('screens/MomentsScreen.tsx');
  assert.match(moments, /dateLabel === '今天'[\s\S]*primaryFont = FONT_CN/);
  assert.match(moments, /dateLabel === '昨天'[\s\S]*primaryFont = FONT_CN/);
}

// 5. Daily preview mixed banner must split Latin vs Chinese families
{
  const preview = read('screens/DailySoftWindowPreviewScreen.tsx');
  assert.match(preview, /FE-R1 · MOCK[\s\S]*FONT_DISPLAY/);
  assert.match(preview, /预览专用[\s\S]*FONT_CN/);
  assert.doesNotMatch(
    preview,
    /fontFamily:\s*FONT_DISPLAY[\s\S]{0,80}预览专用/,
    'Daily preview must not put Chinese on FONT_DISPLAY container',
  );
}

// 6. Codex status must not synthetic-italic Chinese
{
  const codex = read('screens/CodexChatScreen.tsx');
  assert.match(codex, /hasCJK\(statusText\)/);
  assert.match(codex, /statusFontStyle/);
  assert.doesNotMatch(
    codex,
    /fontFamilyForText\(statusText\)[\s\S]{0,60}fontStyle:\s*'italic'/,
    'Codex statusText must not force italic when CJK',
  );
  assert.doesNotMatch(codex, /BottomNav/);
}

// 7. Scan screens: DISPLAY line + nearby CJK without CN split/helper
const allowlist = [
  {
    file: 'ChatScreen.tsx',
    line: (text) => /title=\{usage\.costEstimated/.test(text),
    reason: 'HTML title attribute only; not rendered font family',
  },
  {
    file: 'MomentsScreen.tsx',
    line: (text) => /fontFamily:\s*FONT_DISPLAY/.test(text) && /social\.(likes|dislikes|comments)/.test(text),
    reason: 'numeric social counts intentionally use DISPLAY',
  },
];

const misuse = [];
for (const file of listTsxFiles(screensDir)) {
  const body = fs.readFileSync(path.join(screensDir, file), 'utf8');
  const lines = body.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!DISPLAY_RE.test(line)) continue;
    if (/MixedSectionLabel|fontFamilyForText|hasCJK/.test(line)) continue;

    const windowLines = lines.slice(i, Math.min(lines.length, i + SCAN_AHEAD + 1));
    const windowText = windowLines.map(stripTitleAttrs).join('\n');
    if (!CJK_RE.test(windowText)) continue;
    if (windowLines.some((l) => CN_SPLIT_RE.test(l))) continue;

    const hit = { file, line: i + 1, text: line.trim() };
    const allowed = allowlist.some((rule) => rule.file === file && rule.line(line));
    if (!allowed) misuse.push(hit);
  }
}

if (misuse.length > 0) {
  console.error('Chinese + DISPLAY misuse (multi-line scan):', misuse);
}
assert.equal(misuse.length, 0, `Chinese text must not use DISPLAY without split/helper (found ${misuse.length})`);

console.log('test-typography-semantics: ok');
