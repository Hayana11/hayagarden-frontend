import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import ts from 'typescript';
import * as React from 'react';
import * as jsxRuntime from 'react/jsx-runtime';
import { renderToStaticMarkup } from 'react-dom/server';
import * as cards from '../src/components/Card.tsx';
import * as usageBars from '../src/components/UsageWindowBar.tsx';
import * as formatDisplay from '../src/lib/formatDisplay.ts';
import { lastItem } from '../src/lib/lastItem.ts';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const usagePath = path.join(root, 'src/screens/UsageScreen.tsx');
const usageSource = fs.readFileSync(usagePath, 'utf8');
const apiPath = path.join(root, 'src/lib/api.ts');
const apiSource = fs.readFileSync(apiPath, 'utf8');

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

// 6. Claude percentages must be accepted only from the OAuth-labelled snapshot.
assert.match(apiSource, /source === 'claude_oauth_usage'/);
assert.doesNotMatch(apiSource, /function legacyClaude/);
assert.doesNotMatch(apiSource, /100 - window\.remaining_percentage/);
assert.doesNotMatch(apiSource, /100 - used/);

// 7. Render the real private AgentQuotaCard without exporting a test-only app API.
// Only page-level dependencies are stubbed; the card, bars and formatters run.
const compiledUsage = ts.transpileModule(
  usageSource + '\nexport { AgentQuotaCard };',
  { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2019 } },
).outputText;
const renderedModule = { exports: {} };
const imports = {
  react: React,
  'react/jsx-runtime': jsxRuntime,
  '../components/Card': cards,
  '../components/UsageWindowBar': usageBars,
  '../lib/formatDisplay': formatDisplay,
  '../lib/lastItem': { lastItem },
  '../components/BackHeader': {},
  '../hooks/useUsage': {},
  '../lib/systemConfig': {},
};
vm.runInNewContext(compiledUsage, {
  exports: renderedModule.exports,
  module: renderedModule,
  require(name) {
    assert.ok(Object.prototype.hasOwnProperty.call(imports, name), `unexpected UsageScreen dependency: ${name}`);
    return imports[name];
  },
}, { filename: usagePath });
const { AgentQuotaCard } = renderedModule.exports;
assert.equal(typeof AgentQuotaCard, 'function');

const quotaWindow = { usedPct: null, remainingPct: null, resetAt: '', remainingMinutes: null };
const baseAgent = {
  id: 'claude', name: 'Claude Code', available: true, source: 'claude_project_jsonl',
  updatedAt: '2026-08-30T16:40:00Z', contextTokens: null, contextWindowTokens: null,
  effectiveLimit: null, fiveHour: quotaWindow, sevenDay: quotaWindow,
};
const observedAt = '2026-08-30T16:40:00Z';
function renderAgent(effectiveLimit, overrides = {}) {
  return renderToStaticMarkup(React.createElement(AgentQuotaCard, {
    agent: { ...baseAgent, effectiveLimit, ...overrides },
    now: new Date('2026-08-30T16:45:00Z'),
  }));
}
function limitAlert(html) {
  return html.match(/<div class="usage-limit-alert">([\s\S]*?)<\/div>/)?.[1] || '';
}
const limitCases = [
  [{ kind: 'rate_limit', exhausted: false, resetText: 'Try again after 5pm', observedAt }, 'Claude Code 请求暂时受限', 'Try again after 5pm'],
  [{ kind: 'rate_limit', exhausted: false, resetText: '', observedAt }, 'Claude Code 请求暂时受限', '稍后会自动重试'],
  [{ kind: 'weekly', exhausted: false, resetText: '', observedAt }, 'Claude Code 请求暂时受限', '稍后会自动重试'],
  [{ kind: 'usage_limit', exhausted: true, resetText: 'Resets at 1am', observedAt }, '当前额度已触达限制', 'Resets at 1am'],
  [{ kind: 'rate_limit', exhausted: true, resetText: '', observedAt }, '当前额度已触达限制', '等待下一次额度窗口恢复'],
  [{ kind: 'weekly', exhausted: true, resetText: 'Resets Sep 1', observedAt }, '周额度已触达限制', 'Resets Sep 1'],
  [{ kind: 'opus', exhausted: true, resetText: 'Resets at 3 PM', observedAt }, 'Opus 额度已触达限制', 'Resets at 3 PM'],
];
for (const [signal, title, resetText] of limitCases) {
  const html = renderAgent(signal);
  assert.ok(limitAlert(html).includes(`<strong>${title}</strong>`), `missing title for ${JSON.stringify(signal)}`);
  assert.ok(limitAlert(html).includes(resetText));
  assert.doesNotMatch(html, /当前额度已耗尽/);
}
assert.equal(limitAlert(renderAgent(null)), '', 'no signal must not create an alert');
const generic = limitCases[0][0];
assert.ok(limitAlert(renderAgent(generic, { available: false, source: 'unavailable' })), 'signal must not depend on quota availability');

// ccusage remains timing-only; official percentages and source remain intact.
const ccusageHtml = renderAgent(generic, {
  source: 'ccusage_blocks', fiveHour: { ...quotaWindow, remainingMinutes: 74 },
});
assert.ok(limitAlert(ccusageHtml));
assert.match(ccusageHtml, /剩余 74 分钟/);
assert.match(ccusageHtml, /ccusage active block/);
assert.doesNotMatch(ccusageHtml, />\d+%</);
const officialHtml = renderAgent(limitCases[4][0], {
  source: 'claude_oauth_usage',
  fiveHour: { ...quotaWindow, usedPct: 12, remainingPct: 88 },
  sevenDay: { ...quotaWindow, usedPct: 34, remainingPct: 66 },
});
assert.ok(limitAlert(officialHtml));
assert.match(officialHtml, />12% · 剩余 88%</);
assert.match(officialHtml, />34% · 剩余 66%</);
assert.match(officialHtml, /Claude 官方账号额度/);

// Codex retains its original exhausted-only gate and original wording.
const codex = { id: 'codex', name: 'Codex', source: 'codex_session_jsonl' };
assert.equal(limitAlert(renderAgent(generic, codex)), '');
const codexAlert = limitAlert(renderAgent({ ...generic, exhausted: true, resetText: '' }, codex));
assert.match(codexAlert, /<strong>当前额度已耗尽<\/strong>/);
assert.match(codexAlert, /等待下一次额度窗口恢复/);
assert.match(limitAlert(renderAgent({ ...generic, exhausted: true }, codex)), /Try again after 5pm/);

console.log('test-usage-chrome78-runtime: ok (runtime checks + 14 real-card render cases)');
