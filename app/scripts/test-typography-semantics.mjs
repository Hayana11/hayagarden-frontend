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
  return line
    .replace(/title=\{[^}]*\}/g, '')
    .replace(/title=["'][^"']*["']/g, '');
}

function isDisplayCjkMisuse(lines, i) {
  const line = lines[i];
  if (!DISPLAY_RE.test(line)) return false;
  if (/MixedSectionLabel|fontFamilyForText|hasCJK/.test(line)) return false;

  const noTitle = stripTitleAttrs(line);
  if (CJK_RE.test(noTitle)) return true;

  // Multiline JSX: opening tag with DISPLAY ends line; immediate next line is raw CJK text
  if (/fontFamily:\s*FONT_DISPLAY/.test(line) && />\s*$/.test(line.trim())) {
    const next = lines[i + 1]?.trim() ?? '';
    if (CJK_RE.test(next) && !CN_SPLIT_RE.test(next) && !/^<[/a-zA-Z]/.test(next)) {
      return true;
    }
  }

  return false;
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
  assert.doesNotMatch(codex, /BottomNav/);
}

// 7. Scan screens: DISPLAY + CJK on same line, or DISPLAY opener + next-line CJK child
const misuse = [];
for (const file of listTsxFiles(screensDir)) {
  const lines = fs.readFileSync(path.join(screensDir, file), 'utf8').split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (!isDisplayCjkMisuse(lines, i)) continue;
    misuse.push({ file, line: i + 1, text: lines[i].trim() });
  }
}

if (misuse.length > 0) {
  console.error('Chinese + DISPLAY misuse:', misuse);
}
assert.equal(misuse.length, 0, `Chinese text must not use DISPLAY without split/helper (found ${misuse.length})`);

// 8. CJK + FONT_CN must not use synthetic italic
function cnItalicMisuse(lines, i) {
  const line = lines[i];
  if (!CN_SPLIT_RE.test(line) || !/fontStyle:\s*['"]italic['"]/.test(line)) return false;
  const noTitle = stripTitleAttrs(line);
  if (CJK_RE.test(noTitle)) return true;
  // Multiline: opening tag with FONT_CN + italic; next line is CJK text
  if (/fontFamily:\s*FONT_CN/.test(line) && />\s*$/.test(line.trim())) {
    const next = lines[i + 1]?.trim() ?? '';
    if (CJK_RE.test(next) && !/^<[/a-zA-Z]/.test(next)) return true;
  }
  return false;
}

function assertNoCnItalicInFile(rel, label) {
  const lines = read(rel).split('\n');
  const hits = [];
  for (let i = 0; i < lines.length; i++) {
    if (!cnItalicMisuse(lines, i)) continue;
    hits.push({ line: i + 1, text: lines[i].trim() });
  }
  assert.equal(hits.length, 0, `${label}: FONT_CN + italic on CJK (found ${hits.length})`);
}

// Moments — end-of-feed marker
{
  const moments = read('screens/MomentsScreen.tsx');
  const block = moments.slice(moments.indexOf('— 流到这里就停了 —') - 200, moments.indexOf('— 流到这里就停了 —') + 80);
  assert.match(block, /fontFamily:\s*FONT_CN/);
  assert.doesNotMatch(block, /fontStyle:\s*['"]italic['"]/);
}

// Moments — Corridor title split: Latin italic on DISPLAY only, Chinese normal on FONT_CN
{
  const moments = read('screens/MomentsScreen.tsx');
  const idx = moments.indexOf('The Corridor');
  const block = moments.slice(Math.max(0, idx - 120), idx + 280);
  assert.match(block, /FONT_DISPLAY,\s*fontStyle:\s*['"]italic['"]/);
  assert.match(block, /FONT_CN,\s*fontStyle:\s*['"]normal['"]/);
  const outer = block.match(/<span style=\{\{ fontSize: 12, letterSpacing: 2, color: 'var\(--dream\)' \}\}>/)?.[0] ?? '';
  assert.doesNotMatch(outer, /fontStyle/, 'Corridor outer wrapper must not set italic');
}

// Ledger — memory/read association labels
assertNoCnItalicInFile('screens/LedgerScreen.tsx', 'Ledger');

// Codex — status CJK guard remains
{
  const codex = read('screens/CodexChatScreen.tsx');
  assert.match(codex, /hasCJK\(statusText\)\s*\?\s*'normal'\s*:\s*'italic'/);
}

console.log('test-typography-semantics: ok');
