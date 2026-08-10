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

// 8. Embedded nav must not inherit fixed viewport positioning
{
  const css = fs.readFileSync(path.join(root, 'src/index.css'), 'utf8');
  const navBlock = css.slice(css.indexOf('/* Canonical GlobalBottomNav'));
  assert.doesNotMatch(
    navBlock,
    /\.global-bottom-nav\s*\{[\s\S]*?position:\s*fixed/,
    'base .global-bottom-nav must not be position:fixed (use --fixed modifier)',
  );
  assert.match(navBlock, /\.global-bottom-nav--fixed\s*\{[\s\S]*?position:\s*fixed/);
  assert.match(navBlock, /\.global-bottom-nav--embedded[\s\S]*?flex-shrink:\s*0/);
  assert.doesNotMatch(navBlock, /\.global-bottom-nav--embedded[\s\S]*?position:\s*fixed/);

  const bottom = read('components/GlobalBottomNav.tsx');
  assert.match(bottom, /global-bottom-nav--fixed/);
  assert.match(bottom, /global-bottom-nav--embedded/);
  const embeddedOnly = bottom.slice(
    bottom.indexOf("if (variant === 'embedded')"),
    bottom.indexOf("  return (\n    <nav className=\"bnav global-bottom-nav global-bottom-nav--fixed\""),
  );
  assert.doesNotMatch(embeddedOnly, /global-bottom-nav--fixed/);
}

// 9. Both placement variants share canonical __inner DOM; placement must not fork visuals
{
  const bottom = read('components/GlobalBottomNav.tsx');
  assert.match(bottom, /const inner = <div className="[^"]*global-bottom-nav__inner"/);
  assert.match(bottom, /if \(variant === 'embedded'\)[\s\S]*\{inner\}/);
  assert.match(bottom, /global-bottom-nav--fixed[\s\S]*\{inner\}/);

  const css = fs.readFileSync(path.join(root, 'src/index.css'), 'utf8');
  const navBlock = css.slice(css.indexOf('/* Canonical GlobalBottomNav'));

  const embeddedRule = navBlock.match(/\.global-bottom-nav--embedded\s*\{([^}]*)\}/);
  assert.ok(embeddedRule, 'expected .global-bottom-nav--embedded rule');
  const embeddedBody = embeddedRule[1];
  assert.doesNotMatch(embeddedBody, /\bcolor\s*:/);
  assert.doesNotMatch(embeddedBody, /\bfont(-size|-family)?\s*:/);
  assert.doesNotMatch(embeddedBody, /\bpadding\s*:/);
  assert.doesNotMatch(embeddedBody, /\bborder/);
  assert.doesNotMatch(embeddedBody, /\bbackground/);

  assert.doesNotMatch(
    navBlock,
    /\.global-bottom-nav--embedded[\s\S]*?\.global-bottom-nav__item[\s\S]*?\bcolor\s*:/,
    'embedded variant must not override item color',
  );
  assert.doesNotMatch(
    navBlock,
    /\.global-bottom-nav--embedded[\s\S]*?\.global-bottom-nav__item[\s\S]*?\bpadding\s*:/,
    'embedded variant must not override item padding',
  );

  assert.doesNotMatch(
    navBlock,
    /\.global-bottom-nav--embedded[\s\S]*?\.global-bottom-nav__item\.act[\s\S]*?\bcolor\s*:/,
    'embedded variant must not override active color',
  );
}

// 10. GlobalBottomNav final product contract + legacy native physical parity
{
  const appRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const repoRoot = path.dirname(appRoot);
  const staticNav = fs.readFileSync(path.join(repoRoot, 'static/static-nav.css'), 'utf8');
  const css = fs.readFileSync(path.join(appRoot, 'src/index.css'), 'utf8');
  const navBlock = css.slice(css.indexOf('/* Canonical GlobalBottomNav'));
  const bottom = read('components/GlobalBottomNav.tsx');
  const nav = read('navigation.ts');
  const legacyCompat = read('lib/legacyNativeCompat.ts');

  // 11.1 Final colors
  assert.match(staticNav, /\.bnav\s*\{[\s\S]*?background:\s*#fef4f5/);
  assert.match(navBlock, /\.global-bottom-nav__inner[\s\S]*?background:\s*#fef4f5/);
  assert.match(staticNav, /\.ni\.act\s*\{[\s\S]*?color:\s*#d27790/);
  assert.match(navBlock, /\.global-bottom-nav__item\.act[\s\S]*?color:\s*#d27790/);
  assert.match(navBlock, /\.global-bottom-nav__item[\s\S]*?color:\s*#b8b0b8/);

  // 11.2 Base dimensions (static + React base, unscaled canonical)
  assert.match(staticNav, /padding:\s*8px 0 calc\(8px \+ env\(safe-area-inset-bottom/);
  assert.match(staticNav, /\.ni\s*\{[\s\S]*?padding:\s*2px 0/);
  assert.match(staticNav, /\.ni\s*\{[\s\S]*?font-size:\s*10px/);
  assert.match(staticNav, /\.ni i\s*\{[\s\S]*?font-size:\s*21px/);
  assert.match(staticNav, /margin-bottom:\s*3px/);
  assert.match(staticNav, /border-top:\s*0\.5px solid/);

  assert.match(navBlock, /padding:\s*8px 0 calc\(8px \+ env\(safe-area-inset-bottom,\s*0px\)\)/);
  assert.match(navBlock, /\.global-bottom-nav__item[\s\S]*?font-size:\s*10px/);
  assert.match(navBlock, /\.global-bottom-nav__item[\s\S]*?padding:\s*2px 0/);
  assert.match(navBlock, /\.global-bottom-nav__icon[\s\S]*?font-size:\s*21px/);
  assert.match(navBlock, /border-top:\s*0\.5px solid rgba\(210,\s*185,\s*155,\s*0\.2\)/);
  assert.match(navBlock, /backdrop-filter:\s*blur\(14px\)/);

  // 11.3 Legacy native compensation (body zoom 0.8 → metric ×1.25)
  assert.match(
    navBlock,
    /body\[data-legacy-native-compat='true'\]\s*\.global-bottom-nav__inner[\s\S]*?padding:\s*10px 0 calc\(10px \+ env\(safe-area-inset-bottom,\s*0px\)\)/,
  );
  assert.match(
    navBlock,
    /body\[data-legacy-native-compat='true'\]\s*\.global-bottom-nav__inner[\s\S]*?border-top-width:\s*0\.625px/,
  );
  assert.match(
    navBlock,
    /body\[data-legacy-native-compat='true'\]\s*\.global-bottom-nav__item[\s\S]*?font-size:\s*12\.5px/,
  );
  assert.match(
    navBlock,
    /body\[data-legacy-native-compat='true'\]\s*\.global-bottom-nav__item[\s\S]*?padding:\s*2\.5px 0/,
  );
  assert.match(
    navBlock,
    /body\[data-legacy-native-compat='true'\]\s*\.global-bottom-nav__icon[\s\S]*?font-size:\s*26\.25px/,
  );
  assert.match(
    navBlock,
    /body\[data-legacy-native-compat='true'\]\s*\.global-bottom-nav__icon[\s\S]*?margin-bottom:\s*3\.75px/,
  );

  // 11.4 Scale authority frozen — body zoom 0.8; no nav zoom/scale layer
  assert.match(legacyCompat, /body\.style\.zoom\s*=\s*'0\.8'/);
  assert.doesNotMatch(navBlock, /zoom:\s*1\.25/);
  assert.doesNotMatch(navBlock, /transform:\s*scale\(1\.25\)/);

  // No fixed gradient
  assert.doesNotMatch(navBlock, /\.global-bottom-nav--fixed::before/);

  // 11.5 Routing frozen
  assert.match(bottom, /global-bottom-nav__inner/);
  const navBlockTs = nav.slice(nav.indexOf('export const NAV_ITEMS'));
  const navKeys = [...navBlockTs.matchAll(/key:\s*'([^']+)'/g)].map((m) => m[1]);
  assert.deepEqual(navKeys, ['dash', 'chat', 'read', 'board']);
  assert.match(nav, /to:\s*ROUTES\.contacts/);
  assert.match(nav, /activePaths:\s*\[ROUTES\.contacts,\s*ROUTES\.chat\]/);

  assert.doesNotMatch(navBlock, /\.global-bottom-nav[\s\S]*?\bgap\s*:/);
}

console.log('test-react-shell-architecture: ok');
