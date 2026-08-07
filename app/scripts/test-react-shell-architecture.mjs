import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = path.join(root, 'src');

function read(rel) {
  return fs.readFileSync(path.join(src, rel), 'utf8');
}

// 1. AppShell must not scale the viewport
{
  const shell = read('components/AppShell.tsx');
  assert.doesNotMatch(shell, /getShellZoom/);
  assert.doesNotMatch(shell, /style=\{[\s\S]*\bzoom\b/);
  assert.doesNotMatch(shell, /useState\(getShellZoom/);
  assert.doesNotMatch(shell, /transform:\s*scale|scale\(/);
}

// 2. App.tsx must not maintain an independent fullscreen product path list
{
  const app = read('App.tsx');
  assert.doesNotMatch(app, /FULLSCREEN_PATHS/);
  assert.doesNotMatch(app, /isFullscreenPath/);
  assert.match(app, /AppFrame/);
  assert.doesNotMatch(app, /BottomNav/);
}

// 3. Route chrome metadata has a single source in navigation.ts
{
  const nav = read('navigation.ts');
  assert.match(nav, /export const ROUTE_META/);
  assert.match(nav, /export function resolveRouteMeta/);
  assert.match(nav, /export function isFullscreenPath/);
  assert.doesNotMatch(nav, /FULLSCREEN_PATHS/);
  assert.match(nav, /ROUTE_META\[key\]/);
}

// 4. GlobalBottomNav renders only from NAV_ITEMS
{
  const bottom = read('components/GlobalBottomNav.tsx');
  assert.match(bottom, /NAV_ITEMS\.map/);
  assert.doesNotMatch(bottom, /key:\s*['"]dash['"][\s\S]*label:/);
}

// 5. Current visible global nav items remain dash / chat / read / board
{
  const nav = read('navigation.ts');
  const keys = [...nav.matchAll(/key:\s*'([^']+)'/g)].map((m) => m[1]);
  const navBlock = nav.slice(nav.indexOf('export const NAV_ITEMS'));
  const navKeys = [...navBlock.matchAll(/key:\s*'([^']+)'/g)].map((m) => m[1]);
  assert.deepEqual(navKeys, ['dash', 'chat', 'read', 'board']);
  assert.ok(keys.includes('dash'));
}

// 6. React screen files must not render canonical global nav directly
{
  const screensDir = path.join(src, 'screens');
  const offenders = [];
  for (const file of fs.readdirSync(screensDir)) {
    if (!file.endsWith('.tsx')) continue;
    const body = fs.readFileSync(path.join(screensDir, file), 'utf8');
    if (
      /from ['"][^'"]*BottomNav['"]/.test(body) ||
      /from ['"][^'"]*GlobalBottomNav['"]/.test(body) ||
      /<BottomNav\b/.test(body) ||
      /<GlobalBottomNav\b/.test(body)
    ) {
      offenders.push(file);
    }
  }
  assert.deepEqual(offenders, [], `screens must not import/render global nav: ${offenders.join(', ')}`);
}

// 7. AppFrame owns global nav rendering
{
  const frame = read('components/AppFrame.tsx');
  assert.match(frame, /resolveRouteMeta/);
  assert.match(frame, /GlobalBottomNav/);
}

console.log('test-react-shell-architecture: ok');
