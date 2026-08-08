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

// A. Memory — no page-level zoom; filter row without flex gap
{
  const memory = readSrc('screens/MemoryScreen.tsx');
  assert.doesNotMatch(memory, /zoom:\s*1\.07/);
  assert.match(memory, /className="memory-filter-console"/);
  assert.doesNotMatch(memory, /className="memory-filter-console"[^>]*gap/);

  const drag = readSrc('components/DragScrollRow.tsx');
  assert.match(drag, /drag-scroll-row-outer/);
  assert.doesNotMatch(drag, /\bgap\s*:/);

  const css = readSrc('index.css');
  assert.match(css, /\.drag-scroll-row-outer > \* \+ \* \{[\s\S]*?margin-left:\s*8px/);
  assert.match(css, /\.drag-scroll-row > \* \+ \* \{[\s\S]*?margin-left:\s*8px/);
  assert.doesNotMatch(css, /\.drag-scroll-row\s*\{[^}]*\bgap\s*:/);
}

// B. Ledger — Explore stack + calendar legend without flex gap
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
  assert.match(css, /\.calendar-legend-row > \* > \* \+ \* \{[\s\S]*?margin-left:\s*5px/);
}

// C. Read — inset fallback + /contacts chat nav
{
  const read = readStatic('read.html');
  assert.doesNotMatch(read, /\.mo\{[^}]*\binset\s*:\s*0/);
  assert.match(read, /\.mo\{[^}]*top:\s*0[^}]*right:\s*0[^}]*bottom:\s*0[^}]*left:\s*0/);
  assert.match(read, /href="\/contacts"[^>]*>[\s\S]*?chat/);
  assert.doesNotMatch(read, /href="\/chat"/);
  assert.match(read, /href="\/static\/static-nav\.css"/);
}

// D. Board — overlay inset fallback + /contacts chat nav
{
  const board = readStatic('board.html');
  assert.doesNotMatch(board, /\.panel-overlay\{[^}]*\binset\s*:\s*0/);
  assert.match(
    board,
    /\.panel-overlay\{[^}]*top:\s*0[^}]*right:\s*0[^}]*bottom:\s*0[^}]*left:\s*0/,
  );
  assert.match(board, /href="\/contacts"[^>]*>[\s\S]*?chat/);
  assert.doesNotMatch(board, /href="\/chat"/);
  assert.match(board, /href="\/static\/static-nav\.css"/);
}

// E. Static nav parity — shared stylesheet with canonical core values
{
  const navCss = readStatic('static-nav.css');
  assert.match(navCss, /background:\s*rgba\(255,\s*255,\s*255,\s*0\.97\)/);
  assert.match(navCss, /backdrop-filter:\s*blur\(14px\)/);
  assert.match(navCss, /border-top:\s*1px solid #ece6e0/);
  assert.match(navCss, /padding:\s*7px 0 calc\(7px \+ env\(safe-area-inset-bottom/);
  assert.match(navCss, /\.ni\s*\{[\s\S]*?color:\s*#b0a0aa/);
  assert.match(navCss, /\.ni i\s*\{[\s\S]*?font-size:\s*21px/);
  assert.match(navCss, /\.ni i\s*\{[\s\S]*?margin-bottom:\s*3px/);
  assert.match(navCss, /\.ni\.act\s*\{[\s\S]*?color:\s*#7c6a8a/);

  const reactNav = readSrc('index.css');
  assert.match(reactNav, /--nav-muted:\s*#b0a0aa/);
  assert.match(reactNav, /--nav-icon-size:\s*21px/);
  assert.match(reactNav, /--nav-label-size:\s*10px/);
}

console.log('test:chrome78-geometry-sweep — all checks passed');
