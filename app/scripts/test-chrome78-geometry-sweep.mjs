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
  assert.match(navCss, /background:\s*rgba\(245,\s*243,\s*238,\s*0\.97\)/);
  assert.match(navCss, /border-top:\s*0\.5px solid/);
  assert.match(navCss, /padding:\s*8px 0 calc\(8px \+ env\(safe-area-inset-bottom/);
  assert.match(navCss, /\.ni\s*\{[\s\S]*?color:\s*#b8b0b8/);
  assert.match(navCss, /\.ni\s*\{[\s\S]*?padding:\s*2px 0/);
  assert.match(navCss, /\.ni i\s*\{[\s\S]*?font-size:\s*21px/);
  assert.match(navCss, /\.ni i\s*\{[\s\S]*?margin-bottom:\s*3px/);
  assert.doesNotMatch(navCss, /\.ni\s*\{[^}]*\bgap\s*:/);
  assert.doesNotMatch(navCss, /background:\s*rgba\(255,\s*255,\s*255/);
}

console.log('test:chrome78-geometry-sweep — all checks passed');
