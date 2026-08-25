import assert from 'node:assert/strict';
import { resolveRouterBasename } from '../src/routerBasename.ts';

const cases: Array<[string, '/dash' | '/preview' | undefined]> = [
  ['/', undefined],
  ['/anything-else', undefined],
  ['/dash', '/dash'],
  ['/dash/', '/dash'],
  ['/dash/chat', '/dash'],
  ['/dash/settings', '/dash'],
  ['/preview', '/preview'],
  ['/preview/', '/preview'],
  ['/preview/chat', '/preview'],
  ['/preview/settings', '/preview'],
  ['/dashboard', undefined],
  ['/dash-old', undefined],
  ['/preview-old', undefined],
];

for (const [pathname, expected] of cases) {
  assert.equal(resolveRouterBasename(pathname), expected, pathname);
}

console.log('test:router-basename — all checks passed');
