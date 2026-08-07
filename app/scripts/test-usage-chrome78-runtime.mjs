import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { lastItem } from '../src/lib/lastItem.ts';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const usagePath = path.join(root, 'src/screens/UsageScreen.tsx');
const usageSource = fs.readFileSync(usagePath, 'utf8');

// 1. UsageScreen must not call Array.prototype.at
assert.doesNotMatch(usageSource, /\.at\(/, 'UsageScreen must not use .at()');

// 2. lastItem helper semantics
assert.equal(lastItem([]), undefined);
assert.equal(lastItem(['a']), 'a');
assert.equal(lastItem(['a', 'b', 'c']), 'c');

// 3. Empty daily rows — selection path must not throw
{
  const shownDaily = [];
  const selectedDay = '';
  const selectedUsage = shownDaily.find((item) => item.date === selectedDay) || lastItem(shownDaily);
  assert.equal(selectedUsage, undefined);
  const label = selectedUsage
    ? `${selectedUsage.date} · ${selectedUsage.count} 次请求`
    : '暂无用量数据';
  assert.equal(label, '暂无用量数据');
}

// 4. With daily rows — default latest selection
{
  const daily = [
    { date: '2026-08-05', count: 1, cost: null },
    { date: '2026-08-06', count: 2, cost: null },
    { date: '2026-08-07', count: 3, cost: null },
  ];
  const shownDaily = daily;
  const selectedDay = lastItem(daily)?.date || '';
  assert.equal(selectedDay, '2026-08-07');
  const selectedUsage = shownDaily.find((item) => item.date === selectedDay) || lastItem(shownDaily);
  assert.equal(selectedUsage?.date, '2026-08-07');
  assert.equal(selectedUsage?.count, 3);
}

// 5. Chrome78 narrow scan — only .at() was a P0 hit in UsageScreen
const blockers = ['.at(', 'replaceAll', 'Object.hasOwn', 'structuredClone'];
const hits = blockers.filter((token) => usageSource.includes(token));
assert.deepEqual(hits, [], `unexpected Chrome78 runtime tokens in UsageScreen: ${hits.join(', ')}`);

console.log('test-usage-chrome78-runtime: ok');
