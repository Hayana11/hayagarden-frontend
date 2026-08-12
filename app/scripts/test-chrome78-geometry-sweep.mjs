import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repoRoot = path.dirname(appRoot);
const src = path.join(appRoot, 'src');
const staticDir = path.join(repoRoot, 'static');

function readSrc(rel) {
  return fs.readFileSync(path.join(src, rel), 'utf8');
}

function readStatic(rel) {
  return fs.readFileSync(path.join(staticDir, rel), 'utf8');
}

function filterConsoleBlock(memory) {
  const start = memory.indexOf('className="memory-filter-console"');
  assert.ok(start >= 0, 'memory-filter-console missing');
  const end = memory.indexOf('{view ===', start);
  return memory.slice(start, end);
}

// A. Memory — no page-level zoom; filter row + pill internal spacing without flex gap
{
  const memory = readSrc('screens/MemoryScreen.tsx');
  const filterBlock = filterConsoleBlock(memory);

  assert.doesNotMatch(memory, /zoom:\s*1\.07/);
  assert.doesNotMatch(filterBlock, /\bgap\s*:/);
  assert.match(filterBlock, /className="memory-filter-pill"/);
  assert.match(filterBlock, /className="memory-filter-pill memory-filter-pill--tag"/);

  const drag = readSrc('components/DragScrollRow.tsx');
  assert.match(drag, /drag-scroll-row-outer/);
  assert.doesNotMatch(drag, /\bgap\s*:/);

  const css = readSrc('index.css');
  assert.match(css, /\.drag-scroll-row-outer > \* \+ \* \{[\s\S]*?margin-left:\s*8px/);
  assert.match(css, /\.drag-scroll-row > \* \+ \* \{[\s\S]*?margin-left:\s*8px/);
  assert.match(css, /\.memory-filter-pill > \* \+ \* \{[\s\S]*?margin-left:\s*6px/);
  assert.match(css, /\.memory-filter-pill--tag > \* \+ \* \{[\s\S]*?margin-left:\s*5px/);
  assert.doesNotMatch(css, /\.drag-scroll-row\s*\{[^}]*\bgap\s*:/);
}

// B. Ledger — Explore stack + calendar legend 14×14 wrap-gap fallback
{
  const ledger = readSrc('screens/LedgerScreen.tsx');
  assert.match(ledger, /className="ledger-explore-stack"/);
  assert.match(ledger, /className="calendar-legend-row"/);
  assert.doesNotMatch(
    ledger,
    /tab === '探索'[\s\S]*?flexDirection:\s*'column'[\s\S]*?gap:\s*14/,
  );
  assert.doesNotMatch(
    ledger,
    /calendar-legend-row[\s\S]*?gap:\s*(14|5)/,
  );

  const css = readSrc('index.css');
  assert.match(css, /\.ledger-explore-stack > \* \+ \* \{[\s\S]*?margin-top:\s*14px/);
  assert.match(css, /\.calendar-legend-row \{[\s\S]*?margin-right:\s*-14px/);
  assert.match(css, /\.calendar-legend-row \{[\s\S]*?margin-bottom:\s*-14px/);
  assert.match(css, /\.calendar-legend-row > \* \{[\s\S]*?margin-right:\s*14px/);
  assert.match(css, /\.calendar-legend-row > \* \{[\s\S]*?margin-bottom:\s*14px/);
  assert.match(css, /\.calendar-legend-row > \* > \* \+ \* \{[\s\S]*?margin-left:\s*5px/);
}

// C. Read — inset fallback + SPA contacts route for static HTML
{
  const read = readStatic('read.html');
  assert.doesNotMatch(read, /\.mo\{[^}]*\binset\s*:\s*0/);
  assert.match(read, /\.mo\{[^}]*top:\s*0[^}]*right:\s*0[^}]*bottom:\s*0[^}]*left:\s*0/);
  assert.match(read, /href="\/dash\/contacts"[^>]*>[\s\S]*?chat/);
  assert.doesNotMatch(read, /href="\/contacts"/);
  assert.doesNotMatch(read, /href="\/chat"/);
  assert.match(read, /href="\/static\/static-nav\.css"/);
}

// D. Board — overlay above bnav + inset fallback + SPA contacts route
{
  const board = readStatic('board.html');
  const navCss = readStatic('static-nav.css');

  assert.doesNotMatch(board, /\.panel-overlay\{[^}]*\binset\s*:\s*0/);
  assert.match(
    board,
    /\.panel-overlay\{[^}]*top:\s*0[^}]*right:\s*0[^}]*bottom:\s*0[^}]*left:\s*0/,
  );

  const overlayZ = Number(board.match(/\.panel-overlay\{[^}]*z-index:\s*(\d+)/)?.[1]);
  const panelZ = Number(board.match(/\.side-panel\{[^}]*z-index:\s*(\d+)/)?.[1]);
  const navZ = Number(navCss.match(/\.bnav\s*\{[^}]*z-index:\s*(\d+)/)?.[1]);
  assert.ok(Number.isFinite(overlayZ), 'board panel-overlay z-index missing');
  assert.ok(Number.isFinite(panelZ), 'board side-panel z-index missing');
  assert.ok(Number.isFinite(navZ), 'static nav z-index missing');
  assert.ok(overlayZ > navZ, `overlay z-index (${overlayZ}) must exceed bnav (${navZ})`);
  assert.ok(panelZ > overlayZ, `side-panel z-index (${panelZ}) must exceed overlay (${overlayZ})`);

  assert.match(board, /href="\/dash\/contacts"[^>]*>[\s\S]*?chat/);
  assert.doesNotMatch(board, /href="\/contacts"/);
  assert.doesNotMatch(board, /href="\/chat"/);
  assert.match(board, /href="\/static\/static-nav\.css"/);
}

// E. Static nav — Chat-style visual tokens (static/chat.html), Chrome78-safe
{
  const navCss = readStatic('static-nav.css');
  const chat = readStatic('chat.html');

  assert.match(chat, /\.bnav\{[^}]*background:rgba\(245,243,238/);
  assert.match(navCss, /background:\s*#fef4f5/);
  assert.match(navCss, /border-top:\s*0\.5px solid/);
  assert.match(navCss, /padding:\s*8px 0 calc\(8px \+ env\(safe-area-inset-bottom/);
  assert.match(navCss, /\.ni\s*\{[\s\S]*?color:\s*#b8b0b8/);
  assert.match(navCss, /\.ni\s*\{[\s\S]*?padding:\s*2px 0/);
  assert.match(navCss, /\.ni i\s*\{[\s\S]*?font-size:\s*21px/);
  assert.match(navCss, /\.ni i\s*\{[\s\S]*?margin-bottom:\s*3px/);
  assert.doesNotMatch(navCss, /\.ni\s*\{[^}]*\bgap\s*:/);
  assert.doesNotMatch(navCss, /background:\s*rgba\(255,\s*255,\s*255/);
}

// F. Ledger statistics stack + inner stats tab (G2 residual)
{
  const ledger = readSrc('screens/LedgerScreen.tsx');
  const statsBlock = ledger.slice(
    ledger.indexOf("tab === '统计'"),
    ledger.indexOf("tab === '日历'"),
  );
  assert.match(statsBlock, /className="ledger-stats-stack"/);
  assert.match(statsBlock, /className="ledger-stats-donut-row"/);
  assert.match(statsBlock, /className="ledger-stats-cat-list"/);
  assert.match(statsBlock, /className="ledger-stats-cat-row"/);
  assert.match(statsBlock, /className="ledger-stats-mode-toggle"/);
  assert.match(statsBlock, /ledger-stats-week-bars/);
  assert.match(statsBlock, /ledger-stats-week-col/);
  assert.match(statsBlock, /className="ledger-stats-bar-list"/);
  assert.doesNotMatch(statsBlock, /\bgap\s*:/);

  const css = readSrc('index.css');
  assert.match(css, /\.ledger-stats-stack > \* \+ \* \{[\s\S]*?margin-top:\s*16px/);
  assert.match(css, /\.ledger-stats-donut-row > \* \+ \* \{[\s\S]*?margin-left:\s*20px/);
  assert.match(css, /\.ledger-stats-cat-list > \* \+ \* \{[\s\S]*?margin-top:\s*8px/);
  assert.match(css, /\.ledger-stats-cat-row > \* \+ \* \{[\s\S]*?margin-left:\s*8px/);
  assert.match(css, /\.ledger-stats-mode-toggle > \* \+ \* \{[\s\S]*?margin-left:\s*4px/);
  assert.match(css, /\.ledger-stats-week-bars > \* \+ \* \{[\s\S]*?margin-left:\s*16px/);
  assert.match(css, /\.ledger-stats-week-bars--tight > \* \+ \* \{[\s\S]*?margin-left:\s*10px/);
  assert.match(css, /\.ledger-stats-week-col > \* \+ \* \{[\s\S]*?margin-top:\s*6px/);
  assert.match(css, /\.ledger-stats-bar-list > \* \+ \* \{[\s\S]*?margin-top:\s*13px/);
}

// G. Period calendar legend + record editor (G2 residual)
{
  const period = readSrc('screens/PeriodScreen.tsx');
  const recordBlock = period.slice(
    period.indexOf('选中日期记录卡'),
    period.indexOf('最近周期卡'),
  );

  assert.match(period, /className="period-calendar-legend"/);
  assert.doesNotMatch(period, /period-calendar-legend[\s\S]*?gap:\s*13/);
  assert.match(period, /period-calendar-legend[\s\S]*?<span>已记录经期<\/span>/);
  assert.match(period, /period-calendar-legend[\s\S]*?<span>亲密<\/span>/);

  assert.match(recordBlock, /className="period-record-row"/);
  assert.match(recordBlock, /className="period-record-wrap"/);
  assert.doesNotMatch(recordBlock, /period-record-row[\s\S]*?gap:\s*8/);
  assert.doesNotMatch(recordBlock, /period-record-wrap[\s\S]*?gap:\s*8/);

  const css = readSrc('index.css');
  assert.match(css, /\.period-calendar-legend > \* \{[\s\S]*?margin-right:\s*13px/);
  assert.match(css, /\.period-calendar-legend > \* > \* \+ \* \{[\s\S]*?margin-left:\s*5px/);
  assert.match(css, /\.period-record-row > \* \+ \* \{[\s\S]*?margin-left:\s*8px/);
  assert.match(css, /\.period-record-wrap > \* \{[\s\S]*?margin:\s*4px/);
}

// H. Memory filter console vertical spacing (G2 residual)
{
  const memory = readSrc('screens/MemoryScreen.tsx');
  const css = readSrc('index.css');

  assert.match(memory, /className="memory-filter-console"/);
  assert.match(memory, /ScreenLayout/);
  assert.match(readSrc('components/Card.tsx'), /vstack vstack-16 screen-stack/);
  assert.doesNotMatch(css, /\.memory-filter-console\s*\{[^}]*margin-top:\s*-/);
  assert.doesNotMatch(css, /\.memory-filter-console\s*\{[^}]*margin-top:\s*-4px/);
}

// I. Usage month grid square cells (G2 residual)
{
  const css = readSrc('index.css');
  const monthGrid = css.slice(
    css.indexOf('.config-month-grid {'),
    css.indexOf('.config-usage-summary'),
  );

  assert.doesNotMatch(monthGrid, /height:\s*46px/);
  assert.match(monthGrid, /\.config-month-grid button > span::before[\s\S]*?padding-top:\s*100%/);
  assert.doesNotMatch(monthGrid, /aspect-ratio/);
  assert.doesNotMatch(monthGrid, /\.config-month-grid em\s*\{[^}]*\binset\s*:\s*0/);
  assert.match(
    monthGrid,
    /\.config-month-grid em\s*\{[^}]*top:\s*0[^}]*right:\s*0[^}]*bottom:\s*0[^}]*left:\s*0/,
  );
}

// J. Memory star map — Chrome78-safe absolute canvas (no inset shorthand)
{
  const memory = readSrc('screens/MemoryScreen.tsx');
  const starBlock = memory.slice(
    memory.indexOf('function renderStarView()'),
    memory.indexOf('function renderDayDetail('),
  );

  assert.match(starBlock, /height:\s*460/);

  const transformCanvas = starBlock.match(
    /position:\s*'absolute',\s*top:\s*0,\s*right:\s*0,\s*bottom:\s*0,\s*left:\s*0,\s*transform:/,
  );
  assert.ok(transformCanvas, 'star transform canvas must use top/right/bottom/left: 0');

  assert.doesNotMatch(starBlock, /\binset:\s*0/);

  assert.match(starBlock, /left:\s*`\$\{s\.x\}%`/);
  assert.match(starBlock, /top:\s*`\$\{s\.y\}%`/);

  assert.match(memory, /topicLayout\.set\(t\.key,\s*\{\s*x:\s*50\s*\+\s*Math\.cos\(angle\)\s*\*\s*28/);
  assert.match(memory, /y:\s*50\s*\+\s*Math\.sin\(angle\)\s*\*\s*26/);
  assert.match(memory, /const angle = i \* 2\.4 \+ seeded\(m\.id\) \* 0\.8/);
  assert.match(memory, /const radius = i === 0 \? 0 : 7 \+ \(i % 3\) \* 5/);
  assert.match(memory, /Math\.max\(8,\s*Math\.min\(92/);
  assert.match(memory, /Math\.max\(10,\s*Math\.min\(88/);
}

console.log('test:chrome78-geometry-sweep — all checks passed');
