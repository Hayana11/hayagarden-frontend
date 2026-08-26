import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { installNativeTopInset } from '../src/lib/nativeTopInset.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = (relativePath) => fs.readFileSync(path.join(here, '..', relativePath), 'utf8');

function makeRuntime(raw, withBridge = true) {
  let calls = 0;
  const properties = new Map();
  const attributes = new Map();
  const root = {
    style: {
      setProperty(name, value) {
        properties.set(name, value);
      },
    },
    setAttribute(name, value) {
      attributes.set(name, value);
    },
  };
  const runtime = {
    document: { documentElement: root },
  };
  if (withBridge) {
    runtime.ElpisInsets = {
      getTopInset() {
        calls += 1;
        return raw;
      },
    };
  }
  return { runtime, properties, attributes, get calls() { return calls; } };
}

{
  const state = makeRuntime(undefined, false);
  assert.equal(installNativeTopInset(state.runtime), false);
  assert.equal(state.calls, 0);
  assert.equal(state.properties.size, 0);
  assert.equal(state.attributes.size, 0);
}

{
  const state = makeRuntime(JSON.stringify({
    schemaVersion: 1,
    available: true,
    edgeToEdgeTop: true,
    topInsetPx: 18,
    density: 3,
    topInsetCssPx: 6,
  }));
  assert.equal(installNativeTopInset(state.runtime), true);
  assert.equal(state.properties.get('--elpis-safe-top'), '6px');
  assert.equal(state.attributes.get('data-elpis-top-overlay'), 'true');
  assert.equal(installNativeTopInset(state.runtime), false);
  assert.equal(state.calls, 1);
}

for (const raw of [
  JSON.stringify({ schemaVersion: 1, available: false, edgeToEdgeTop: false }),
  '{malformed',
  JSON.stringify({ schemaVersion: 2, available: true, edgeToEdgeTop: true, topInsetCssPx: 6 }),
  JSON.stringify({
    schemaVersion: 1,
    available: true,
    edgeToEdgeTop: true,
    topInsetPx: 18,
    density: 3,
    topInsetCssPx: 201,
  }),
]) {
  const state = makeRuntime(raw);
  assert.doesNotThrow(() => installNativeTopInset(state.runtime));
  assert.equal(state.properties.size, 0);
  assert.equal(state.attributes.size, 0);
  assert.equal(state.calls, 1);
}

const main = source('src/main.tsx');
assert(main.indexOf('installNativeTopInset()') < main.indexOf('createRoot('));
assert(main.includes("import { installNativeTopInset } from './lib/nativeTopInset'"));
assert(main.includes('installNativeTopInset()'));

const app = source('src/App.tsx');
assert(app.includes('const basename = resolveRouterBasename(window.location.pathname);'));

const legacyCompat = source('src/lib/legacyNativeCompat.ts');
assert(legacyCompat.includes("body.style.zoom = '0.8'"));

const helper = source('src/lib/nativeTopInset.ts');
assert(!/24px|32px|status_bar_height|setNavigationBarColor|setDecorFitsSystemWindows/.test(helper));
const css = source('src/index.css');
assert(css.includes("html[data-elpis-top-overlay='true']"));
assert(css.includes('var(--elpis-safe-top)'));
assert(css.includes('.c78-fill-fixed'));
assert(!css.includes('env(safe-area-inset-top'));

console.log('test:native-top-inset — all checks passed');
